"""Drop-in: !dmall — DM every human member (Developer-Portal team only).

Mass DMs are exactly what Discord's anti-spam heuristics look for, so this
command sends in short bursts with a long cooldown between them (which looks
far more organic than a steady drip), asks for confirmation before starting,
can be aborted mid-run by typing `stop`, and aborts ITSELF if Discord starts
erroring on consecutive sends — that's the API telling us to back off, and
pushing through it is how bots get quarantined. Prefer an announcement
channel + role ping for routine news; keep this for the rare message that
truly must reach everyone's inbox.

Only ONE broadcast can exist at a time. A second `!dmall` — even while the
first is still waiting at its yes/no prompt — is refused up front, because a
single `yes` in the channel would otherwise confirm both prompts and a second
run would reset the shared stop signal (which is exactly what made `stop`
stop working). The guard below is deliberately independent of the
max_concurrency decorator so the invariant holds regardless of deploy state.
"""

import asyncio
import logging
import random

import discord
from discord.ext import commands

log = logging.getLogger("nightvoid.extras.dmall")

BURST_SIZE = 15          # DMs per burst
IN_BURST_DELAY = 1.0     # seconds between DMs inside a burst
COOLDOWN_RANGE = (20, 30)    # rest between bursts (randomized), seconds
MAX_CONSECUTIVE_ERRORS = 5   # API errors in a row -> auto-abort (spam signal)

BUSY_MSG = "⚠️ فيه إرسال جماعي شغّال حالياً — اكتب `stop` توقفه أو انتظره يخلص."


