"""Owner-only credit administration.

Prefix commands restricted to the bot's application owner (commands.is_owner).
They never appear in the public slash list. To keep them fully private, every
command deletes the message you typed and DMs the result back to you, so no one
else in the channel sees the command or its output.
"""

import logging

import discord
from discord.ext import commands

import branding
import config
from amounts import AmountConverter

log = logging.getLogger("nightvoid.owner")


class Owner(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db

    async def cog_check(self, ctx: commands.Context) -> bool:
        """Gate every command in this cog behind the application owner."""
        return await self.bot.is_owner(ctx.author)

    async def _private(
        self, ctx: commands.Context, content: str | None = None, *, embed: discord.Embed | None = None
    ) -> None:
        """Delete the invoking message (in guilds) and DM the response to the owner."""
        if ctx.guild is not None:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass  # missing Manage Messages or already gone — DM still goes out
        try:
            await ctx.author.send(content=content, embed=embed)
        except discord.Forbidden:
            # Owner's DMs are closed; tell them in-channel, then self-destruct.
            try:
                await ctx.send(
                    "⚠️ ما أقدر أرسل لك خاص — فعّل **الرسائل الخاصة** من إعدادات "
                    "الخصوصية عشان ردود المالك تبقى خاصة.",
                    delete_after=15,
                )
            except discord.HTTPException:
                pass

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        # Stay silent for non-owners so the commands remain invisible.
        if isinstance(error, commands.CheckFailure):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await self._private(ctx, f"⚠️ ناقص معلومة: `{error.param.name}`. شوف `!nvhelp`.")
            return
        if isinstance(error, (commands.BadArgument, commands.MemberNotFound)):
            await self._private(ctx, "⚠️ ما قدرت أفهم — تأكد من منشن العضو والمبلغ.")
            return
        log.exception("Owner command error", exc_info=error)
        await self._private(ctx, "⚠️ صار فيه خطأ وأنا أشغّل الأمر.")

    # ------------------------------------------------------------------ #
    # Mutations
    # ------------------------------------------------------------------ #
    @commands.command(name="give", aliases=["addcredits", "givecredits"])
    async def give(self, ctx: commands.Context, member: discord.Member, amount: AmountConverter) -> None:
        """Give credits to a member. Amount accepts shorthand like 1k / 2.5m."""
        if amount <= 0:
            await self._private(ctx, "⚠️ المبلغ لازم يكون رقم موجب.")
            return
        await self.db.add_credits(member.id, amount)
        new_balance = await self.db.get_balance(member.id)
        await self._private(
            ctx,
            f"✅ عطيت **{amount:,}** كريدت لـ {member} (`{member.id}`). "
            f"الرصيد الجديد: **{new_balance:,}**.",
        )
        log.info("Owner %s gave %s credits to %s", ctx.author, amount, member)

    @commands.command(name="take", aliases=["removecredits", "takecredits"])
    async def take(self, ctx: commands.Context, member: discord.Member, amount: AmountConverter) -> None:
        """Remove credits from a member (balance clamps at 0). Accepts 1k / 2.5m."""
        if amount <= 0:
            await self._private(ctx, "⚠️ المبلغ لازم يكون رقم موجب.")
            return
        new_balance = await self.db.remove_credits(member.id, amount)
        await self._private(
            ctx,
            f"✅ خصمت لين **{amount:,}** كريدت من {member} (`{member.id}`). "
            f"الرصيد الجديد: **{new_balance:,}**.",
        )
        log.info("Owner %s removed %s credits from %s", ctx.author, amount, member)

    @commands.command(name="setcred", aliases=["setcredits", "set"])
    async def setcredits(self, ctx: commands.Context, member: discord.Member, amount: AmountConverter) -> None:
        """Set a member's balance to an exact amount. Accepts 1k / 2.5m."""
        if amount < 0:
            await self._private(ctx, "⚠️ المبلغ ما يصير بالسالب.")
            return
        await self.db.set_credits(member.id, amount)
        await self._private(
            ctx, f"✅ حطيت رصيد {member} (`{member.id}`) على **{amount:,}** كريدت."
        )
        log.info("Owner %s set %s balance to %s", ctx.author, member, amount)

    # ------------------------------------------------------------------ #
    # Lookups
    # ------------------------------------------------------------------ #
    @commands.command(name="credits", aliases=["checkcredits", "bal", "checkbal"])
    async def credits_(self, ctx: commands.Context, member: discord.Member) -> None:
        """Check a member's balance and their recent purchases."""
        balance = await self.db.get_balance(member.id)
        purchases = await self.db.get_purchases(member.id)

        embed = discord.Embed(
            title=f"الكريدت — {member.display_name}",
            color=branding.NEUTRAL,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="الرصيد", value=f"**{balance:,}** كريدت", inline=False)
        embed.add_field(
            name="آخر المشتريات",
            value=self._format_purchases(purchases),
            inline=False,
        )
        embed.set_footer(text=f"آيدي العضو: {member.id}")
        await self._private(ctx, embed=embed)

    @commands.command(name="purchases", aliases=["bought", "history"])
    async def purchases(self, ctx: commands.Context, member: discord.Member) -> None:
        """List everything a member has bought (roles + services)."""
        rows = await self.db.get_purchases(member.id)
        embed = discord.Embed(
            title=f"سجل المشتريات — {member.display_name}",
            description=self._format_purchases(rows),
            color=branding.NEUTRAL,
        )
        embed.set_footer(text=f"آيدي العضو: {member.id}")
        await self._private(ctx, embed=embed)

    # ------------------------------------------------------------------ #
    # Maintenance
    # ------------------------------------------------------------------ #
    @commands.command(name="tickreset", aliases=["ticketreset", "resettickets"])
    async def tickreset(self, ctx: commands.Context) -> None:
        """Reset ticket numbering to #1 (deletes closed-ticket history rows)."""
        deleted = await self.db.reset_tickets()
        if deleted is None:
            await self._private(
                ctx, "⚠️ فيه تذاكر مفتوحة حالياً — سكّرها كلها قبل تصفير العداد."
            )
            return
        await self._private(
            ctx,
            f"✅ تم تصفير عداد التذاكر — التذكرة الجاية تبدأ من **#1**.\n"
            f"(انحذف **{deleted}** سجل تذكرة قديم؛ التقييمات والسجلات في قناة اللوق باقية.)",
        )
        log.info("Owner %s reset ticket numbering (%s rows deleted).", ctx.author, deleted)

    # ------------------------------------------------------------------ #
    # Database overview
    # ------------------------------------------------------------------ #
    @commands.command(name="db", aliases=["database", "dbinfo", "dbstats"])
    async def db_overview(self, ctx: commands.Context) -> None:
        """Full snapshot of the database (owner only)."""
        o = await self.db.overview(exclude=config.OWNER_USER_ID)

        embed = discord.Embed(
            title="نظرة عامة على قاعدة البيانات",
            color=branding.NEUTRAL,
        )
        tables = "\n".join(f"`{t}` — **{n:,}**" for t, n in o["tables"].items())
        embed.add_field(name="الجداول (عدد الصفوف)", value=tables or "—", inline=False)

        c = o["codes"]
        embed.add_field(
            name="الأكواد",
            value=f"الإجمالي **{c['total']:,}** • مستخدمة **{c['used']:,}** • متبقية **{c['left']:,}**",
            inline=False,
        )
        cr = o["credits"]
        embed.add_field(
            name="الكريدت",
            value=f"عدد الحاملين **{cr['holders']:,}** • المتداول **{cr['total']:,}**",
            inline=False,
        )
        tk = o["tickets"]
        embed.add_field(
            name="التذاكر",
            value=f"مفتوحة **{tk['open']:,}** • مسكّرة **{tk['closed']:,}**",
            inline=False,
        )
        rv = o["reviews"]
        embed.add_field(
            name="التقييمات",
            value=f"العدد **{rv['count']:,}** • المعدّل **{rv['avg']}/5**",
            inline=False,
        )
        pu = o["purchases"]
        embed.add_field(
            name="المشتريات",
            value=f"العدد **{pu['count']:,}** • إجمالي المصروف **{pu['spent']:,}**",
            inline=False,
        )
        reviews_on = o["settings"].get("reviews_enabled", "1") == "1"
        embed.add_field(
            name="الإعدادات",
            value=f"نظام التقييم: {'مشغّل ✅' if reviews_on else 'متوقّف 🛑'}",
            inline=False,
        )
        embed.set_footer(text=f"الملف: {config.DATABASE_PATH}")
        await self._private(ctx, embed=embed)

    @staticmethod
    def _format_purchases(rows) -> str:
        if not rows:
            return "*ما فيه مشتريات.*"
        lines = []
        for r in rows:
            date = r["created_at"][:10]
            kind = "🎭" if r["item_type"] == "role" else "🛎️"
            lines.append(f"{kind} **{r['item_name']}** — {r['cost']:,} credits ({date})")
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Help
    # ------------------------------------------------------------------ #
    @commands.command(name="nvhelp")
    async def nvhelp(self, ctx: commands.Context) -> None:
        """List the owner commands."""
        embed = discord.Embed(
            title="أوامر المالك",
            description="كل الردود تجيك خاص ورسالتك تنحذف، عشان ما يشوفها غيرك.",
            color=branding.NEUTRAL,
        )
        embed.add_field(name="!give @user <مبلغ>", value="إضافة كريدت (يقبل 1k / 2.5m)", inline=False)
        embed.add_field(name="!take @user <مبلغ>", value="خصم كريدت (أقل شي 0، يقبل 1k)", inline=False)
        embed.add_field(name="!setcred @user <مبلغ>", value="تحديد الرصيد بالضبط (يقبل 1k)", inline=False)
        embed.add_field(name="!credits @user", value="عرض الرصيد + آخر المشتريات", inline=False)
        embed.add_field(name="!purchases @user", value="كل سجل المشتريات", inline=False)
        embed.add_field(name="!db", value="نظرة كاملة على قاعدة البيانات", inline=False)
        embed.add_field(name="!dbbp", value="نسخة احتياطية من قاعدة البيانات (توصلك خاص)", inline=False)
        embed.add_field(name="!tickreset", value="تصفير عداد التذاكر (يبدأ من #1)", inline=False)
        embed.add_field(name="!restart", value="إعادة تشغيل البوت (أو `!rest`)", inline=False)
        # !dmall is disabled for now — its cog is parked at cogs/extras/_dmall.py
        # (the leading underscore keeps the loader from loading it). To bring it
        # back, rename the file to dmall.py and un-comment this field.
        # embed.add_field(
        #     name="!dmall <رسالة>",
        #     value="رسالة خاصة لكل الأعضاء (مع تأكيد، وإيقاف بـ stop) — هذا الأمر يشتغل في القناة نفسها وما ينحذف",
        #     inline=False,
        # )
        await self._private(ctx, embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Owner(bot))
