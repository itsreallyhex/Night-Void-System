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

# Shared with the Owner cog (!output): whether replies go private (DM) or into
# the channel. Kept as a literal here so this drop-in stays import-independent.
OUTPUT_MODE_KEY = "owner_output_mode"


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
        await self._deliver(
            ctx,
            "🚨 فشل أخذ النسخة الاحتياطية — ما تم إرسال أي ملف. "
            "شوف سجلات Railway للتفاصيل وحاول مرة ثانية.",
        )

    @commands.command(name="dbbp", aliases=["dbbackup", "backup"])
    async def dbbp(self, ctx: commands.Context) -> None:
        """Snapshot the database and deliver it (DM or channel, per !output)."""
        private = await self._private_mode()
        # In private mode the invoking message disappears; in channel mode it's
        # left, since the backup is going into that channel on purpose anyway.
        if private and ctx.guild is not None:
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
                await self._deliver(
                    ctx,
                    f"⚠️ النسخة الاحتياطية حجمها {size / 1024 / 1024:.1f} MB — "
                    f"أكبر من حد رفع ديسكورد. خذها من Railway مباشرة "
                    f"(`/data/store.db`).",
                )
                return

            target = ctx.author if private else ctx.channel
            try:
                await target.send(
                    f"🗄️ نسخة احتياطية من قاعدة البيانات — "
                    f"{size / 1024:.0f} KB\n"
                    f"للاسترجاع: ارفعها مكان `/data/store.db` وأعد تشغيل البوت.",
                    file=discord.File(path, filename=filename),
                )
            except discord.Forbidden:
                # DMs closed (private) or no send perms (channel) — leave a notice.
                await self._deliver(ctx, None)
                return
            except discord.HTTPException as exc:
                # Discord refused the upload anyway (its real limit varies,
                # error 40005 = entity too large) — report instead of vanishing.
                log.warning("dbbp upload rejected (%s bytes): %s", size, exc)
                await self._deliver(
                    ctx,
                    f"🚨 ديسكورد رفض رفع الملف ({size / 1024 / 1024:.1f} MB) — "
                    f"ما تم إرسال نسخة. خذها من Railway مباشرة (`/data/store.db`).",
                )
                return

        log.info("Database backup (%s bytes) delivered to owner %s", size, ctx.author.id)

    async def _private_mode(self) -> bool:
        """Whether replies should go to the owner's DMs (True) or the channel
        (False). Set with the Owner cog's !output command."""
        return (await self.db.get_setting(OUTPUT_MODE_KEY, "private")) != "channel"

    async def _deliver(self, ctx: commands.Context, content: str | None) -> None:
        """Send `content` to the DM or the channel per the current mode; if that
        target refuses (DMs closed / no send perms), leave a self-destructing
        in-channel notice so a failure is never silent."""
        private = await self._private_mode()
        target = ctx.author if private else ctx.channel
        if content is not None:
            try:
                await target.send(content)
                return
            except discord.Forbidden:
                pass
        try:
            await ctx.send(
                "⚠️ ما قدرت أوصّل الرد — فعّل **الرسائل الخاصة** أو استخدم "
                "`!output here` عشان تجيك بالقناة.",
                delete_after=15,
            )
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DbBackup(bot))
