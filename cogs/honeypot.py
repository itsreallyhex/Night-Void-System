"""Honeypot channel trap.

One channel is designated as bait: legitimate members have no reason to post
there, so anything that does (typically a compromised account or spam bot)
triggers an automatic response — kick, ban, strip roles, or alert-only. The
only record kept is a bare numeric counter on a tracking embed; by design this
feature never stores who triggered it or when.

Single-guild by design (like the rest of the bot): exactly one configuration,
one safe-role list, no guild_id anywhere.
"""

import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

import branding
import utilities

log = logging.getLogger("nightvoid.honeypot")

# Display labels for the tracking embed / status output. Keys double as the
# canonical action_type values stored in the database.
ACTION_LABELS = {
    "kick": "طرد (Kick)",
    "ban": "حظر (Ban)",
    "remove_roles": "سحب الرتب (Remove Roles)",
    "timeout": "كتم مؤقت (Timeout)",
    "alert_only": "تنبيه فقط (Alert Only)",
}

ACTION_CHOICES = [
    app_commands.Choice(name=label, value=value)
    for value, label in ACTION_LABELS.items()
]

# Discord's hard API cap on communication_disabled_until.
MAX_TIMEOUT = timedelta(days=28)


def _format_duration(seconds: int) -> str:
    """Render seconds back into the compact form parse_duration accepts."""
    for unit_seconds, suffix in ((604800, "w"), (86400, "d"), (3600, "h"), (60, "m")):
        if seconds % unit_seconds == 0:
            return f"{seconds // unit_seconds}{suffix}"
    return f"{seconds}s"


def _action_label(action_type: str, timeout_seconds: int) -> str:
    """Display label for an action; timeout carries its duration."""
    label = ACTION_LABELS.get(action_type, action_type)
    if action_type == "timeout":
        label += f" — {_format_duration(timeout_seconds)}"
    return label


async def _parse_timeout_arg(
    interaction: discord.Interaction, duration: str
) -> int | None:
    """Parse a duration option into seconds, replying ephemerally (and
    returning None) when it's malformed, non-positive, or over Discord's
    28-day timeout cap."""
    try:
        delta = utilities.parse_duration(duration)
    except ValueError:
        await interaction.response.send_message(
            "❌ المدة غير صالحة. اكتب شي مثل `30m` أو `2h` أو `7d`.",
            ephemeral=True,
        )
        return None
    if delta <= timedelta(0) or delta > MAX_TIMEOUT:
        await interaction.response.send_message(
            "❌ أقصى مدة كتم يسمح فيها ديسكورد **28 يوم**.", ephemeral=True
        )
        return None
    return int(delta.total_seconds())


