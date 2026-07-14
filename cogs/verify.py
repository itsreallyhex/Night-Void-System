"""Verification gate.

New members are held behind a gate: on join they get an "unverify" hold role,
and every channel (text, voice, category) carries a deny-View overwrite for that
role — except the gate channel — so a held member sees only the gate. A panel
there carries one button; clicking it removes the hold role (revealing the
server) and grants the "verified" role. The button is persistent (fixed
custom_id, timeout=None) so it keeps working across restarts.

Setup wires the whole server for you: pick the two roles and the bot applies the
hold-role overwrites to every channel itself — no manual per-channel editing.

Single-guild by design (like the rest of the bot): exactly one configuration,
no guild_id anywhere. Privacy by design: no per-member verification record.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import branding
import utilities

log = logging.getLogger("nightvoid.verify")

# Fixed custom_id so the persistent view re-binds after a restart.
VERIFY_BUTTON_ID = "nv:verify:go"


class VerifyView(discord.ui.View):
    """Persistent one-button gate panel."""

    def __init__(self, cog: "Verify") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="تحقّق",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id=VERIFY_BUTTON_ID,
    )
    async def verify(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.cog.handle_verify(interaction)


class Verify(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db
        # Cache the fields the join-listener and button-path need, so neither
        # touches the DB on the hot path. Refreshed after any config mutation.
        self._verify_role_id: int | None = None
        self._unverify_role_id: int | None = None
        self._channel_id: int | None = None
        self._enabled: bool = False

    async def cog_load(self) -> None:
        await self._refresh_cache()
        # Re-register the persistent view so the panel button survives a restart.
        self.bot.add_view(VerifyView(self))

    async def _refresh_cache(self) -> None:
        row = await self.db.get_verify_config()
        self._verify_role_id = row["role_id"] if row else None
        self._unverify_role_id = (
            row["unverify_role_id"] if row and row["unverify_role_id"] else None
        )
        self._channel_id = row["channel_id"] if row else None
        self._enabled = bool(row["enabled"]) if row else False

    # ------------------------------------------------------------------ #
    # Panel
    # ------------------------------------------------------------------ #
    def _build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="🛡️ تحقّق عشان تدخل",
            description=(
                "عشان تشوف باقي السيرفر، اضغط زر **تحقّق** تحت.\n\n"
                "خطوة وحدة تتأكد إنك مو بوت — وبعدها ينفتح لك السيرفر كامل."
            ),
            color=branding.BRAND,
        )
        embed.set_footer(text=f"{branding.FOOTER} • التحقق")
        return embed

    # ------------------------------------------------------------------ #
    # Hold new members on join
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not self._enabled or self._unverify_role_id is None or member.bot:
            return
        role = member.guild.get_role(self._unverify_role_id)
        if role is None:
            log.error("Verify: hold role %s no longer exists.", self._unverify_role_id)
            return
        await utilities.try_add_role(member, role, "Verify gate: hold until verified")

    # ------------------------------------------------------------------ #
    # Keep new channels hidden from held members
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_guild_channel_create(
        self, channel: discord.abc.GuildChannel
    ) -> None:
        """A channel/category created after setup gets the same deny-View
        overwrite for the hold role, so the lockdown never drifts as the server
        grows. (Synced channels under an already-locked category inherit it
        anyway; this covers new categories and desynced channels too.)"""
        if not self._enabled or self._unverify_role_id is None:
            return
        if channel.id == self._channel_id:  # never hide the gate itself
            return
        role = channel.guild.get_role(self._unverify_role_id)
        if role is None:
            return
        try:
            await channel.set_permissions(
                role, view_channel=False, reason="Verify gate: auto-hide new channel"
            )
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Verify: could not auto-hide new channel %s.", channel.id)

    # ------------------------------------------------------------------ #
    # Button handler
    # ------------------------------------------------------------------ #
    async def handle_verify(self, interaction: discord.Interaction) -> None:
        if not self._enabled or self._verify_role_id is None:
            await interaction.response.send_message(
                "❌ التحقق متوقف حالياً. رجّع بعد شوي.", ephemeral=True
            )
            return
        member = interaction.user
        if not isinstance(member, discord.Member) or interaction.guild is None:
            await interaction.response.send_message(
                "❌ لازم تسوي هذا من داخل السيرفر.", ephemeral=True
            )
            return
        verify_role = interaction.guild.get_role(self._verify_role_id)
        if verify_role is None:
            log.error("Verify: verified role %s no longer exists.", self._verify_role_id)
            await interaction.response.send_message(
                "❌ صار خطأ في الإعداد — بلّغ الإدارة.", ephemeral=True
            )
            return
        if verify_role in member.roles:
            await interaction.response.send_message(
                "✅ أنت متحقق من قبل — عندك وصول كامل.", ephemeral=True
            )
            return

        # Removing the hold role is what actually reveals the server (a role
        # deny beats the @everyone allow), so a failure here must be surfaced —
        # not swallowed — or the member ends up "verified" but still locked out.
        unverify_role = (
            interaction.guild.get_role(self._unverify_role_id)
            if self._unverify_role_id
            else None
        )
        if unverify_role is not None and unverify_role in member.roles:
            try:
                await member.remove_roles(unverify_role, reason="Verified")
            except (discord.Forbidden, discord.HTTPException):
                log.error(
                    "Verify: could not remove hold role %s from %s.",
                    unverify_role.id, member.id,
                )
                await interaction.response.send_message(
                    "❌ ما قدرت أكمّل التحقق — بلّغ الإدارة (غالباً ترتيب الرتب).",
                    ephemeral=True,
                )
                return

        # Grant the badge role. Best-effort: the reveal already happened above.
        await utilities.try_add_role(member, verify_role, "Verification gate")
        await interaction.response.send_message(
            "✅ تم التحقق! انفتح لك السيرفر كامل. حيّاك 🌙", ephemeral=True
        )
        log.info("Verified member %s", member.id)

    # ------------------------------------------------------------------ #
    # Server lockdown
    # ------------------------------------------------------------------ #
    async def _apply_lockdown(
        self,
        guild: discord.Guild,
        unverify_role: discord.Role,
        gate_channel: discord.abc.GuildChannel,
    ) -> tuple[int, int]:
        """Hide every channel from the hold role except the gate channel.

        Overwrites are set on each channel directly (not left to category
        inheritance) so non-synced channels are covered too. Returns
        (hidden, failed)."""
        hidden = failed = 0
        for channel in guild.channels:
            try:
                if channel.id == gate_channel.id:
                    # Held members must see the gate AND be able to read the
                    # panel (posted before they joined -> needs history), even
                    # if the server denied @everyone read history elsewhere.
                    await channel.set_permissions(
                        unverify_role,
                        view_channel=True,
                        read_message_history=True,
                        reason="Verify gate",
                    )
                    continue  # the gate is shown, not hidden — don't count it
                await channel.set_permissions(
                    unverify_role, view_channel=False, reason="Verify gate lockdown"
                )
                hidden += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
                log.warning("Verify: could not set overwrite on channel %s.", channel.id)
        return hidden, failed

    # ------------------------------------------------------------------ #
    # Owner commands
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="verify-setup",
        description="جهّز بوابة التحقق وخفِّ كل القنوات عن غير المتحققين (للإدارة فقط).",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        verify_role="الرتبة اللي تُعطى بعد التحقق (البادج)",
        unverify_role="رتبة الانتظار اللي تنعطى للعضو أول ما يدخل (تخفي عنه السيرفر)",
    )
    @utilities.admin_only()
    async def verify_setup(
        self,
        interaction: discord.Interaction,
        verify_role: discord.Role,
        unverify_role: discord.Role,
    ) -> None:
        guild = interaction.guild
        me = guild.me
        # Validate before touching anything server-wide.
        if verify_role == unverify_role:
            await interaction.response.send_message(
                "❌ لازم تختار رتبتين مختلفتين للتحقق والانتظار.", ephemeral=True
            )
            return
        if verify_role.is_default() or unverify_role.is_default():
            await interaction.response.send_message(
                "❌ ما ينفع تستخدم @everyone كرتبة تحقق أو انتظار.", ephemeral=True
            )
            return
        if not me.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "❌ أحتاج صلاحية **Manage Roles** عشان أعطي/أسحب الرتب وأعدّل صلاحيات القنوات.",
                ephemeral=True,
            )
            return
        if verify_role >= me.top_role or unverify_role >= me.top_role:
            await interaction.response.send_message(
                "❌ لازم رتبتي تكون **أعلى** من رتبة التحقق ورتبة الانتظار — رتّب الرتب وأعد المحاولة.",
                ephemeral=True,
            )
            return

        # The lockdown loop can outrun the 3-second interaction window.
        await interaction.response.defer(ephemeral=True)

        embed = self._build_embed()
        view = VerifyView(self)
        try:
            message = await interaction.channel.send(embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException):
            await interaction.followup.send(
                "❌ ما قدرت أنشر لوحة التحقق هنا. تأكد إني أقدر أرسل + Embed Links.",
                ephemeral=True,
            )
            return

        hidden, failed = await self._apply_lockdown(guild, unverify_role, interaction.channel)

        await self.db.upsert_verify_config(
            verify_role.id, unverify_role.id, interaction.channel.id, message.id
        )
        await self._refresh_cache()

        note = f"\n⚠️ **{failed}** قناة ما قدرت أعدّلها (تحقق من صلاحياتي فيها)." if failed else ""
        await interaction.followup.send(
            f"✅ تجهزت بوابة التحقق هنا.\n"
            f"• رتبة التحقق: {verify_role.mention}\n"
            f"• رتبة الانتظار: {unverify_role.mention}\n"
            f"• خبّيت **{hidden}** قناة عن رتبة الانتظار (عدا هالقناة).{note}\n\n"
            f"أي عضو يدخل من الحين ياخذ رتبة الانتظار ويشوف هالقناة بس، لين يتحقق.",
            ephemeral=True,
        )
        log.info(
            "Verify gate configured: channel=%s verify=%s unverify=%s (hidden=%s failed=%s)",
            interaction.channel.id, verify_role.id, unverify_role.id, hidden, failed,
        )

    @app_commands.command(
        name="verify-status", description="شوف وضع بوابة التحقق (للإدارة فقط)."
    )
    @app_commands.default_permissions(administrator=True)
    @utilities.admin_only()
    async def verify_status(self, interaction: discord.Interaction) -> None:
        row = await self.db.get_verify_config()
        if row is None:
            await interaction.response.send_message(
                "❌ بوابة التحقق مو مجهزة. استخدم `/verify-setup` أول.", ephemeral=True
            )
            return
        guild = interaction.guild
        verify_role = guild.get_role(row["role_id"]) if guild else None
        unverify_role = (
            guild.get_role(row["unverify_role_id"])
            if guild and row["unverify_role_id"]
            else None
        )
        verify_txt = verify_role.mention if verify_role else f"`{row['role_id']}` (محذوفة!)"
        unverify_txt = (
            unverify_role.mention if unverify_role else f"`{row['unverify_role_id']}` (محذوفة!)"
        )
        count = len(verify_role.members) if verify_role else 0
        await interaction.response.send_message(
            f"**القناة:** <#{row['channel_id']}>\n"
            f"**رتبة التحقق:** {verify_txt}\n"
            f"**رتبة الانتظار:** {unverify_txt}\n"
            f"**الحالة:** {'🟢 مفعّل' if row['enabled'] else '🔴 معطّل'}\n"
            f"**عدد المتحققين:** {count:,}",
            ephemeral=True,
        )

    @app_commands.command(
        name="verify-enable", description="شغّل بوابة التحقق (للإدارة فقط)."
    )
    @app_commands.default_permissions(administrator=True)
    @utilities.admin_only()
    async def verify_enable(self, interaction: discord.Interaction) -> None:
        await self._set_enabled(interaction, True, "✅ بوابة التحقق اشتغلت.")

    @app_commands.command(
        name="verify-disable", description="وقّف بوابة التحقق (للإدارة فقط)."
    )
    @app_commands.default_permissions(administrator=True)
    @utilities.admin_only()
    async def verify_disable(self, interaction: discord.Interaction) -> None:
        await self._set_enabled(interaction, False, "✅ بوابة التحقق توقفت (الأعضاء الجدد ما ينحجزون).")

    async def _set_enabled(
        self, interaction: discord.Interaction, enabled: bool, confirmation: str
    ) -> None:
        if await self.db.get_verify_config() is None:
            await interaction.response.send_message(
                "❌ بوابة التحقق مو مجهزة. استخدم `/verify-setup` أول.", ephemeral=True
            )
            return
        await self.db.set_verify_enabled(enabled)
        await self._refresh_cache()
        await interaction.response.send_message(confirmation, ephemeral=True)
        log.info("Verify gate %s.", "enabled" if enabled else "disabled")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Verify(bot))
