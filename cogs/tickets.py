"""System 1 — One-hold ticket system.

A user may hold exactly one open ticket. Duplicate prevention is enforced at the
database level via a partial unique index (see database.py). Tickets are created
as private threads or dedicated channels (TICKET_USE_THREADS). Closing a ticket
triggers the review system (System 3). User-facing text is Saudi Arabic.
"""

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import branding
import config
import utilities

log = logging.getLogger("nightvoid.tickets")

OPEN_TICKET_ID = "nv:ticket:open"
CLOSE_TICKET_ID = "nv:ticket:close"
# Footer marker that identifies the bot's own inactivity warning, so the sweep
# can tell "the last message is our warning" from real conversation.
AUTOCLOSE_MARK = "nv:ticket:autoclose-warning"


# The ticket sections. Each opens with its own welcome layout, embed colour,
# and channel-name prefix, so staff can tell the queue apart at a glance.
TICKET_TYPES: dict[str, dict] = {
    "store": {
        "label": "تذكرة متجر",
        "emoji": "🛒",
        "prefix": "store",
        "desc": "طلب أو شراء من المتجر",
        "color": branding.BRAND,
        "title": "تذكرة متجر",
        "welcome": (
            "شكراً {user}، أحد من الإدارة بيجيك بأقرب وقت.\n\n"
            "**عشان نخدمك أسرع، اكتب من الحين:**\n"
            "🛒 **المنتج أو الخدمة** اللي تبيها\n"
            "💳 **طريقة الدفع** المناسبة لك\n"
            "📝 أي **تفاصيل إضافية** عن طلبك\n\n"
            "أي شي **غير متعلق بالمتجر**، أو إذا ما كتبت طلبك، "
            "**راح تنسكر التذكرة تلقائياً**."
        ),
    },
    "ask": {
        "label": "استفسار",
        "emoji": "❓",
        "prefix": "ask",
        "desc": "سؤال عن المتجر أو السيرفر",
        "color": branding.INFO,
        "title": "تذكرة استفسار",
        "welcome": (
            "أهلاً {user}! اسأل والإدارة تجاوبك.\n\n"
            "❓ اكتب **سؤالك بوضوح** في رسالة وحدة\n"
            "🖼️ أرفق **صور أو سكرينشوت** إذا كانت تساعد\n"
            "📦 إذا سؤالك عن **طلب سابق**، اذكر تفاصيله\n\n"
            "إذا ما كتبت سؤالك، **راح تنسكر التذكرة تلقائياً**."
        ),
    },
    "suggest": {
        "label": "اقتراح",
        "emoji": "💡",
        "prefix": "sug",
        "desc": "فكرة أو تحسين تشاركنا فيه",
        "color": branding.GOLD,
        "title": "تذكرة اقتراح",
        "welcome": (
            "يعطيك العافية {user}! نحب نسمع اقتراحاتك.\n\n"
            "💡 **وش الاقتراح؟** اشرحه ببساطة\n"
            "🎯 **وش الفايدة منه** أو المشكلة اللي يحلها؟\n"
            "📌 أمثلة أو تفاصيل تساعدنا نقيّمه\n\n"
            "كل اقتراح ينقرأ، حتى لو ما اتطبق على طول."
        ),
    },
}