class Honeypot(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db
        # Hot-path cache for on_message (which fires on every guild message):
        # only the fields needed to decide "is this a trigger?" live here. The
        # full config row is re-read from the DB on the rare actual trigger.
        self._channel_id: int | None = None
        self._enabled: bool = False
        self._safe_role_ids: set[int] = set()

    async def cog_load(self) -> None:
        await self._refresh_cache()

    async def _refresh_cache(self) -> None:
        """Re-read the trigger-decision fields after any config mutation."""
        row = await self.db.get_honeypot_config()
        self._channel_id = row["channel_id"] if row else None
        self._enabled = bool(row["enabled"]) if row else False
        self._safe_role_ids = set(await self.db.list_safe_roles())

    # ------------------------------------------------------------------ #
    # Tracking embed
    # ------------------------------------------------------------------ #
    def _build_embed(
        self,
        channel_id: int,
        action_type: str,
        enabled: bool,
        count: int,
        timeout_seconds: int,
    ) -> discord.Embed:
        # Deliberately no timestamp and no per-incident detail: the counter is
        # the entire record this feature keeps.
        #
        # State-driven semantic color: an armed trap is a danger surface,
        # a disarmed one drops to low-attention neutral. The الحالة field
        # spells the state out in text too, so color is never the only signal.
        embed = discord.Embed(
            title="⚠️ لا ترسل أي رسالة هنا",
            description=(
                "هذي القناة مصيدة أمنية لاكتشاف الحسابات المخترقة.\n"
                "الحسابات المخترقة غالباً ترسل روابط سبام (مسابقات وهمية، "
                "مستر بيست، مواقع قمار) بكل قناة تقدر توصلها تلقائياً.\n\n"
                "**أي رسالة تُرسل هنا = عقوبة تلقائية فورية.**\n\n"
                "لو انكتمت أو انطردت من هالقناة، الأغلب حسابك مخترق.\n"
                "غيّر كلمة المرور وفعّل التحقق بخطوتين (2FA) فوراً.\n\n"
                "لو ما سويت شي، هذا التنبيه ما يخصك."
            ),
            color=branding.DANGER if enabled else branding.NEUTRAL,
        )
        # Hierarchy: the counter is the hero metric (the feature's only
        # record) — full-width on top; config details follow as one row.
        embed.add_field(name="🎯 عدد التفعيلات", value=f"**{count:,}**", inline=False)
        embed.add_field(name="القناة", value=f"<#{channel_id}>", inline=True)
        embed.add_field(
            name="الإجراء",
            value=_action_label(action_type, timeout_seconds),
            inline=True,
        )
        embed.add_field(
            name="الحالة", value="🟢 مفعّل" if enabled else "🔴 معطّل", inline=True
        )
        # Brand consistency with the other panels: logo thumbnail whenever the
        # asset exists on disk. attachment:// resolves on sends that attach the
        # file, and on edits of a message that already carries it.
        if branding.logo_file() is not None:
            embed.set_thumbnail(url=branding.LOGO_ATTACHMENT)
        embed.set_footer(text=f"{branding.FOOTER} • المصيدة")
        return embed

    @staticmethod
    async def _send_tracking_embed(
        channel: discord.abc.Messageable, embed: discord.Embed
    ) -> discord.Message:
        """Send the tracking embed with the store logo attached (the same
        attachment dance utilities.post_panel does for the other panels)."""
        kwargs: dict = {"embed": embed}
        logo = branding.logo_file()
        if logo is not None:
            kwargs["file"] = logo
        return await channel.send(**kwargs)

    async def _update_embed(self, row, count: int) -> None:
        """Edit the tracking embed in place; self-heal if it's gone.

        Never surfaces an error to the member who tripped the trap — if the
        stored message can't be edited (deleted, permissions changed, transient
        HTTP failure) a fresh embed is posted to the configured channel and the
        stored message id is repointed at it.
        """
        channel = self.bot.get_channel(row["channel_id"])
        if channel is None:
            log.warning(
                "Honeypot: configured channel %s not found; embed not updated.",
                row["channel_id"],
            )
            return
        embed = self._build_embed(
            row["channel_id"],
            row["action_type"],
            bool(row["enabled"]),
            count,
            row["timeout_seconds"],
        )
        if row["embed_message_id"]:
            try:
                # Partial message: edits by id without a fetch round-trip.
                await channel.get_partial_message(row["embed_message_id"]).edit(
                    embed=embed
                )
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass  # fall through to the self-heal repost below
        try:
            message = await self._send_tracking_embed(channel, embed)
            await self.db.set_honeypot_embed_message_id(message.id)
        except (discord.Forbidden, discord.HTTPException):
            log.error(
                "Honeypot: could not repost tracking embed in channel %s.",
                row["channel_id"],
            )

    async def _refresh_embed_from_db(self) -> None:
        """Push the current DB state onto the embed (after enable/disable/
        action changes), reusing the self-healing edit path."""
        row = await self.db.get_honeypot_config()
        if row is not None:
            await self._update_embed(row, row["trigger_count"])

    # ------------------------------------------------------------------ #
    # Trigger
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Bots (including this bot's own tracking embed) never trigger.
        if message.guild is None or message.author.bot:
            return
        if not self._enabled or message.channel.id != self._channel_id:
            return
        member = message.author

        # Safe-role exemption comes BEFORE deletion: exempt members keep their
        # message and cause no action and no counter bump.
        if any(r.id in self._safe_role_ids for r in member.roles):
            return

        row = await self.db.get_honeypot_config()
        if row is None:
            # Cache says armed but the config row is gone — resync and bail.
            await self._refresh_cache()
            return

        # Deletion always happens first, and its failure never blocks the
        # configured action.
        try:
            await message.delete()
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Honeypot: could not delete trigger message: %s", exc)

        await self._apply_action(member, row)

        # The counter bump + embed edit is the only record of the trigger.
        count = await self.db.increment_honeypot_counter()
        await self._update_embed(row, count)

    async def _apply_action(self, member: discord.Member, row) -> None:
        """Apply the configured action, try_add_role-style: log any permission
        or hierarchy failure and continue — the counter still increments."""
        action = row["action_type"]
        guild = member.guild
        try:
            if action == "kick":
                await guild.kick(member, reason="Honeypot channel trigger")
            elif action == "ban":
                await guild.ban(
                    member,
                    reason="Honeypot channel trigger",
                    delete_message_seconds=0,
                )
            elif action == "remove_roles":
                # Strip everything except: @everyone (unremovable by the API),
                # safe-listed roles, and managed roles (bot/boost roles — the
                # API rejects removing those with a 400).
                roles = [
                    r
                    for r in member.roles
                    if r != guild.default_role
                    and r.id not in self._safe_role_ids
                    and not r.managed
                ]
                if roles:
                    await member.remove_roles(
                        *roles, reason="Honeypot channel trigger"
                    )
            elif action == "timeout":
                # Duration is validated (positive, <= 28d) at command time,
                # so no clamping is needed here.
                await member.timeout(
                    timedelta(seconds=row["timeout_seconds"]),
                    reason="Honeypot channel trigger",
                )
            # alert_only: nothing beyond the embed counter bump.
        except discord.Forbidden:
            log.error(
                "Honeypot: missing permission/hierarchy for action %r.", action
            )
        except discord.HTTPException:
            log.exception("Honeypot: HTTP error applying action %r.", action)

    # ------------------------------------------------------------------ #
    # Owner commands
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="honeypot-setup", description="جهّز قناة المصيدة (للمالك فقط)."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        channel="القناة اللي بتصير مصيدة",
        action="وش يصير لأي عضو يرسل فيها",
        duration="مدة الكتم إذا اخترت Timeout — مثل 30m أو 2h أو 7d (الافتراضي يوم)",
    )
    @app_commands.choices(action=ACTION_CHOICES)
    @utilities.owner_only()
    async def honeypot_setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        action: app_commands.Choice[str],
        duration: str | None = None,
    ) -> None:
        timeout_seconds: int | None = None
        if duration is not None:
            timeout_seconds = await _parse_timeout_arg(interaction, duration)
            if timeout_seconds is None:
                return
        # Re-setup keeps the lifetime counter: read it before overwriting so
        # the freshly posted embed doesn't show 0 on an established trap. The
        # stored timeout duration survives a re-setup too, unless overridden.
        existing = await self.db.get_honeypot_config()
        count = existing["trigger_count"] if existing else 0
        effective_timeout = timeout_seconds or (
            existing["timeout_seconds"] if existing else 86400
        )

        embed = self._build_embed(
            channel.id, action.value, True, count, effective_timeout
        )
        try:
            message = await self._send_tracking_embed(channel, embed)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                f"❌ ما قدرت أرسل في {channel.mention}. تأكد إن عندي صلاحية "
                "أشوف القناة وأرسل فيها (Send Messages / Embed Links).",
                ephemeral=True,
            )
            return

        await self.db.upsert_honeypot_config(
            channel.id, action.value, message.id, timeout_seconds
        )
        await self._refresh_cache()
        await interaction.response.send_message(
            f"✅ تجهزت المصيدة في {channel.mention} — "
            f"الإجراء: **{_action_label(action.value, effective_timeout)}**.",
            ephemeral=True,
        )
        log.info("Honeypot configured: channel=%s action=%s", channel.id, action.value)

    @app_commands.command(
        name="honeypot-status", description="شوف وش وضع المصيدة (للمالك فقط)."
    )
    @app_commands.default_permissions(administrator=True)
    @utilities.owner_only()
    async def honeypot_status(self, interaction: discord.Interaction) -> None:
        row = await self.db.get_honeypot_config()
        if row is None:
            await interaction.response.send_message(
                "❌ المصيدة مو مجهزة. استخدم `/honeypot-setup` أول.", ephemeral=True
            )
            return
        safe_ids = await self.db.list_safe_roles()
        safe = " ".join(f"<@&{rid}>" for rid in safe_ids) or "لا يوجد"
        await interaction.response.send_message(
            f"**القناة:** <#{row['channel_id']}>\n"
            f"**الإجراء:** {_action_label(row['action_type'], row['timeout_seconds'])}\n"
            f"**الحالة:** {'🟢 مفعّل' if row['enabled'] else '🔴 معطّل'}\n"
            f"**عدد التفعيلات:** {row['trigger_count']:,}\n"
            f"**الرتب المستثناة:** {safe}",
            ephemeral=True,
        )

    @app_commands.command(
        name="honeypot-enable", description="شغّل المصيدة (للمالك فقط)."
    )
    @app_commands.default_permissions(administrator=True)
    @utilities.owner_only()
    async def honeypot_enable(self, interaction: discord.Interaction) -> None:
        await self._set_enabled(interaction, True, "✅ المصيدة اشتغلت.")

    @app_commands.command(
        name="honeypot-disable", description="وقّف المصيدة (للمالك فقط)."
    )
    @app_commands.default_permissions(administrator=True)
    @utilities.owner_only()
    async def honeypot_disable(self, interaction: discord.Interaction) -> None:
        await self._set_enabled(interaction, False, "✅ المصيدة توقفت.")

    async def _set_enabled(
        self, interaction: discord.Interaction, enabled: bool, confirmation: str
    ) -> None:
        if await self.db.get_honeypot_config() is None:
            await interaction.response.send_message(
                "❌ المصيدة مو مجهزة. استخدم `/honeypot-setup` أول.", ephemeral=True
            )
            return
        await self.db.set_honeypot_enabled(enabled)
        await self._refresh_cache()
        # Keep the tracking embed's state field truthful.
        await self._refresh_embed_from_db()
        await interaction.response.send_message(confirmation, ephemeral=True)
        log.info("Honeypot %s.", "enabled" if enabled else "disabled")