class DmAll(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._team_ids: set[int] | None = None  # fetched once, then cached
        self._active = False            # a broadcast (incl. its prompt) is live
        self._stop = asyncio.Event()    # set -> the live broadcast aborts ASAP

    async def _team_member_ids(self) -> set[int]:
        """Everyone on the app's Developer-Portal team (or just the owner if
        the app isn't owned by a team). Managed on the portal — adding or
        removing a teammate there is all it takes."""
        if self._team_ids is None:
            app = await self.bot.application_info()
            if app.team:
                self._team_ids = {m.id for m in app.team.members}
            else:
                self._team_ids = {app.owner.id}
            log.info("dmall team allowlist loaded: %s member(s)", len(self._team_ids))
        return self._team_ids

    async def cog_check(self, ctx: commands.Context) -> bool:
        # Unlike the Owner cog (pinned to one account), this command is open
        # to the whole dev team. Still silent to everyone else (CheckFailure
        # is swallowed in bot.py), so its existence never leaks.
        if await self.bot.is_owner(ctx.author):
            return True
        return ctx.author.id in await self._team_member_ids()

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        # Stay silent for non-team users so the command remains invisible.
        if isinstance(error, commands.CheckFailure):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("⚠️ اكتب الرسالة بعد الأمر: `!dmall <نص الرسالة>`")
            return
        if isinstance(error, commands.MaxConcurrencyReached):
            await ctx.send(BUSY_MSG)
            return
        log.exception("dmall command error", exc_info=error)
        await ctx.send("⚠️ صار فيه خطأ وأنا أشغّل الأمر.")

    @commands.command(name="dmall")
    @commands.guild_only()
    @commands.max_concurrency(1, wait=False)  # never two broadcasts at once
    async def dmall(self, ctx: commands.Context, *, message: str) -> None:
        """DM every human member. Confirm with `yes`; abort anytime with `stop`."""
        # Re-entrancy guard set BEFORE the first await, so a second !dmall can
        # never open a parallel confirmation prompt or reset the stop signal.
        if self._active:
            await ctx.send(BUSY_MSG)
            return
        self._active = True
        self._stop.clear()
        try:
            await self._broadcast(ctx, message)
        finally:
            self._active = False

    async def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep, but return immediately if `stop` was typed."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass  # normal case: the full delay elapsed without a stop

    async def _broadcast(self, ctx: commands.Context, message: str) -> None:
        # Fill the member cache if it isn't already — but never block forever on
        # it. `chunk()` waits on the gateway to stream every member, and if that
        # stalls the whole command would hang silently with no prompt (which is
        # exactly what "!dmall <text> does nothing" looks like). Cap it and fall
        # back to whatever is already cached.
        if not ctx.guild.chunked:
            try:
                await asyncio.wait_for(ctx.guild.chunk(), timeout=15)
            except (asyncio.TimeoutError, discord.ClientException, discord.HTTPException):
                log.warning(
                    "dmall: guild.chunk() unavailable; using cached member list (%d cached).",
                    len(ctx.guild.members),
                )
        members = [m for m in ctx.guild.members if not m.bot]
        bursts = -(-len(members) // BURST_SIZE)  # ceil
        avg_cooldown = sum(COOLDOWN_RANGE) / 2
        eta_min = int((len(members) * IN_BURST_DELAY + max(bursts - 1, 0) * avg_cooldown) / 60) + 1

        await ctx.send(
            f"⚠️ راح ترسل رسالة خاصة إلى **{len(members)}** عضو "
            f"(تقريباً {eta_min} دقيقة).\n"
            f"رد بـ `yes` للتأكيد أو `no` للإلغاء (30 ثانية).\n"
            f"بعد ما تبدأ، اكتب `stop` أي وقت عشان توقفها."
        )

        def confirm(m: discord.Message) -> bool:
            return (
                m.author == ctx.author
                and m.channel == ctx.channel
                and m.content.lower() in ("yes", "no")
            )

        try:
            reply = await self.bot.wait_for("message", check=confirm, timeout=30)
        except asyncio.TimeoutError:
            await ctx.send("❌ تم الإلغاء (انتهى الوقت).")
            return
        if reply.content.lower() == "no":
            await ctx.send("❌ تم الإلغاء.")
            return

        def stop_check(m: discord.Message) -> bool:
            return (
                m.author == ctx.author
                and m.channel == ctx.channel
                and m.content.lower() == "stop"
            )

        async def watch_stop() -> None:
            await self.bot.wait_for("message", check=stop_check)
            self._stop.set()

        watcher = asyncio.create_task(watch_stop())
        no_pings = discord.AllowedMentions.none()
        # Failures are split by cause so the report can tell an unfixable
        # recipient-side block apart from a real API problem:
        #   blocked  -> discord.Forbidden (DMs closed / bot blocked, code 50007)
        #   errored  -> any other discord.HTTPException (rate limits, 5xx, ...)
        sent = blocked = errored = consecutive_errors = 0
        first_reason: str | None = None  # code+text of the first failure, for the hint

        async def edit_status(text: str) -> None:
            try:
                await status.edit(content=text)
            except discord.HTTPException:
                pass  # status message deleted — keep going anyway

        def breakdown() -> str:
            """Per-cause failure counts, with a hint when nothing got through."""
            lines = [
                f"✅ تم الإرسال: **{sent}**",
                f"🔒 الخاص مقفّل/مرفوض: **{blocked}**",
                f"⚠️ أخطاء اتصال: **{errored}**",
            ]
            if sent == 0 and blocked > 0:
                # Everyone refused the DM: this is a privacy/blocking condition on
                # the recipients' side (or Discord blocking a new/flagged bot), not
                # a bug — a bot needs no permission to DM, so nothing in the code
                # forces it through.
                lines.append(
                    "ℹ️ ولا رسالة وصلت — الأعضاء ما يستقبلون رسائل خاصة من البوت "
                    "(إعدادات الخصوصية أو حظر البوت)"
                    + (f" — الكود: `{first_reason}`" if first_reason else "")
                    + ".\nاستخدم قناة إعلانات + منشن للرول للوصول للكل."
                )
            return "\n".join(lines)

        status = await ctx.send(f"📨 جاري الإرسال... 0/{len(members)}")

        try:
            for i, member in enumerate(members, 1):
                if self._stop.is_set():
                    await edit_status(
                        f"🛑 تم الإيقاف عند {i - 1}/{len(members)}.\n{breakdown()}"
                    )
                    return
                try:
                    await member.send(message, allowed_mentions=no_pings)
                    sent += 1
                    consecutive_errors = 0
                except discord.Forbidden as e:
                    blocked += 1  # DMs closed or bot blocked — not our fault
                    consecutive_errors = 0
                    if first_reason is None:
                        first_reason = f"{e.code} {e.text}"
                        log.info(
                            "dmall: DM refused for %s — code=%s text=%r",
                            member.id, e.code, e.text,
                        )
                except discord.HTTPException as e:
                    errored += 1
                    consecutive_errors += 1
                    if first_reason is None:
                        first_reason = f"{e.code} {e.text}"
                    log.exception("dmall: failed to DM %s", member.id)
                    # A run of raw API errors means Discord is pushing back
                    # (e.g. 40003 "opening DMs too fast"). Keeping going from
                    # here is how bots get flagged — bail out instead.
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        await edit_status(
                            f"🚨 توقفت تلقائياً عند {i}/{len(members)} — ديسكورد "
                            f"بدأ يرفض الإرسال ({MAX_CONSECUTIVE_ERRORS} أخطاء ورا "
                            f"بعض).\n{breakdown()}"
                        )
                        log.warning(
                            "dmall auto-aborted at %s/%s after %s consecutive API errors",
                            i, len(members), MAX_CONSECUTIVE_ERRORS,
                        )
                        return

                # Burst pattern: quick inside a burst, long rest between bursts.
                # Reads as organic activity instead of a nonstop DM drip. Both
                # waits return the instant `stop` is typed.
                if i % BURST_SIZE == 0 and i < len(members):
                    cooldown = random.uniform(*COOLDOWN_RANGE)
                    await edit_status(
                        f"📨 {i}/{len(members)} — ⏸️ استراحة "
                        f"{int(cooldown)} ثانية (حماية من فلاتر السبام)..."
                    )
                    await self._interruptible_sleep(cooldown)
                elif i < len(members):
                    await self._interruptible_sleep(IN_BURST_DELAY)
        finally:
            watcher.cancel()
            log.info(
                "dmall by %s: sent=%s blocked=%s errored=%s reason=%s",
                ctx.author.id, sent, blocked, errored, first_reason,
            )

        if self._stop.is_set():
            await edit_status(f"🛑 تم الإيقاف.\n{breakdown()}")
        else:
            await edit_status(breakdown())

        # Fallback: DMs didn't reach everyone (usually because recipients don't
        # accept the bot's DMs). Offer to post the message right here so the
        # announcement still lands — an announcement channel is the right tool
        # for "reach everyone" anyway.
        if blocked + errored > 0:
            await self._offer_channel_fallback(ctx, message, blocked + errored)

    async def _offer_channel_fallback(
        self, ctx: commands.Context, message: str, missed: int
    ) -> None:
        await ctx.send(
            f"📢 ما وصلت الرسالة لـ **{missed}** عضو. تبي أنشرها هني في القناة؟ "
            f"رد بـ `send` خلال 30 ثانية."
        )

        def wants_post(m: discord.Message) -> bool:
            return (
                m.author == ctx.author
                and m.channel == ctx.channel
                and m.content.lower() == "send"
            )

        try:
            await self.bot.wait_for("message", check=wants_post, timeout=30)
        except asyncio.TimeoutError:
            return  # operator didn't ask for it — leave the channel quiet
        await ctx.send(message, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DmAll(bot))
