"""Drop-in: !dbbp — DM the owner a snapshot of the live database.

The whole store (balances, tickets, reviews, codes, purchases) lives in one
SQLite file on Railway's volume. This command takes a consistent snapshot
(safe while the bot is running) and sends it to the owner's DMs, so a copy
always exists outside Railway. Owner-only and invisible to everyone else —
the file IS the entire economy.

To restore: upload the file back to /data/store.db (Railway volume) and
restart the bot.
"""

import logging
import os
import tempfile
from datetime import datetime, timezone

import discord
from discord.ext import commands

log = logging.getLogger("nightvoid.extras.dbbp")

# Discord rejects bot uploads over 10 MB — warn before wasting the attempt.
UPLOAD_LIMIT = 10 * 1024 * 1024


class DbBackup(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db

    async def cog_check(self, ctx: commands.Context) -> bool:
        # Pinned owner only — this file contains the entire economy.
        return await self.bot.is_owner(ctx.author)

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CheckFailure):
            return  # invisible to non-owners
        # Anything else means NO backup was delivered — say so, never fail
        # silently, or the owner walks away believing a copy exists.
        log.exception("dbbp command error", exc_info=error)
        await self._dm(
            ctx,
            "🚨 فشل أخذ النسخة الاحتياطية — ما تم إرسال أي ملف. "
            "شوف سجلات Railway للتفاصيل وحاول مرة ثانية.",
        )

    @commands.command(name="dbbp", aliases=["dbbackup", "backup"])
    async def dbbp(self, ctx: commands.Context) -> None:
        """Snapshot the database and DM it to the owner."""
        # Same privacy pattern as the Owner cog: the invoking message
        # disappears and the backup goes to DMs, never into a channel.
        if ctx.guild is not None:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
        filename = f"store-backup-{stamp}.db"

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, filename)
            await self.db.backup_to(path)
            size = os.path.getsize(path)

            if size > UPLOAD_LIMIT:
                await self._dm(
                    ctx,
                    f"⚠️ النسخة الاحتياطية حجمها {size / 1024 / 1024:.1f} MB — "
                    f"أكبر من حد رفع ديسكورد. خذها من Railway مباشرة "
                    f"(`/data/store.db`).",
                )
                return

            try:
                await ctx.author.send(
                    f"🗄️ نسخة احتياطية من قاعدة البيانات — "
                    f"{size / 1024:.0f} KB\n"
                    f"للاسترجاع: ارفعها مكان `/data/store.db` وأعد تشغيل البوت.",
                    file=discord.File(path, filename=filename),
                )
            except discord.Forbidden:
                await self._dm(ctx, None)  # DMs closed — fall through to notice
                return
            except discord.HTTPException as exc:
                # Discord refused the upload anyway (its real limit varies,
                # error 40005 = entity too large) — report instead of vanishing.
                log.warning("dbbp upload rejected (%s bytes): %s", size, exc)
                await self._dm(
                    ctx,
                    f"🚨 ديسكورد رفض رفع الملف ({size / 1024 / 1024:.1f} MB) — "
                    f"ما تم إرسال نسخة. خذها من Railway مباشرة (`/data/store.db`).",
                )
                return

        log.info("Database backup (%s bytes) DMed to owner %s", size, ctx.author.id)

    async def _dm(self, ctx: commands.Context, content: str | None) -> None:
        """DM `content`, or if DMs are closed leave a self-destructing notice."""
        if content is not None:
            try:
                await ctx.author.send(content)
                return
            except discord.Forbidden:
                pass
        try:
            await ctx.send(
                "⚠️ ما أقدر أرسل لك خاص — فعّل **الرسائل الخاصة** وأعد المحاولة.",
                delete_after=15,
            )
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DbBackup(bot))
