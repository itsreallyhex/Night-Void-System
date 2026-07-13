"""Shared helpers used across the cogs.

Collects the small patterns that were duplicated in several cogs: UTC
timestamps for DB rows, role/permission gates for slash commands, the panel
posting dance (logo attachment + Forbidden handling), and best-effort role
assignment. Cogs import this module instead of re-implementing these.
"""

import logging
import re
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands

import branding
import config

log = logging.getLogger("nightvoid.utilities")


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
def utc_now_iso() -> str:
    """Current UTC time in the fixed ISO-8601 format every DB row uses.

    All created_at/redeemed_at/closed_at columns are written through this so
    string comparison on timestamps stays valid everywhere.
    """
    return datetime.now(timezone.utc).isoformat()


_DURATION_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_DURATION_PATTERN = re.compile(r"^([0-9]*\.?[0-9]+)\s*([mhdw])$", re.IGNORECASE)


def parse_duration(text: str) -> timedelta:
    """Parse a human duration like '30m', '2h', '7d' or '1w' into a timedelta.

    The unit suffix is required (a bare number is ambiguous). Raises ValueError
    on anything else.
    """
    match = _DURATION_PATTERN.match(text.strip().lower())
    if not match:
        raise ValueError(f"invalid duration: {text!r}")
    value, unit = float(match.group(1)), match.group(2)
    return timedelta(**{_DURATION_UNITS[unit]: value})


# --------------------------------------------------------------------------- #
# Role checks
# --------------------------------------------------------------------------- #
def has_role(user: discord.abc.User, role_id: int) -> bool:
    """True if `user` is a guild Member carrying the role.

    Safe to call with a plain User (e.g. a DM interaction) — returns False
    instead of raising on the missing `roles` attribute.
    """
    return isinstance(user, discord.Member) and any(r.id == role_id for r in user.roles)


def is_admin(user: discord.abc.User) -> bool:
    return has_role(user, config.ADMIN_ROLE_ID)


def is_staff(user: discord.abc.User) -> bool:
    return has_role(user, config.TICKET_STAFF_ROLE_ID)


# --------------------------------------------------------------------------- #
# Slash-command permission gates
#
# Each check sends its own ephemeral rejection before failing, so the tree
# error handler must swallow app_commands.CheckFailure (bot.py does).
# --------------------------------------------------------------------------- #
def owner_only():
    """App-command check: only the bot's application owner passes."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if await interaction.client.is_owner(interaction.user):
            return True
        await interaction.response.send_message(
            "❌ هذا الأمر للمالك فقط.", ephemeral=True
        )
        return False

    return app_commands.check(predicate)


def admin_only(message: str = "❌ ما عندك صلاحية تستخدم هذا."):
    """App-command check: requires the admin role (ADMIN_ROLE_ID)."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if is_admin(interaction.user):
            return True
        await interaction.response.send_message(message, ephemeral=True)
        return False

    return app_commands.check(predicate)


def staff_only(message: str = "❌ ما عندك صلاحية تستخدم هذا."):
    """App-command check: requires the ticket staff role (TICKET_STAFF_ROLE_ID)."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if is_staff(interaction.user):
            return True
        await interaction.response.send_message(message, ephemeral=True)
        return False

    return app_commands.check(predicate)


# --------------------------------------------------------------------------- #
# Panels
# --------------------------------------------------------------------------- #
async def post_panel(
    interaction: discord.Interaction, embed: discord.Embed, view: discord.ui.View
) -> bool:
    """Post a public panel embed (with the store logo) to the invoking channel.

    Handles the logo attachment dance and, on missing channel permissions,
    replies to the invoker ephemerally and returns False. On success returns
    True — the caller sends its own confirmation so each panel keeps its
    specific wording.
    """
    logo = branding.logo_file()
    if logo is not None:
        embed.set_thumbnail(url=branding.LOGO_ATTACHMENT)
    kwargs: dict = {"embed": embed, "view": view}
    if logo is not None:
        kwargs["file"] = logo
    try:
        await interaction.channel.send(**kwargs)
    except discord.Forbidden:
        log.error("Missing access to post panel in channel %s", interaction.channel_id)
        await interaction.response.send_message(
            "❌ ما عندي صلاحية أنشر في هذي القناة. تأكد إني أقدر **أشوف القناة** "
            "وأرسل فيها (Send Messages / Embed Links / Attach Files).",
            ephemeral=True,
        )
        return False
    return True


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #
async def try_add_role(member: discord.Member, role: discord.Role, reason: str) -> bool:
    """Assign a role, logging any failure. Returns False on permission/HTTP
    errors (role hierarchy, missing Manage Roles) so the caller can refund or
    compensate the user."""
    try:
        await member.add_roles(role, reason=reason)
        return True
    except discord.Forbidden:
        log.error(
            "Missing permission to assign role %s to %s (%s). Check role hierarchy.",
            role.id, member, reason,
        )
    except discord.HTTPException:
        log.exception("HTTP error assigning role %s to %s (%s)", role.id, member, reason)
    return False
