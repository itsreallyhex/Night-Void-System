"""Shared branding helpers: the NVS logo (attached to panels) and the web-store
link button. Keeping these in one place avoids duplicating the attachment dance
across cogs.
"""

import os

import discord

import config

LOGO_FILENAME = "nvs-logo.png"
LOGO_ATTACHMENT = f"attachment://{LOGO_FILENAME}"

# --------------------------------------------------------------------------- #
# Semantic embed palette (design tokens).
#
# Cogs reference these by MEANING — never discord.Color.xxx() directly — so the
# whole bot can be re-themed from this one block and every surface stays
# consistent. Values are tuned for Discord's dark theme (vibrant, not muddy).
# --------------------------------------------------------------------------- #
BRAND = discord.Color(0x8B5CF6)    # Night Void violet — panels, store, announcements
SUCCESS = discord.Color(0x22C55E)  # confirmations, completed actions
WARNING = discord.Color(0xF59E0B)  # needs-attention: inactivity, staff requests
DANGER = discord.Color(0xEF4444)   # destructive / error surfaces
INFO = discord.Color(0x38BDF8)     # questions, informational embeds
GOLD = discord.Color(0xFACC15)     # reviews, leaderboard, economy (value/prestige)
NEUTRAL = discord.Color(0x64748B)  # logs, admin diagnostics (low-attention)

FOOTER = "Night Void Store"


def logo_file() -> discord.File | None:
    """Return a fresh discord.File of the logo, or None if it isn't on disk.

    A new File must be built per send (the handle is consumed on upload). Set the
    embed thumbnail/image to LOGO_ATTACHMENT and pass this File in the same message.
    """
    path = config.LOGO_PATH
    if path and os.path.exists(path):
        return discord.File(path, filename=LOGO_FILENAME)
    return None


def store_button() -> discord.ui.Button | None:
    """Link button to the web store, or None if WEB_STORE_URL is unset."""
    if config.WEB_STORE_URL:
        return discord.ui.Button(
            label="زورنا في المتجر",
            emoji="🛒",
            style=discord.ButtonStyle.link,
            url=config.WEB_STORE_URL,
        )
    return None
