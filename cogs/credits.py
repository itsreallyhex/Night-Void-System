"""System 5 — Credit system (message + voice earning).

Message earning: weighted random award (higher amounts exponentially rarer),
per-user cooldown, per-minute anti-spam cap, ignored-channel list.
Voice earning: fixed award every interval while in a non-AFK voice channel with
at least one other non-bot member present. A background task polls voice state.
"""

import logging
import math
import random
import time
from collections import defaultdict, deque
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

import branding
import config
import utilities
from amounts import parse_amount

log = logging.getLogger("nightvoid.credits")


class Credits(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db
        # Anti-spam / cooldown trackers (in-memory).
        self._last_award: dict[int, float] = {}
        self._msg_window: dict[int, deque[float]] = defaultdict(deque)
        # Precompute the weighted distribution for message awards.
        self._values, self._weights = self._build_distribution()
        self.voice_tick.start()
        self._prune_trackers.start()

    def cog_unload(self) -> None:
        self.voice_tick.cancel()
        self._prune_trackers.cancel()

    # ------------------------------------------------------------------ #
    # Housekeeping: the cooldown/anti-spam dicts are keyed by user id and
    # would otherwise grow with every member who ever sent a message.
    # ------------------------------------------------------------------ #
    @tasks.loop(hours=6)
    async def _prune_trackers(self) -> None:
        now = time.monotonic()
        for user_id, last in list(self._last_award.items()):
            if now - last >= config.CREDIT_MSG_COOLDOWN:
                del self._last_award[user_id]
        for user_id in list(self._msg_window):
            window = self._msg_window[user_id]
            while window and now - window[0] >= 60:
                window.popleft()
            if not window:
                del self._msg_window[user_id]

    # ------------------------------------------------------------------ #
    # Weighted distribution
    # ------------------------------------------------------------------ #
    def _build_distribution(self) -> tuple[list[int], list[float]]:
        lo, hi = config.CREDIT_MSG_MIN, config.CREDIT_MSG_MAX
        values = list(range(lo, hi + 1))
        lam = config.CREDIT_WEIGHT_LAMBDA
        # Exponential decay from the low end: weight = e^(-lambda * (v - lo)).
        weights = [math.exp(-lam * (v - lo)) for v in values]
        return values, weights

    def _roll_award(self) -> int:
        return random.choices(self._values, weights=self._weights, k=1)[0]

    # ------------------------------------------------------------------ #
    # Message earning
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if message.channel.id in config.CREDIT_IGNORED_CHANNELS:
            return
        # Quality gate: ignore trivially short messages (single-char / emoji
        # spam) so they can't farm the per-message award.
        if len(message.content.strip()) < 3:
            return

        user_id = message.author.id
        now = time.monotonic()

        # Anti-spam: at most N counted messages per rolling 60s.
        window = self._msg_window[user_id]
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= config.CREDIT_MSG_MAX_PER_MIN:
            return
        window.append(now)

        # Cooldown between awards.
        last = self._last_award.get(user_id, 0.0)
        if now - last < config.CREDIT_MSG_COOLDOWN:
            return
        self._last_award[user_id] = now

        amount = self._roll_award()
        await self.db.add_credits(user_id, amount)
        log.debug("Awarded %s message credits to %s", amount, message.author)

    # ------------------------------------------------------------------ #
    # Voice earning
    # ------------------------------------------------------------------ #
    @tasks.loop(seconds=config.CREDIT_VOICE_INTERVAL)
    async def voice_tick(self) -> None:
        guild = self.bot.get_guild(config.GUILD_ID)
        if guild is None:
            return
        for channel in guild.voice_channels:
            if config.AFK_CHANNEL_ID and channel.id == config.AFK_CHANNEL_ID:
                continue
            humans = [m for m in channel.members if not m.bot]
            # Require at least two real people in the channel — a user sitting
            # alone earns nothing (anti-farm).
            if len(humans) < 2:
                continue
            for member in humans:
                if member.voice and (member.voice.self_deaf or member.voice.deaf):
                    continue
                await self.db.add_credits(member.id, config.CREDIT_VOICE_AMOUNT)
            log.debug(
                "Awarded %s voice credits to %d members in %s",
                config.CREDIT_VOICE_AMOUNT, len(humans), channel.name,
            )

    @voice_tick.before_loop
    async def _before_voice(self) -> None:
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    @app_commands.command(name="balance", description="اعرف رصيدك من الكريدت.")
    async def balance(self, interaction: discord.Interaction) -> None:
        bal = await self.db.get_balance(interaction.user.id)
        await interaction.response.send_message(
            f"💰 رصيدك: **{bal:,}** كريدت.", ephemeral=True
        )

    @app_commands.command(
        name="leaderboard", description="أعلى 10 أعضاء في الكريدت."
    )
    async def leaderboard(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        # Pull a wider pool than we show, then drop anyone who has left the
        # server, so the board is always 10 *current* members even when top
        # holders have gone (their balances are burned later by the leavers
        # system; this only keeps them off the board immediately).
        rows = await self.db.top_balances(50, exclude=[config.OWNER_USER_ID])
        present = [
            row for row in rows
            if guild is not None and guild.get_member(row["user_id"]) is not None
        ][:10]
        if not present:
            await interaction.followup.send(
                "📊 ما فيه أحد عنده كريدت لين الحين.", ephemeral=True
            )
            return

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines: list[str] = []
        for rank, row in enumerate(present, start=1):
            member = guild.get_member(row["user_id"])
            badge = medals.get(rank, f"**{rank}.**")
            lines.append(f"{badge} {member.mention} — **{row['balance']:,}** كريدت")

        embed = discord.Embed(
            title="أعلى 10 في الكريدت",
            description="\n".join(lines),
            color=branding.GOLD,
        )
        utilities.brand_footer(embed, "لوحة الصدارة")
        await interaction.followup.send(
            embed=embed, allowed_mentions=discord.AllowedMentions.none(), ephemeral=True
        )

    # ------------------------------------------------------------------ #
    # Transfers
    # ------------------------------------------------------------------ #
    @app_commands.command(name="pay", description="حوّل كريدت لعضو ثاني.")
    @app_commands.describe(
        user="العضو اللي تبي تحوّل له",
        amount=f"المبلغ (يقبل 1k / 2.5m — أقل شي {config.PAY_MIN_AMOUNT})",
    )
    async def pay(
        self, interaction: discord.Interaction, user: discord.Member, amount: str
    ) -> None:
        sender = interaction.user
        if user.bot:
            await interaction.response.send_message(
                "❌ ما تقدر تحوّل كريدت لبوت.", ephemeral=True
            )
            return
        if user.id == sender.id:
            await interaction.response.send_message(
                "❌ ما تقدر تحوّل لنفسك.", ephemeral=True
            )
            return

        try:
            value = parse_amount(amount)
        except ValueError:
            await interaction.response.send_message(
                "❌ المبلغ غير صالح. اكتب رقم مثل `500` أو `1k`.", ephemeral=True
            )
            return
        if value < config.PAY_MIN_AMOUNT:
            await interaction.response.send_message(
                f"❌ أقل مبلغ للتحويل هو **{config.PAY_MIN_AMOUNT:,}** كريدت.",
                ephemeral=True,
            )
            return

        # Rolling 24h send cap (anti alt-account funnelling).
        since = (utilities.utc_now() - timedelta(hours=24)).isoformat()
        sent_today = await self.db.transfers_sent_since(sender.id, since)
        if sent_today + value > config.PAY_DAILY_CAP:
            remaining = max(0, config.PAY_DAILY_CAP - sent_today)
            await interaction.response.send_message(
                f"❌ وصلت الحد اليومي للتحويل (**{config.PAY_DAILY_CAP:,}** كريدت / ٢٤ ساعة). "
                f"المتبقي لك اليوم: **{remaining:,}**.",
                ephemeral=True,
            )
            return

        fee = value * config.PAY_FEE_PERCENT // 100
        received = value - fee
        if not await self.db.transfer_credits(
            sender.id, user.id, value, fee, utilities.utc_now_iso()
        ):
            balance = await self.db.get_balance(sender.id)
            await interaction.response.send_message(
                f"❌ رصيدك ما يكفي. تحتاج {value:,} وعندك {balance:,}.",
                ephemeral=True,
            )
            return

        fee_note = f" (رسوم التحويل: **{fee:,}**)" if fee else ""
        new_balance = await self.db.get_balance(sender.id)
        await interaction.response.send_message(
            f"✅ حوّلت **{value:,}** كريدت لـ {user.mention}، "
            f"وصله **{received:,}**{fee_note}. رصيدك الحين: **{new_balance:,}**.",
            ephemeral=True,
        )
        # Tell the recipient (best effort — their DMs may be closed).
        await utilities.try_dm(
            user,
            f"💸 وصلك تحويل: **{received:,}** كريدت من **{sender.display_name}**.",
        )
        log.info(
            "Transfer: %s -> %s, amount %s (fee %s)", sender, user, value, fee
        )

    # ------------------------------------------------------------------ #
    # Admin: economy dashboard
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="economy", description="لوحة اقتصاد السيرفر (للإدارة)."
    )
    @app_commands.default_permissions(administrator=True)
    @utilities.admin_only("❌ هذا الأمر للإدارة فقط.")
    async def economy(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        now = utilities.utc_now()
        windows = (
            ("آخر ٧ أيام", now - timedelta(days=7)),
            ("آخر شهر", now - timedelta(days=30)),
            ("آخر ٦ أشهر", now - timedelta(days=180)),
            ("كل الوقت", None),
        )

        circ = await self.db.circulating(exclude=config.OWNER_USER_ID)
        embed = discord.Embed(
            title="اقتصاد Night Void",
            color=branding.GOLD,
            timestamp=now,
        )
        embed.add_field(
            name="المتداول حالياً",
            value=f"**{circ['total']:,}** كريدت • **{circ['holders']:,}** حامل",
            inline=False,
        )
        for label, since in windows:
            d = await self.db.economy_window(
                since.isoformat() if since else None,
                exclude=config.OWNER_USER_ID,
            )
            embed.add_field(
                name=label,
                value=(
                    f"مصروف: **{d['spent_total']:,}** كريدت "
                    f"({d['spent_count']:,} عملية)\n"
                    f"رتب: **{d['role_spent']:,}** • "
                    f"خدمات: **{d['service_spent']:,}**\n"
                    f"تقييمات: **{d['reviews']:,}**"
                ),
                inline=False,
            )
        embed.set_footer(
            text="يُسجّل المصروف فقط؛ كسب الكريدت التاريخي يحتاج سجل معاملات."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        log.info("Economy dashboard viewed by %s", interaction.user)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Credits(bot))
