"""Drop-in: !restart (aliases: rest, reboot) — restart the bot process.

Owner-only. Flushes the database first (checkpointing SQLite's WAL so nothing
is lost), then re-executes the Python process in place with os.execv. The bot
comes back on a fresh connection without depending on the host's restart
policy — on Railway the container stays up and the bot just reconnects a few
seconds later. Use it after changing config that only loads at startup, or to
clear a stuck state without opening the Railway dashboard.
"""

import logging
import os
import sys

import discord
from discord.ext import commands

log = logging.getLogger("nightvoid.extras.restart")


class Restart(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        # Pinned owner only — this takes the whole bot down for a few seconds.
        return await self.bot.is_owner(ctx.author)

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CheckFailure):
            return  # invisible to non-owners
        # We only reach here if the restart failed BEFORE execv replaced us —
        # execv never returns, so a message here means the bot is still up.
        log.exception("restart command error", exc_info=error)
        try:
            await ctx.send("⚠️ ما قدرت أعيد التشغيل — شوف سجلات Railway.")
        except discord.HTTPException:
            pass

    @commands.command(name="restart", aliases=["rest", "reboot"])
    async def restart(self, ctx: commands.Context) -> None:
        """Cleanly restart the bot process in place."""
        log.warning("Restart requested by owner %s", ctx.author.id)
        try:
            await ctx.send("🔄 جاري إعادة التشغيل... أرجع خلال ثواني.")
        except discord.HTTPException:
            pass

        # Flush the database (checkpoint the WAL) before we drop the process.
        # execv discards open handles, so a clean close here avoids leaving the
        # WAL to be replayed on next boot.
        try:
            await self.bot.db.close()
        except Exception:
            log.exception("Could not close the database cleanly before restart; continuing.")

        # Replace this process image with a fresh interpreter running the same
        # entry point (python bot.py …). The Discord socket is torn down by the
        # OS; the new process reconnects from scratch.
        log.warning("Re-executing: %s %s", sys.executable, sys.argv)
        os.execv(sys.executable, [sys.executable, *sys.argv])


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Restart(bot))