class HoneypotSafeRoles(app_commands.Group):
    """/honeypot-safe-roles add|remove|list — roles fully exempt from the trap."""

    def __init__(self, cog: Honeypot) -> None:
        super().__init__(
            name="honeypot-safe-roles",
            description="الرتب اللي المصيدة ما تلمسها (للمالك فقط).",
            default_permissions=discord.Permissions(administrator=True),
        )
        self.cog = cog

    @app_commands.command(name="add", description="خلّ المصيدة ما تلمس أصحاب رتبة معيّنة.")
    @app_commands.describe(role="الرتبة اللي تبي المصيدة تطنّشها")
    @utilities.owner_only()
    async def add(self, interaction: discord.Interaction, role: discord.Role) -> None:
        await self.cog.db.add_safe_role(role.id)
        await self.cog._refresh_cache()
        await interaction.response.send_message(
            f"✅ رتبة {role.mention} صارت مستثناة من المصيدة.", ephemeral=True
        )
        log.info("Honeypot safe role added: %s", role.id)

    @app_commands.command(name="remove", description="شِل رتبة من الاستثناء وخلّ المصيدة تمسكها.")
    @app_commands.describe(role="الرتبة اللي تبي تشيلها من الاستثناء")
    @utilities.owner_only()
    async def remove(
        self, interaction: discord.Interaction, role: discord.Role
    ) -> None:
        removed = await self.cog.db.remove_safe_role(role.id)
        await self.cog._refresh_cache()
        if removed:
            await interaction.response.send_message(
                f"✅ رتبة {role.mention} ما عادت مستثناة.", ephemeral=True
            )
            log.info("Honeypot safe role removed: %s", role.id)
        else:
            await interaction.response.send_message(
                f"❌ رتبة {role.mention} أصلاً مو في قائمة الاستثناء.", ephemeral=True
            )

    @app_commands.command(name="list", description="شوف الرتب اللي المصيدة ما تلمسها.")
    @utilities.owner_only()
    async def list(self, interaction: discord.Interaction) -> None:
        role_ids = await self.cog.db.list_safe_roles()
        if not role_ids:
            await interaction.response.send_message(
                "ما فيه رتب مستثناة حالياً.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "**الرتب المستثناة:**\n" + "\n".join(f"• <@&{rid}>" for rid in role_ids),
            ephemeral=True,
        )


