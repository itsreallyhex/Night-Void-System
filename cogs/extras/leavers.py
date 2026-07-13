"""Drop-in: cleanup when a member leaves (or is kicked/banned).

What happens on leave:
  * Their open ticket, if any, is closed immediately — DB row, log-channel
    entry, and the channel deleted — instead of sitting open forever.
  * Their credit balance enters a 24-hour grace period. Rejoining within it
    cancels the burn with everything intact; staying gone past it burns the
    balance. The pending mark lives in the database, so redeploys/restarts
    can't lose the clock.

What is deliberately KEPT forever:
  * Their username record — so old purchases/reviews in the v_* views stay
    readable instead of collapsing back to raw ids.
  * All history rows (purchases, redemptions, reviews, transfers) — that's
    the store's audit trail, not the member's property.
"""

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

import config
import utilities

log = logging.getLogger("nightvoid.extras.leavers")

GRACE_HOURS = 24
SWEEP_MINUTES = 60


class Leavers(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db

    async def cog_load(self) -> None:
        self._burn_sweep.start()

    async def cog_unload(self) -> None:
        self._burn_sweep.cancel()

    # ------------------------------------------------------------------ #
    # Leave: close ticket now, start the burn clock
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot or member.guild.id != config.GUILD_ID:
            return

        # The two cleanup jobs are independent — a failure in one must never
        # skip the other, so each gets its own try.
        try:
            if await self.db.get_balance(member.id) > 0:
                await self.db.mark_leaver(member.id, utilities.utc_now_iso())
                log.info(
                    "Member %s (%s) left; balance burns in %sh unless they return.",
                    member, member.id, GRACE_HOURS,
                )
        except Exception:
            log.exception("Failed to mark leaver %s for the grace period.", member.id)

        try:
            await self._close_leftover_ticket(member)
        except Exception:
            log.exception("Failed to close ticket after %s left.", member.id)

    async def _close_leftover_ticket(self, member: discord.Member) -> None:
        ticket = await self.db.get_open_ticket(member.id)
        if ticket is None:
            return
        await self.db.close_ticket(ticket["id"], utilities.utc_now_iso())

        guild = member.guild
        tickets_cog = self.bot.get_cog("Tickets")
        if tickets_cog is not None:
            try:
                await tickets_cog._log_closure(guild, ticket, guild.me)
            except discord.HTTPException:
                log.exception("Could not post leave-closure of ticket #%s to the log.", ticket["id"])

        channel = guild.get_channel(ticket["channel_id"])
        if channel is not None:
            try:
                if isinstance(channel, discord.Thread):
                    await channel.edit(archived=True, locked=True)
                else:
                    await channel.delete(
                        reason=f"Ticket #{ticket['id']} closed — opener left the server"
                    )
            except discord.HTTPException:
                log.exception(
                    "Failed to remove ticket channel %s after opener left.", channel.id
                )
        log.info("Ticket #%s closed because opener %s left.", ticket["id"], member.id)

    # ------------------------------------------------------------------ #
    # Rejoin inside the grace period: cancel the burn
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot or member.guild.id != config.GUILD_ID:
            return
        try:
            if await self.db.unmark_leaver(member.id):
                log.info(
                    "Member %s (%s) returned within the grace period; balance kept.",
                    member, member.id,
                )
        except Exception:
            # Not fatal even if this fails: the sweep re-checks real guild
            # membership before burning, so a returned member is never burned.
            log.exception("Failed to cancel pending burn for rejoined %s.", member.id)

    # ------------------------------------------------------------------ #
    # Hourly sweep: burn whoever stayed gone past the grace period
    # ------------------------------------------------------------------ #
    @tasks.loop(minutes=SWEEP_MINUTES)
    async def _burn_sweep(self) -> None:
        # An exception escaping a tasks.loop body KILLS the loop until the
        # next restart — everything here must be caught.
        try:
            guild = self.bot.get_guild(config.GUILD_ID)
            if guild is None:
                return
            cutoff = (
                datetime.now(timezone.utc) - timedelta(hours=GRACE_HOURS)
            ).isoformat()
            due = await self.db.due_leavers(cutoff)
        except Exception:
            log.exception("Burn sweep failed to fetch due leavers; retrying next hour.")
            return

        for user_id in due:
            try:
                # Safety net: if the rejoin event was ever missed (downtime),
                # being in the guild always wins over the pending mark.
                if guild.get_member(user_id) is not None:
                    await self.db.unmark_leaver(user_id)
                    continue
                burned = await self.db.clear_balance(user_id)
                await self.db.unmark_leaver(user_id)
                if burned:
                    log.info(
                        "Burned %s credits from %s (gone > %sh).",
                        burned, user_id, GRACE_HOURS,
                    )
            except Exception:
                # One bad row must not block the rest of the queue.
                log.exception("Burn sweep failed for user %s; will retry next hour.", user_id)

    @_burn_sweep.before_loop
    async def _before_sweep(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Leavers(bot))
