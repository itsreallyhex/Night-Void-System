"""Drop-in: keeps the `users` table fresh so the database reads with names.

No commands — pure background plumbing. Three feeds:
  1. Startup backfill: every current guild member, so old rows resolve too.
  2. on_member_join: newcomers are named before their first row exists.
  3. on_message (throttled): catches renames from active members for free.

The v_* views in database.py join against this table, so opening a backup
in DB Browser shows `فايز — 8,581` instead of a raw Discord id.
"""

import logging
import time

import discord
from discord.ext import commands

import config
import utilities

log = logging.getLogger("nightvoid.extras.usernames")

REFRESH_SECONDS = 12 * 3600  # per-user throttle for the on_message feed


def display(member: discord.abc.User) -> str:
    """Most recognizable single string for a member: display name first,
    unique handle in parentheses when they differ."""
    shown = member.global_name or member.name
    return shown if shown == member.name else f"{shown} ({member.name})"


class Usernames(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db
        self._last_seen: dict[int, float] = {}

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Runs on every (re)connect; the upsert is idempotent so that's fine.
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            log.warning("Guild %s unavailable; username backfill skipped.", config.GUILD_ID)
            return
        try:
            await guild.chunk()
        except discord.HTTPException:
            log.exception("Guild chunk failed; backfilling from cache only.")
        try:
            rows = [(m.id, display(m)) for m in guild.members if not m.bot]
            await self.db.upsert_users(rows, utilities.utc_now_iso())
            log.info("Username backfill: %s member(s) recorded.", len(rows))
        except Exception:
            log.exception("Username backfill failed; names refresh via activity instead.")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        try:
            await self.db.upsert_users([(member.id, display(member))], utilities.utc_now_iso())
        except Exception:
            log.exception("Failed to record username for joining member %s.", member.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        author = message.author
        if author.bot or message.guild is None:
            return
        now = time.monotonic()
        if now - self._last_seen.get(author.id, 0.0) < REFRESH_SECONDS:
            return
        self._last_seen[author.id] = now
        try:
            await self.db.upsert_users([(author.id, display(author))], utilities.utc_now_iso())
        except Exception:
            log.exception("Failed to refresh username for %s.", author.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Usernames(bot))