class HoneypotAction(app_commands.Group):
    """/honeypot-action set — change the configured action without a re-setup."""

    def __init__(self, cog: Honeypot) -> None:
        super().__init__(
            name="honeypot-action",
            description="وش تسوي المصيدة بللي يرسل فيها (للمالك فقط).",
            default_permissions=discord.Permissions(administrator=True),
        )
        self.cog = cog

    @app_commands.command(name="set", description="غيّر وش يصير للي يرسل في المصيدة.")
    @app_commands.describe(
        action="العقوبة الجديدة",
        duration="مدة الكتم إذا اخترت Timeout — مثل 30m أو 2h أو 7d",
    )
    @app_commands.choices(action=ACTION_CHOICES)
    @utilities.owner_only()
    async def set(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        duration: str | None = None,
    ) -> None:
        timeout_seconds: int | None = None
        if duration is not None:
            timeout_seconds = await _parse_timeout_arg(interaction, duration)
            if timeout_seconds is None:
                return
        row = await self.cog.db.get_honeypot_config()
        if row is None:
            await interaction.response.send_message(
                "❌ المصيدة مو مجهزة. استخدم `/honeypot-setup` أول.", ephemeral=True
            )
            return
        await self.cog.db.set_honeypot_action(action.value, timeout_seconds)
        await self.cog._refresh_cache()
        # Keep the tracking embed's action field truthful.
        await self.cog._refresh_embed_from_db()
        effective_timeout = timeout_seconds or row["timeout_seconds"]
        await interaction.response.send_message(
            f"✅ الإجراء صار: **{_action_label(action.value, effective_timeout)}**.",
            ephemeral=True,
        )
        log.info("Honeypot action set to %s", action.value)


async def setup(bot: commands.Bot) -> None:
    cog = Honeypot(bot)
    await bot.add_cog(cog)
    # Command groups can't be declared as plain @app_commands.command methods
    # on the cog, so they're registered onto the tree here; all logic still
    # lives on the cog. override=True keeps a cog reload from raising
    # CommandAlreadyRegistered.
    bot.tree.add_command(HoneypotSafeRoles(cog), override=True)
    bot.tree.add_command(HoneypotAction(cog), override=True)
