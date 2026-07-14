"""System 4 — Status updates & announcements.

Admin-only /announce posts a rich embed to the announcements channel with an
optional image and an optional @here/@everyone ping.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import branding
import config
import utilities

log = logging.getLogger("nightvoid.announcements")


class Announcements(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="announce", description="انشر إعلان (للإدارة).")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        title="عنوان الإعلان",
        body="نص الإعلان",
        image="رابط صورة (اختياري)",
        ping="مين تبي تمنشن",
    )
    @app_commands.choices(
        ping=[
            app_commands.Choice(name="بدون", value="none"),
            app_commands.Choice(name="@here", value="here"),
            app_commands.Choice(name="@everyone", value="everyone"),
        ]
    )
    @utilities.admin_only("❌ ما عندك صلاحية تنشر إعلانات.")
    async def announce(
        self,
        interaction: discord.Interaction,
        title: str,
        body: str,
        image: str | None = None,
        ping: app_commands.Choice[str] | None = None,
    ) -> None:
        # Discord rejects non-http(s) image URLs with an opaque HTTP 400 —
        # catch it here with a readable error instead.
        if image and not image.startswith(("http://", "https://")):
            await interaction.response.send_message(
                "❌ رابط الصورة لازم يبدأ بـ `http://` أو `https://`.", ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(config.ANNOUNCEMENTS_CHANNEL_ID)
        if channel is None:
            log.error("Announcements channel %s not found.", config.ANNOUNCEMENTS_CHANNEL_ID)
            await interaction.response.send_message(
                "❌ قناة الإعلانات فيها مشكلة إعداد.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=title,
            description=body,
            color=branding.BRAND,
            timestamp=utilities.utc_now(),
        )
        utilities.brand_footer(embed, interaction.user.display_name)
        if image:
            embed.set_image(url=image)

        ping_value = ping.value if ping else "none"
        content = {"here": "@here", "everyone": "@everyone"}.get(ping_value)
        allowed = discord.AllowedMentions(
            everyone=ping_value in ("here", "everyone")
        )

        try:
            await channel.send(content=content, embed=embed, allowed_mentions=allowed)
        except discord.Forbidden:
            log.error("Missing permission to post announcement in %s", channel.id)
            await interaction.response.send_message(
                "❌ ما عندي صلاحية أنشر في قناة الإعلانات.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"✅ تم نشر الإعلان في {channel.mention}.", ephemeral=True
        )
        log.info("Announcement '%s' posted by %s", title, interaction.user)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Announcements(bot))
