"""System 3 — Support review system.

Triggered when a ticket closes (if enabled). DMs the customer a star-rating view;
clicking a star opens a modal asking about their support experience: was the
problem solved, how was the staff, and any extra comments. Accepted reviews are
posted to the reviews channel. Admins can toggle the whole system on/off with
/reviews; the state persists in the database.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import branding
import config
import utilities

log = logging.getLogger("nightvoid.reviews")

STAR_FULL = "⭐"
REVIEWS_ENABLED_KEY = "reviews_enabled"


class ReviewModal(discord.ui.Modal):
    def __init__(self, cog: "Reviews", stars: int, ticket_id: int) -> None:
        super().__init__(title=f"تقييم تجربتك — {stars} نجوم")
        self.cog = cog
        self.stars = stars
        self.ticket_id = ticket_id

        self.fixed = discord.ui.TextInput(
            label="هل انحلّت مشكلتك؟",
            style=discord.TextStyle.short,
            required=True,
            max_length=200,
            placeholder="إيه، انحلّت / لا، ما انحلّت",
        )
        self.staff = discord.ui.TextInput(
            label="كيف كان تعامل الإدارة؟",
            style=discord.TextStyle.short,
            required=True,
            max_length=200,
            placeholder="ممتاز، سريع، متعاون…",
        )
        self.comment = discord.ui.TextInput(
            label="أي شي تبي تقوله؟ (اختياري)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
            placeholder="اكتب ملاحظاتك هنا…",
        )
        self.add_item(self.fixed) 
        self.add_item(self.staff)
        self.add_item(self.comment)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.submit_review(
            interaction,
            self.stars,
            self.ticket_id,
            str(self.fixed.value).strip(),
            str(self.staff.value).strip(),
            str(self.comment.value).strip(),
        )


class StarButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"nv:review:(?P<stars>[1-5]):(?P<ticket>[0-9]+)",
):
    """One star button, restart-proof AND ticket-aware.

    The ticket id rides inside the custom_id, so after a redeploy the button
    can be reconstructed from the id alone (DynamicItem pattern matching) and
    the one-review-per-ticket rule still knows which ticket it belongs to.
    """

    def __init__(self, stars: int, ticket_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label=str(stars),
                emoji=STAR_FULL,
                style=discord.ButtonStyle.secondary,
                custom_id=f"nv:review:{stars}:{ticket_id}",
            )
        )
        self.stars = stars
        self.ticket_id = ticket_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match) -> "StarButton":
        return cls(int(match["stars"]), int(match["ticket"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog: "Reviews" = interaction.client.get_cog("Reviews")
        if cog is None:
            await interaction.response.send_message(
                "❌ نظام التقييم مو متاح حالياً. حاول مرة ثانية بعدين."
            )
            return
        # Friendly early exit; the real guard is the unique index on insert.
        if await cog.db.ticket_reviewed(self.ticket_id):
            await interaction.response.send_message(
                "✅ قيّمت هذي التذكرة من قبل — التقييم مرة وحدة بس. شكراً لك!"
            )
            return
        await interaction.response.send_modal(
            ReviewModal(cog, self.stars, self.ticket_id)
        )


def review_view(ticket_id: int) -> discord.ui.View:
    """The 5-star DM view for one ticket."""
    view = discord.ui.View(timeout=None)
    for n in range(1, 6):
        view.add_item(StarButton(n, ticket_id))
    return view


class LegacyReviewView(discord.ui.View):
    """Handles star buttons from DMs sent before ticket-linked reviews
    (custom_id `nv:review:<stars>` with no ticket part). Those can't be tied
    to a ticket, so they just get a polite 'expired' reply."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        for n in range(1, 6):
            button = discord.ui.Button(
                label=str(n),
                emoji=STAR_FULL,
                style=discord.ButtonStyle.secondary,
                custom_id=f"nv:review:{n}",
            )
            button.callback = self._expired
            self.add_item(button)

    @staticmethod
    async def _expired(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            "⌛ انتهت صلاحية طلب التقييم هذا. إذا سكّرت تذكرة جديدة بيوصلك طلب جديد."
        )