class TicketSectionSelect(discord.ui.Select):
    """The section dropdown: open it, pick the exact kind of support you want,
    and a ticket of that type opens immediately."""

    def __init__(self, cog: "Tickets") -> None:
        options = [
            discord.SelectOption(
                label=spec["label"], value=key,
                emoji=spec["emoji"], description=spec["desc"],
            )
            for key, spec in TICKET_TYPES.items()
        ]
        super().__init__(
            placeholder="🎫 اختر نوع الدعم اللي تحتاجه…",
            min_values=1, max_values=1,
            options=options,
            custom_id="nv:ticket:section",
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.open_ticket(interaction, self.values[0])
        # Clear the visual selection so the shared panel doesn't stay stuck on
        # this user's choice (cosmetic, best effort).
        if interaction.message is not None and self.view is not None:
            try:
                await interaction.message.edit(view=self.view)
            except discord.HTTPException:
                pass


class TicketPanelView(discord.ui.View):
    """Persistent panel: the section dropdown plus the store link."""

    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.add_item(TicketSectionSelect(cog))
        store = branding.store_button()
        if store is not None:
            self.add_item(store)


class LegacyPanelView(discord.ui.View):
    """Compatibility with panels posted in older layouts, so they keep working
    without a repost:
    - the original single 'افتح تذكرة' button -> replies with the dropdown
    - the three-button layout -> opens that section directly
    """

    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="افتح تذكرة", style=discord.ButtonStyle.primary,
        emoji="🎫", custom_id=OPEN_TICKET_ID,
    )
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        view = discord.ui.View(timeout=180)
        view.add_item(TicketSectionSelect(self.cog))
        await interaction.response.send_message(
            "اختر نوع الدعم:", view=view, ephemeral=True
        )

    @discord.ui.button(label="تذكرة متجر", emoji="🛒", custom_id="nv:ticket:open:store")
    async def open_store(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.open_ticket(interaction, "store")

    @discord.ui.button(label="استفسار", emoji="❓", custom_id="nv:ticket:open:ask")
    async def open_ask(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.open_ticket(interaction, "ask")

    @discord.ui.button(label="اقتراح", emoji="💡", custom_id="nv:ticket:open:suggest")
    async def open_suggest(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.open_ticket(interaction, "suggest")


class TicketControlView(discord.ui.View):
    """Persistent in-ticket controls (staff close button)."""

    def __init__(self, cog: "Tickets") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="سكّر التذكرة", style=discord.ButtonStyle.danger,
        emoji="🔒", custom_id=CLOSE_TICKET_ID,
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.close_ticket(interaction)


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db

    async def cog_load(self) -> None:
        # Re-register persistent views so buttons survive a restart.
        self.bot.add_view(TicketPanelView(self))
        self.bot.add_view(LegacyPanelView(self))
        self.bot.add_view(TicketControlView(self))
        if config.TICKET_AUTOCLOSE_HOURS > 0:
            self._autoclose_sweep.start()

    def cog_unload(self) -> None:
        if self._autoclose_sweep.is_running():
            self._autoclose_sweep.cancel()

    # ------------------------------------------------------------------ #
    # Inactivity auto-close
    # ------------------------------------------------------------------ #
    @tasks.loop(minutes=10)
    async def _autoclose_sweep(self) -> None:
        """Warn tickets whose OPENER has been silent for TICKET_AUTOCLOSE_HOURS,
        then close them if the opener stays silent for TICKET_AUTOCLOSE_GRACE_HOURS
        more. Only the ticket opener's messages reset the clock — staff replies
        don't keep an abandoned ticket alive.

        Stateless across restarts: the pending warning is read back from the
        channel history itself (our embed carrying AUTOCLOSE_MARK), so a
        redeploy never re-warns or forgets a pending closure.
        """
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            return
        now = datetime.now(timezone.utc)
        warn_after = timedelta(hours=config.TICKET_AUTOCLOSE_HOURS)
        grace = timedelta(hours=config.TICKET_AUTOCLOSE_GRACE_HOURS)

        for ticket in await self.db.get_open_tickets():
            channel = self.bot.get_channel(ticket["channel_id"])
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(ticket["channel_id"])
                except discord.NotFound:
                    # Deleted by hand — clear the dangling row.
                    await self.db.close_ticket(ticket["id"], utilities.utc_now_iso())
                    continue
                except discord.HTTPException:
                    continue

            # Newest-first scan: the opener's latest message and the latest
            # warning. Stop at the opener's first hit — any warning newer than
            # it has already been seen.
            last_user_time = None
            warning_time = None
            try:
                async for m in channel.history(limit=100):
                    if (
                        warning_time is None
                        and m.author.id == self.bot.user.id
                        and m.embeds
                        and m.embeds[0].footer.text == AUTOCLOSE_MARK
                    ):
                        warning_time = m.created_at
                    if m.author.id == ticket["user_id"]:
                        last_user_time = m.created_at
                        break
            except discord.HTTPException:
                continue
            if last_user_time is None:
                # Opener never wrote (or it's beyond the scan window): fall
                # back to the ticket's creation time.
                last_user_time = datetime.fromisoformat(ticket["created_at"])

            if warning_time is not None and warning_time > last_user_time:
                # Warning pending and the opener hasn't replied since.
                if now - warning_time >= grace:
                    await self._auto_close(guild, ticket, channel)
            elif now - last_user_time >= warn_after:
                embed = discord.Embed(
                    title="⏰ تذكرة غير نشطة",
                    description=(
                        f"ما فيه رد منك من **{config.TICKET_AUTOCLOSE_HOURS}** ساعة.\n"
                        f"إذا ما رديت خلال **{config.TICKET_AUTOCLOSE_GRACE_HOURS}** "
                        "ساعات، التذكرة بتنسكر تلقائياً."
                    ),
                    color=branding.WARNING,
                )
                embed.set_footer(text=AUTOCLOSE_MARK)
                try:
                    await channel.send(content=f"<@{ticket['user_id']}>", embed=embed)
                except discord.HTTPException:
                    log.warning("Could not post inactivity warning in %s", channel.id)

    @_autoclose_sweep.before_loop
    async def _before_autoclose(self) -> None:
        await self.bot.wait_until_ready()

    async def _auto_close(self, guild: discord.Guild, ticket, channel) -> None:
        """Close an inactive ticket: DB row, log entry, user DM, channel cleanup.
        No review request — an abandoned ticket wasn't a support experience."""
        await self.db.close_ticket(ticket["id"], utilities.utc_now_iso())
        await self._log_closure(guild, ticket, guild.me)

        member = guild.get_member(ticket["user_id"])
        if member is not None:
            try:
                await member.send(
                    f"🔒 تذكرتك رقم **#{ticket['id']}** انسكرت تلقائياً بسبب عدم النشاط. "
                    "تقدر تفتح تذكرة جديدة بأي وقت."
                )
            except discord.HTTPException:
                pass

        try:
            if isinstance(channel, discord.Thread):
                await channel.edit(archived=True, locked=True)
            else:
                await channel.delete(
                    reason=f"Ticket #{ticket['id']} auto-closed (inactive)"
                )
        except discord.HTTPException:
            log.exception("Failed to archive/delete auto-closed ticket channel %s", channel.id)

        log.info("Ticket #%s auto-closed for inactivity.", ticket["id"])

    # ------------------------------------------------------------------ #
    # Core actions
    # ------------------------------------------------------------------ #
    async def _channel_alive(self, channel_id: int) -> bool:
        """Whether the ticket's channel/thread still exists on Discord.

        Guards against stale 'open' rows left behind when a ticket channel is
        deleted by hand (the 🔒 button cleans up the DB; manual deletion can't).
        Uncertain errors (e.g. Forbidden) assume alive, so we never let a user
        open a duplicate when a real ticket is still around.
        """
        if self.bot.get_channel(channel_id) is not None:
            return True
        try:
            await self.bot.fetch_channel(channel_id)
            return True
        except discord.NotFound:
            return False
        except discord.HTTPException:
            return True

    async def open_ticket(self, interaction: discord.Interaction, ttype: str = "store") -> None:
        user = interaction.user
        spec = TICKET_TYPES.get(ttype, TICKET_TYPES["store"])

        existing = await self.db.get_open_ticket(user.id)
        if existing is not None:
            if await self._channel_alive(existing["channel_id"]):
                await interaction.response.send_message(
                    f"❌ عندك تذكرة مفتوحة من قبل (<#{existing['channel_id']}>). "
                    "سكّرها قبل لا تفتح وحدة ثانية.",
                    ephemeral=True,
                )
                return
            # The channel was deleted manually — clear the dangling row and
            # let the user open a fresh ticket.
            await self.db.close_ticket(existing["id"], utilities.utc_now_iso())
            log.info(
                "Auto-closed stale ticket #%s for %s (channel %s no longer exists).",
                existing["id"], user, existing["channel_id"],
            )

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild

        try:
            if config.TICKET_USE_THREADS:
                channel = await self._create_thread(interaction, user)
            else:
                channel = await self._create_channel(guild, user)
        except discord.Forbidden:
            log.error(
                "Missing permissions to create ticket for %s in guild %s",
                user, guild.id,
            )
            await interaction.followup.send(
                "❌ ما عندي صلاحية أفتح لك تذكرة. تم تنبيه الإدارة.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            log.exception("Failed to create ticket channel for %s", user)
            await interaction.followup.send(
                "❌ صار فيه خطأ وأنا أفتح تذكرتك. حاول مرة ثانية بعدين.",
                ephemeral=True,
            )
            return
                       
        ticket_id = await self.db.create_ticket(
            user.id, channel.id, utilities.utc_now_iso(), ttype
        )
        if ticket_id is None:
            # Lost a race against a concurrent open — clean up the channel.
            log.warning("Race on ticket creation for %s; rolling back channel.", user)
            try:
                await channel.delete(reason="Duplicate ticket rollback")
            except discord.HTTPException:
                pass
            await interaction.followup.send(
                "❌ عندك تذكرة مفتوحة من قبل.", ephemeral=True
            )
            return

        # Stamp the section + ticket number into the name — the channel had to
        # be created before the DB row existed, so the id wasn't known until now.
        try:
            await channel.edit(name=f"{spec['prefix']}-{user.name}-{ticket_id}")
        except discord.HTTPException:
            log.warning("Could not rename ticket channel %s", channel.id)

        embed = discord.Embed(
            title=f"{spec['emoji']} {spec['title']}",
            description=spec["welcome"].format(user=user.mention),
            color=spec["color"],
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"تذكرة #{ticket_id} • {spec['label']}")
        staff_mention = f"<@&{config.TICKET_STAFF_ROLE_ID}>"
        await channel.send(
            content=f"{user.mention} {staff_mention}",
            embed=embed,
            view=TicketControlView(self),
        )
        await interaction.followup.send(
            f"✅ تم فتح تذكرتك: {channel.mention}", ephemeral=True
        )
        log.info("Ticket #%s opened by %s -> channel %s", ticket_id, user, channel.id)

    async def _create_thread(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> discord.Thread:
        parent = interaction.channel
        thread = await parent.create_thread(
            name=user.name,
            type=discord.ChannelType.private_thread,
            invitable=False,
            reason=f"Ticket opened by {user}",
        )
        await thread.add_user(user)
        return thread

    async def _create_channel(
        self, guild: discord.Guild, user: discord.Member
    ) -> discord.TextChannel:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True),
        }
        staff_role = guild.get_role(config.TICKET_STAFF_ROLE_ID)
        if staff_role is not None:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True
            )
        category = (
            guild.get_channel(config.TICKET_CATEGORY_ID)
            if config.TICKET_CATEGORY_ID
            else None
        )
        return await guild.create_text_channel(
            name=user.name,
            overwrites=overwrites,
            category=category,
            reason=f"Ticket opened by {user}",
        )

    async def close_ticket(self, interaction: discord.Interaction) -> None:
        if not utilities.is_staff(interaction.user):
            await interaction.response.send_message(
                "❌ بس الإدارة تقدر تسكّر التذاكر.", ephemeral=True
            )
            return

        ticket = await self.db.get_ticket_by_channel(interaction.channel.id)
        if ticket is None:
            await interaction.response.send_message(
                "❌ هذي القناة مو تذكرة مفتوحة.", ephemeral=True
            )
            return

        await interaction.response.send_message("🔒 جاري تسكير التذكرة…", ephemeral=True)
        await self.db.close_ticket(ticket["id"], utilities.utc_now_iso())

        # Trigger the review system (System 3).
        reviews = self.bot.get_cog("Reviews")
        if reviews is not None:
            await reviews.request_review(ticket["user_id"], ticket["id"])

        await self._log_closure(interaction.guild, ticket, interaction.user)

        channel = interaction.channel
        try:
            if isinstance(channel, discord.Thread):
                await channel.edit(archived=True, locked=True)
            else:
                await channel.delete(reason=f"Ticket #{ticket['id']} closed by {interaction.user}")
        except discord.Forbidden:
            log.error("Missing permissions to archive/delete ticket channel %s", channel.id)
        except discord.HTTPException:
            log.exception("Failed to archive/delete ticket channel %s", channel.id)

        log.info("Ticket #%s closed by %s", ticket["id"], interaction.user)

    async def _log_closure(
        self, guild: discord.Guild, ticket, closer: discord.Member
    ) -> None:
        if not config.TICKET_LOG_CHANNEL_ID:
            return
        log_channel = guild.get_channel(config.TICKET_LOG_CHANNEL_ID)
        if log_channel is None:
            log.warning("Ticket log channel %s not found.", config.TICKET_LOG_CHANNEL_ID)
            return
        embed = discord.Embed(
            title="تم تسكير التذكرة",
            color=branding.NEUTRAL,
            timestamp=datetime.now(timezone.utc),
        )
        spec = TICKET_TYPES.get(ticket["type"], TICKET_TYPES["store"])
        embed.add_field(name="التذكرة", value=f"#{ticket['id']}", inline=True)
        embed.add_field(name="النوع", value=f"{spec['emoji']} {spec['label']}", inline=True)
        embed.add_field(name="فتحها", value=f"<@{ticket['user_id']}>", inline=True)
        embed.add_field(name="سكّرها", value=closer.mention, inline=True)
        try:
            await log_channel.send(embed=embed)
        except discord.Forbidden:
            log.error("Missing permission to post to ticket log channel %s", log_channel.id)

    # ------------------------------------------------------------------ #
    # Slash commands
    # ------------------------------------------------------------------ #
    @app_commands.command(name="ticket-panel", description="انشر لوحة فتح التذاكر (للإدارة).")
    @app_commands.default_permissions(manage_channels=True)
    @utilities.staff_only()
    async def ticket_panel(self, interaction: discord.Interaction) -> None:
        sections = "\n".join(
            f"{spec['emoji']} **{spec['label']}** — {spec['desc']}"
            for spec in TICKET_TYPES.values()
        )
        embed = discord.Embed(
            title="دعم Night Void",
            description=(
                "اختر نوع الدعم اللي تحتاجه **من القائمة تحت**:\n\n"
                f"{sections}\n\n"
                "اذا فتحت تذكرة بدون ما تكتب طلبك، فيه **احتمال تنسكر "
                "التذكرة تلقائياً**.\n\n"
                "تقدر يكون عندك **تذكرة وحدة فقط** في نفس الوقت."
            ),
            color=branding.BRAND,
        )
        embed.set_footer(text=f"{branding.FOOTER} • الدعم")
        if await utilities.post_panel(interaction, embed, TicketPanelView(self)):
            await interaction.response.send_message("✅ تم نشر اللوحة.", ephemeral=True)

    @app_commands.command(name="close", description="سكّر التذكرة الحالية (للإدارة).")
    @app_commands.default_permissions(manage_channels=True)
    async def close_command(self, interaction: discord.Interaction) -> None:
        await self.close_ticket(interaction)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tickets(bot))
