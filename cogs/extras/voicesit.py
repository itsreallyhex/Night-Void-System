"""Drop-in: park the bot silently in a voice channel.

The bot joins a voice channel, **self-mutes and self-deafens**, and just sits
there — it never listens and never plays audio (no FFmpeg involved). Handy for
a permanent "presence" in a lounge/AFK channel.

It does this the lightweight way: a raw gateway voice-state update
(`Guild.change_voice_state`) rather than a full voice client. The bot shows up
in the channel muted + deafened, but no encrypted voice session is opened — so
this needs **no PyNaCl, no davey, and no FFmpeg**. Those are only required to
actually send/receive audio, which this feature never does.

The target channel is stored in the settings table, so the bot rejoins it on
its own after a restart or a gateway reconnect ("just stay there"). Admin-only.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import branding
import config
import utilities

log = logging.getLogger("nightvoid.extras.voicesit")

# Settings key holding the channel the bot should sit in ("0" / unset = none).
SIT_CHANNEL_KEY = "vc_sit_channel"


class VoiceSit(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db

    # ------------------------------------------------------------------ #
    # Rejoin after a restart / reconnect
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # on_ready also fires on gateway RESUME; only act when we're not
        # already sitting where we're supposed to.
        raw = await self.db.get_setting(SIT_CHANNEL_KEY, "0")
        channel_id = int(raw) if raw and raw.isdigit() else 0
        if channel_id == 0:
            return

        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            return
        me_voice = guild.me.voice
        if me_voice is not None and me_voice.channel and me_voice.channel.id == channel_id:
            return  # already parked in the right place

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            log.warning("Saved VC-sit channel %s is gone; clearing.", channel_id)
            await self.db.set_setting(SIT_CHANNEL_KEY, "0")
            return
        try:
            await self._park(guild, channel)
            log.info("Rejoined VC-sit channel %s after startup.", channel_id)
        except Exception:
            log.exception("Failed to rejoin VC-sit channel %s on startup.", channel_id)

    async def _park(self, guild: discord.Guild, channel: discord.VoiceChannel) -> None:
        """Appear in `channel` muted + deafened via a gateway voice-state update.

        No voice client is created, so no audio stack (PyNaCl/davey/FFmpeg) is
        touched. Passing a new channel while already connected just moves us."""
        await guild.change_voice_state(channel=channel, self_mute=True, self_deaf=True)

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="vc-join",
        description="خلّي البوت يدخل روم صوتي ويدفن نفسه ويقعد ساكت (للإدارة).",
    )
    @app_commands.describe(channel="الروم الصوتي (اختياري — الافتراضي رومك الحالي)")
    @app_commands.default_permissions(administrator=True)
    @utilities.admin_only("❌ هذا الأمر للإدارة فقط.")
    async def vc_join(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | None = None,
    ) -> None:
        # Default to the caller's current voice channel.
        if channel is None:
            voice = getattr(interaction.user, "voice", None)
            channel = voice.channel if voice else None
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message(
                "❌ حدّد روم صوتي أو ادخل روم صوتي أول.", ephemeral=True
            )
            return

        # Permission preflight: refusing here beats a silent no-op.
        perms = channel.permissions_for(interaction.guild.me)
        if not perms.connect:
            await interaction.response.send_message(
                f"❌ ما عندي صلاحية **Connect** في {channel.mention}.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await self._park(interaction.guild, channel)
        except Exception:
            log.exception("Failed to enter voice channel %s", channel.id)
            await interaction.followup.send(
                "❌ ما قدرت أدخل الروم الصوتي. حاول مرة ثانية.", ephemeral=True
            )
            return

        await self.db.set_setting(SIT_CHANNEL_KEY, str(channel.id))
        await interaction.followup.send(
            f"✅ دخلت {channel.mention}.",
            ephemeral=True,
        )
        log.info("VC-sit joined %s by %s", channel.id, interaction.user)

    @app_commands.command(
        name="vc-leave", description="طلّع البوت من الروم الصوتي (للإدارة).",
    )
    @app_commands.default_permissions(administrator=True)
    @utilities.admin_only("❌ هذا الأمر للإدارة فقط.")
    async def vc_leave(self, interaction: discord.Interaction) -> None:
        # Clear the saved channel first, so on_ready won't rejoin after this.
        await self.db.set_setting(SIT_CHANNEL_KEY, "0")
        me_voice = interaction.guild.me.voice
        if me_voice is None or me_voice.channel is None:
            await interaction.response.send_message(
                "ℹ️ أنا أصلاً مو داخل روم صوتي.", ephemeral=True
            )
            return
        try:
            await interaction.guild.change_voice_state(channel=None)
        except Exception:
            log.exception("Failed to leave voice.")
        await interaction.response.send_message("👋 طلعت من الروم الصوتي.", ephemeral=True)
        log.info("VC-sit left voice by %s", interaction.user)

    @app_commands.command(
        name="vc-status", description="وين البوت قاعد بالروم الصوتي (للإدارة).",
    )
    @app_commands.default_permissions(administrator=True)
    @utilities.admin_only("❌ هذا الأمر للإدارة فقط.")
    async def vc_status(self, interaction: discord.Interaction) -> None:
        me_voice = interaction.guild.me.voice
        embed = discord.Embed(title="حالة الروم الصوتي", color=branding.NEUTRAL)
        if me_voice is not None and me_voice.channel is not None:
            embed.description = f"🎧 قاعد في {me_voice.channel.mention}."
        else:
            embed.description = "⚪ مو داخل أي روم صوتي حالياً."
        utilities.brand_footer(embed, "الروم الصوتي")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceSit(bot))