class Reviews(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db

    async def cog_load(self) -> None:
        # Dynamic items make ticket-linked star buttons survive restarts; the
        # legacy view answers buttons from DMs sent before ticket linking.
        self.bot.add_dynamic_items(StarButton)
        self.bot.add_view(LegacyReviewView())

    async def _is_enabled(self) -> bool:
        # Enabled by default until an admin turns it off.
        return (await self.db.get_setting(REVIEWS_ENABLED_KEY, "1")) == "1"

    # ------------------------------------------------------------------ #
    # Admin toggle
    # ------------------------------------------------------------------ #
    @app_commands.command(name="reviews", description="تشغيل أو إيقاف نظام التقييم (للإدارة).")
    @app_commands.default_permissions(administrator=True)
    @utilities.admin_only()
    @app_commands.describe(state="شغّل أو أوقف نظام التقييم")
    @app_commands.choices(
        state=[
            app_commands.Choice(name="تشغيل", value="on"),
            app_commands.Choice(name="إيقاف", value="off"),
        ]
    )
    async def reviews_toggle(
        self, interaction: discord.Interaction, state: app_commands.Choice[str]
    ) -> None:
        enabled = state.value == "on"
        await self.db.set_setting(REVIEWS_ENABLED_KEY, "1" if enabled else "0")
        msg = "✅ تم **تشغيل** نظام التقييم." if enabled else "🛑 تم **إيقاف** نظام التقييم."
        await interaction.response.send_message(msg, ephemeral=True)
        log.info("Reviews system %s by %s", "enabled" if enabled else "disabled", interaction.user)

    # ------------------------------------------------------------------ #
    # Triggered by ticket closure
    # ------------------------------------------------------------------ #
    async def request_review(self, user_id: int, ticket_id: int) -> None:
        if not await self._is_enabled():
            log.info("Reviews disabled; skipping request for %s", user_id)
            return

        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            log.error("Guild %s unavailable; cannot request review.", config.GUILD_ID)
            return
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                log.warning("Could not fetch member %s for review request.", user_id)
                return

        embed = discord.Embed(
            title="قيّم تجربتك مع الدعم",
            description=(
                "سكّرنا تذكرتك! كيف كانت تجربتك مع الدعم؟\n"
                "اضغط على نجمة من **1** لـ **5** عشان تقيّم تجربتك.\n"
                "*التقييم مرة وحدة لكل تذكرة.*"
            ),
            color=branding.GOLD,
        )
        utilities.brand_footer(embed, "تقييم الدعم")
        try:
            await member.send(embed=embed, view=review_view(ticket_id))
            log.info("Review DM sent to %s (ticket #%s)", member, ticket_id)
        except discord.Forbidden:
            log.info("Could not DM %s for review (DMs closed).", member)
        except discord.HTTPException:
            log.exception("Failed to send review DM to %s", member)

    # ------------------------------------------------------------------ #
    # Modal submission
    # ------------------------------------------------------------------ #
    async def submit_review(
        self,
        interaction: discord.Interaction,
        stars: int,
        ticket_id: int,
        fixed: str,
        staff: str,
        comment: str,
    ) -> None:
        if not await self._is_enabled():
            await interaction.response.send_message(
                "🛑 نظام التقييم متوقّف حالياً. شكراً لك!"
            )
            return

        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            await interaction.response.send_message(
                "❌ السيرفر مو متاح حالياً. حاول مرة ثانية بعدين."
            )
            return

        member = guild.get_member(interaction.user.id) or interaction.user

        # Store a combined text record (schema keeps a single text column).
        combined = (
            f"انحلّت المشكلة: {fixed}\n"
            f"تعامل الإدارة: {staff}\n"
            f"ملاحظات: {comment or '—'}"
        )
        created_at = utilities.utc_now_iso()
        # The partial unique index makes this the atomic once-per-ticket gate —
        # even two modals submitted at the same instant can't both land.
        if not await self.db.add_review(member.id, stars, combined, created_at, ticket_id):
            await interaction.response.send_message(
                "✅ قيّمت هذي التذكرة من قبل — التقييم مرة وحدة بس. شكراً لك!"
            )
            return

        channel = guild.get_channel(config.REVIEWS_CHANNEL_ID)
        if channel is None:
            log.error("Reviews channel %s not found.", config.REVIEWS_CHANNEL_ID)
            await interaction.response.send_message(
                "⚠️ تم حفظ تقييمك بس قناة التقييمات فيها مشكلة إعداد."
            )
            return

        embed = discord.Embed(
            color=branding.GOLD,
            timestamp=utilities.utc_now(),
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.add_field(name="التقييم", value=f"{STAR_FULL * stars} ({stars}/5)", inline=False)
        embed.add_field(name="هل انحلّت مشكلتك؟", value=fixed, inline=False)
        embed.add_field(name="كيف كان تعامل الإدارة؟", value=staff, inline=False)
        if comment:
            embed.add_field(name="ملاحظات", value=comment, inline=False)
        utilities.brand_footer(embed, "تقييم الدعم")

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            log.error("Missing permission to post review to channel %s", channel.id)
            await interaction.response.send_message(
                "⚠️ تم حفظ تقييمك بس ما قدرت أنشره. تم تنبيه الإدارة."
            )
            return

        await interaction.response.send_message(
            f"✅ يعطيك العافية! تم نشر تقييمك ({stars} نجوم)."
        )
        log.info("Review posted by %s (%s stars)", member, stars)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reviews(bot))
