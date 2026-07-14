"""System 7 — Minor services shop.

/request-service opens a modal. If the user's balance meets MINOR_SERVICES_COST,
the cost is deducted atomically and the request is posted to the staff channel as
an embed. The user receives an ephemeral confirmation.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import branding
import config
import utilities

log = logging.getLogger("nightvoid.services")


class ServiceModal(discord.ui.Modal, title="اطلب خدمة"):
    def __init__(self, cog: "Services") -> None:
        super().__init__()
        self.cog = cog

    service = discord.ui.TextInput(
        label="وش الخدمة اللي تبيها؟",
        style=discord.TextStyle.short,
        required=True,
        max_length=100,
        placeholder="مثلاً: إعلان مخصص",
    )
    details = discord.ui.TextInput(
        label="التفاصيل",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
        placeholder="اشرح بالضبط وش تبي…",
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.process_request(
            interaction, str(self.service.value).strip(), str(self.details.value).strip()
        )


class Services(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db

    @app_commands.command(
        name="request-service", description="اصرف كريدت عشان تطلب خدمة بسيطة."
    )
    async def request_service(self, interaction: discord.Interaction) -> None:
        balance = await self.db.get_balance(interaction.user.id)
        if balance < config.MINOR_SERVICES_COST:
            await interaction.response.send_message(
                f"❌ هذي تحتاج **{config.MINOR_SERVICES_COST:,}** كريدت. "
                f"عندك {balance:,}.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(ServiceModal(self))

    async def process_request(
        self, interaction: discord.Interaction, service: str, details: str
    ) -> None:
        cost = config.MINOR_SERVICES_COST
        # Re-check + deduct atomically (balance may have changed since the modal opened).
        if not await self.db.deduct_credits(interaction.user.id, cost):
            balance = await self.db.get_balance(interaction.user.id)
            await interaction.response.send_message(
                f"❌ كريدتك ما يكفي. تحتاج {cost:,} وعندك {balance:,}.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(config.STAFF_SERVICES_CHANNEL_ID)
        if channel is None:
            await self.db.add_credits(interaction.user.id, cost)  # refund
            log.error("Staff services channel %s not found; refunded.", config.STAFF_SERVICES_CHANNEL_ID)
            await interaction.response.send_message(
                "⚠️ قناة الخدمات فيها مشكلة إعداد. رجّعنا لك كريدتك.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="طلب خدمة جديد",
            color=branding.WARNING,
            timestamp=utilities.utc_now(),
        )
        embed.add_field(name="طلبها", value=interaction.user.mention, inline=True)
        embed.add_field(name="آيدي العضو", value=str(interaction.user.id), inline=True)
        embed.add_field(name="الخدمة", value=service, inline=False)
        embed.add_field(name="التفاصيل", value=details, inline=False)
        embed.add_field(name="الكريدت المصروف", value=f"{cost:,}", inline=False)
        utilities.brand_footer(embed, "الخدمات")

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            await self.db.add_credits(interaction.user.id, cost)  # refund
            log.error("Missing permission to post to staff services channel %s; refunded.", channel.id)
            await interaction.response.send_message(
                "⚠️ ما قدرت أوصل لقناة الإدارة. رجّعنا لك كريدتك.",
                ephemeral=True,
            )
            return

        await self.db.add_purchase(
            interaction.user.id, "service", service, cost, utilities.utc_now_iso()
        )
        await interaction.response.send_message(
            f"✅ تم إرسال طلبك وخصمنا **{cost:,}** كريدت. "
            "الإدارة بتتواصل معك قريب.",
            ephemeral=True,
        )
        log.info("Service request from %s: %s (%s credits)", interaction.user, service, cost)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Services(bot))
    