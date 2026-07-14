"""System 2 — Code redemption (panel + modal).

An admin posts a redeem panel with a button (/redeem-panel). Clicking it opens a
modal where the user types their code. Code format: NIGHTVOID-xxxxxxxxxxxxxxx (15
alphanumeric chars). Redemption assigns a linked role and is atomic against
double-spend. Rate limiting is in-memory only. All responses are ephemeral.
"""

import io
import logging
import re
import secrets
import string
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import branding
import config
import utilities
from amounts import parse_amount

log = logging.getLogger("nightvoid.codes")

CODE_BODY_LEN = 15
CODE_PATTERN = re.compile(rf"^NIGHTVOID-[A-Za-z0-9]{{{CODE_BODY_LEN}}}$")
CODE_ALPHABET = string.ascii_letters + string.digits
# Full code length = len("NIGHTVOID-") + body = 10 + CODE_BODY_LEN.
CODE_FULL_LEN = len("NIGHTVOID-") + CODE_BODY_LEN
REDEEM_BUTTON_ID = "nv:redeem:open"
# Hard ceiling on how many credits a single code may grant (anti-abuse).
CODE_CREDIT_MAX = 200_000


def generate_code() -> str:
    """Create a fresh NIGHTVOID-xxxxxxxxxxxxxxx code (15-char body)."""
    body = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_BODY_LEN))
    return f"NIGHTVOID-{body}"


class RedeemModal(discord.ui.Modal, title="استبدال كود"):
    def __init__(self, cog: "Codes") -> None:
        super().__init__()
        self.cog = cog

    code = discord.ui.TextInput(
        label="الكود حقك",
        placeholder="NIGHTVOID-AB12cd34EF9gh56",
        required=True,
        min_length=CODE_FULL_LEN,
        max_length=CODE_FULL_LEN,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.process_redeem(interaction, str(self.code.value).strip())


class RedeemPanelView(discord.ui.View):
    """Persistent panel with a single 'Redeem Code' button."""

    def __init__(self, cog: "Codes") -> None:
        super().__init__(timeout=None)
        self.cog = cog
        store = branding.store_button()
        if store is not None:
            self.add_item(store)

    @discord.ui.button(
        label="استبدل الكود", style=discord.ButtonStyle.success,
        emoji="🎟️", custom_id=REDEEM_BUTTON_ID,
    )
    async def redeem(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(RedeemModal(self.cog))


class RedemptionsView(discord.ui.View):
    """Owner-only paginated browser over the permanent redemption log."""

    PER_PAGE = 10

    def __init__(
        self,
        cog: "Codes",
        guild: discord.Guild | None,
        owner_id: int,
        user_id: int | None,
        since: str | None,
        until: str | None,
        total: int,
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.guild = guild
        self.owner_id = owner_id
        self.user_id = user_id
        self.since = since
        self.until = until
        self.total = total
        self.pages = max(1, (total + self.PER_PAGE - 1) // self.PER_PAGE)
        self.page = 0
        self._sync()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("❌ هذي القائمة مو لك.", ephemeral=True)
            return False
        return True

    def _sync(self) -> None:
        at_start = self.page <= 0
        at_end = self.page >= self.pages - 1
        self.first.disabled = self.prev.disabled = at_start
        self.last.disabled = self.next.disabled = at_end

    async def _show(self, interaction: discord.Interaction) -> None:
        rows = await self.cog.db.search_redemptions(
            self.user_id, self.since, self.until,
            self.PER_PAGE, self.page * self.PER_PAGE,
        )
        embed = self.cog._redemptions_embed(
            self.guild, rows, self.page, self.pages, self.total
        )
        self._sync()
        await interaction.response.edit_message(
            embed=embed, view=self, allowed_mentions=discord.AllowedMentions.none()
        )

    @discord.ui.button(emoji="⏮", style=discord.ButtonStyle.secondary)
    async def first(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = 0
        await self._show(interaction)

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        await self._show(interaction)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = min(self.pages - 1, self.page + 1)
        await self._show(interaction)

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary)
    async def last(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = self.pages - 1
        await self._show(interaction)


class Codes(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db
        # user_id -> deque[timestamp] of recent attempts within the window.
        self._attempts: dict[int, deque[float]] = defaultdict(deque)

    async def cog_load(self) -> None:
        # Re-register the persistent panel so the button survives a restart.
        self.bot.add_view(RedeemPanelView(self))
        self._prune_redeemed.start()

    def cog_unload(self) -> None:
        self._prune_redeemed.cancel()

    # ------------------------------------------------------------------ #
    # Housekeeping: auto-delete old redeemed codes so the list stays short
    # ------------------------------------------------------------------ #
    @tasks.loop(hours=6)
    async def _prune_redeemed(self) -> None:
        cutoff = (
            utilities.utc_now()
            - timedelta(days=config.CODE_REDEEMED_RETENTION_DAYS)
        ).isoformat()
        deleted = await self.db.delete_old_redeemed_codes(cutoff)
        if deleted:
            log.info(
                "Pruned %s redeemed code(s) older than %s days.",
                deleted, config.CODE_REDEEMED_RETENTION_DAYS,
            )
        expired = await self.db.delete_expired_codes(cutoff)
        if expired:
            log.info(
                "Pruned %s expired code(s) past %s days of retention.",
                expired, config.CODE_REDEEMED_RETENTION_DAYS,
            )
        # Also sweep the in-memory rate-limit tracker: it's keyed by user id
        # and would otherwise grow with every member who ever tried a code.
        now = time.monotonic()
        window = config.CODE_RATE_LIMIT_WINDOW
        for user_id in list(self._attempts):
            attempts = self._attempts[user_id]
            while attempts and now - attempts[0] >= window:
                attempts.popleft()
            if not attempts:
                del self._attempts[user_id]

    @_prune_redeemed.before_loop
    async def _before_prune(self) -> None:
        await self.bot.wait_until_ready()

    def _rate_limited(self, user_id: int) -> bool:
        now = time.monotonic()
        window = config.CODE_RATE_LIMIT_WINDOW
        attempts = self._attempts[user_id]
        while attempts and now - attempts[0] >= window:
            attempts.popleft()
        if len(attempts) >= config.CODE_RATE_LIMIT_MAX:
            return True
        attempts.append(now)
        return False

    # ------------------------------------------------------------------ #
    # Panel command
    # ------------------------------------------------------------------ #
    @app_commands.command(name="redeem-panel", description="انشر لوحة استبدال الأكواد (للإدارة).")
    @app_commands.default_permissions(administrator=True)
    @utilities.admin_only()
    async def redeem_panel(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="استبدل كود",
            description=(
                "هنا تقدر تستبدل **كل أنواع الأكواد** اللي يوفرها السيرفر: "
                "**رتب محدودة**، **كريدت**، **رومات حصرية**، وأكثر!\n\n"
                "عندك كود **NIGHTVOID**؟ اضغط الزر تحت واكتب الكود عشان تاخذ مكافأتك.\n"
                "الصيغة: `NIGHTVOID-` وبعدها **15 حرف/رقم**.\n\n"
                "تقدر **تشيك على الأكواد من الموقع** بعد."
            ),
            color=branding.BRAND,
        )
        utilities.brand_footer(embed, "الأكواد")
        if await utilities.post_panel(interaction, embed, RedeemPanelView(self)):
            await interaction.response.send_message("✅ تم نشر لوحة الاستبدال.", ephemeral=True)

    @app_commands.command(name="redeem", description="أسرع طريقة تستبدل كود (بدون اللوحة).")
    @app_commands.describe(code="الكود حقك — مثال: NIGHTVOID-AB12cd34EF9gh56")
    async def redeem(self, interaction: discord.Interaction, code: str) -> None:
        # Same redemption path as the panel's modal, just skipping the button.
        await self.process_redeem(interaction, code.strip())

    # ------------------------------------------------------------------ #
    # Admin: stats + adding codes from Discord
    # ------------------------------------------------------------------ #
    @app_commands.command(name="code-stats", description="إحصائية الأكواد: المستخدمة والمتبقية (للمالك فقط).")
    @app_commands.default_permissions(administrator=True)
    @utilities.owner_only()
    async def code_stats(self, interaction: discord.Interaction) -> None:
        total, used = await self.db.count_codes()
        left = total - used
        embed = discord.Embed(title="إحصائية الأكواد", color=branding.NEUTRAL)
        embed.add_field(name="الإجمالي", value=f"**{total:,}**", inline=True)
        embed.add_field(name="المستخدمة", value=f"**{used:,}**", inline=True)
        embed.add_field(name="المتبقية", value=f"**{left:,}**", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="list-codes", description="اعرض كل الأكواد الموجودة (للمالك فقط).")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        status="فلتر حسب الحالة (الكل افتراضياً)",
        role="(اختياري) اعرض فقط أكواد رتبة معينة",
    )
    @app_commands.choices(
        status=[
            app_commands.Choice(name="الكل", value="all"),
            app_commands.Choice(name="غير المستخدمة", value="unused"),
            app_commands.Choice(name="المستخدمة", value="used"),
        ]
    )
    @utilities.owner_only()
    async def list_codes(
        self,
        interaction: discord.Interaction,
        status: app_commands.Choice[str] | None = None,
        role: discord.Role | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        value = status.value if status else "all"
        redeemed = {"all": None, "unused": False, "used": True}[value]
        rows = await self.db.list_codes(redeemed, role.id if role else None)

        if not rows:
            scope = f" لرتبة {role.mention}" if role else ""
            await interaction.followup.send(
                f"ℹ️ ما فيه أكواد{scope}.", ephemeral=True
            )
            return

        guild = interaction.guild
        name_cache: dict[int, str] = {}
        now_iso = utilities.utc_now_iso()
        lines: list[str] = []
        for r in rows:
            reward_parts: list[str] = []
            if r["role_id"]:
                resolved = guild.get_role(r["role_id"]) if guild else None
                reward_parts.append(resolved.name if resolved else f"role {r['role_id']}")
            if r["credits"]:
                reward_parts.append(f"{r['credits']:,} credits")
            reward_label = " + ".join(reward_parts) if reward_parts else "nothing"
            if r["redeemed"]:
                when = (r["redeemed_at"] or "")[:10]
                who = await self._resolve_name(guild, r["redeemed_by"], name_cache)
                mark = f"used by {who} ({when})"
                if r["max_uses"] > 1:
                    mark = f"{r['uses']}/{r['max_uses']} used, last by {who} ({when})"
            elif r["expires_at"] and r["expires_at"] <= now_iso:
                mark = "expired"
            elif r["uses"] > 0:
                mark = f"{r['uses']}/{r['max_uses']} used"
            else:
                mark = "unused"
                if r["max_uses"] > 1:
                    mark = f"unused (x{r['max_uses']})"
            if r["expires_at"] and r["expires_at"] > now_iso:
                mark += f", expires {r['expires_at'][:10]}"
            lines.append(f"{r['code']}  ->  {reward_label}  |  {mark}")
        body = "\n".join(lines)
        header = f"الأكواد ({len(rows)})"

        # Short lists go inline; long ones overflow Discord's 2000-char limit, so
        # send them as a downloadable text file instead.
        block = f"**{header}**\n```\n{body}\n```"
        if len(block) <= 1990:
            await interaction.followup.send(block, ephemeral=True)
        else:
            buffer = io.BytesIO(body.encode("utf-8"))
            file = discord.File(buffer, filename="codes.txt")
            await interaction.followup.send(
                f"**{header}** — مرفقة في الملف تحت.", file=file, ephemeral=True
            )
        log.info("Owner %s listed %s codes (status=%s)", interaction.user, len(rows), value)

    # ------------------------------------------------------------------ #
    # Owner: searchable, paginated redemption history
    # ------------------------------------------------------------------ #
    @staticmethod
    def _day_start(date_str: str) -> str:
        """'2026-07-08' -> start-of-day ISO UTC. Raises ValueError on bad format."""
        d = datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return d.isoformat()

    @staticmethod
    def _day_end(date_str: str) -> str:
        """'2026-07-08' -> end-of-day ISO UTC. Raises ValueError on bad format."""
        d = datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, microsecond=999999, tzinfo=timezone.utc
        )
        return d.isoformat()

    def _redemptions_embed(
        self,
        guild: discord.Guild | None,
        rows: list,
        page: int,
        pages: int,
        total: int,
    ) -> discord.Embed:
        lines: list[str] = []
        for r in rows:
            reward_parts: list[str] = []
            if r["role_id"]:
                role = guild.get_role(r["role_id"]) if guild else None
                reward_parts.append(role.mention if role else f"رتبة `{r['role_id']}`")
            if r["credits"]:
                reward_parts.append(f"**{r['credits']:,}** كريدت")
            reward = " + ".join(reward_parts) if reward_parts else "—"
            when = (r["redeemed_at"] or "")[:10]
            lines.append(f"`{r['code']}`\n└ <@{r['user_id']}> • {reward} • {when}")
        embed = discord.Embed(
            title="سجل الاستبدالات",
            description="\n".join(lines) if lines else "ما فيه نتائج.",
            color=branding.NEUTRAL,
        )
        embed.set_footer(text=f"صفحة {page + 1}/{pages} • {total} استبدال")
        return embed

    @app_commands.command(name="redemptions", description="ابحث في سجل استبدال الأكواد (للمالك).")
    @app_commands.default_permissions(administrator=True)
    @app_commands.rename(date_from="from", date_to="to")
    @app_commands.describe(
        user="فلتر باستبدالات عضو معيّن",
        date_from="من تاريخ (YYYY-MM-DD)",
        date_to="إلى تاريخ (YYYY-MM-DD)",
    )
    @utilities.owner_only()
    async def redemptions(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> None:
        try:
            since = self._day_start(date_from) if date_from else None
            until = self._day_end(date_to) if date_to else None
        except ValueError:
            await interaction.response.send_message(
                "❌ صيغة التاريخ غلط. استخدم YYYY-MM-DD (مثال: 2026-07-08).",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        user_id = user.id if user else None
        total = await self.db.count_redemptions(user_id, since, until)
        if total == 0:
            await interaction.followup.send(
                "ℹ️ ما فيه استبدالات بهذي الفلاتر.", ephemeral=True
            )
            return

        view = RedemptionsView(
            self, interaction.guild, interaction.user.id, user_id, since, until, total
        )
        rows = await self.db.search_redemptions(
            user_id, since, until, RedemptionsView.PER_PAGE, 0
        )
        embed = self._redemptions_embed(interaction.guild, rows, 0, view.pages, total)
        await interaction.followup.send(
            embed=embed,
            view=view if view.pages > 1 else None,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        log.info(
            "Owner %s searched redemptions (user=%s, from=%s, to=%s, total=%s)",
            interaction.user, user_id, date_from, date_to, total,
        )

    @staticmethod
    def _parse_credit_input(credits: str | None) -> int:
        """Parse the optional 'credits' slash field (accepts 1k / 2.5m).

        Returns 0 when omitted. Raises ValueError carrying a ready Arabic error
        message on invalid or out-of-range input.
        """
        if credits is None:
            return 0
        try:
            amount = parse_amount(credits)
        except ValueError:
            raise ValueError("❌ مبلغ الكريدت غير صالح. اكتب رقم مثل `500` أو `1k`.")
        if not (1 <= amount <= CODE_CREDIT_MAX):
            raise ValueError(
                f"❌ مبلغ الكريدت لازم يكون بين 1 و {CODE_CREDIT_MAX:,}."
            )
        return amount

    async def _resolve_name(
        self, guild: discord.Guild | None, user_id: int | None, cache: dict[int, str]
    ) -> str:
        """Best-effort username for a redeemer id (mentions don't render in code
        blocks, so /list-codes shows plain names). Falls back to the raw id."""
        if not user_id:
            return "?"
        if user_id in cache:
            return cache[user_id]
        member = guild.get_member(user_id) if guild else None
        if member is not None:
            name = member.display_name
        else:
            user = self.bot.get_user(user_id)
            if user is None:
                try:
                    user = await self.bot.fetch_user(user_id)
                except discord.HTTPException:
                    user = None
            name = user.name if user else str(user_id)
        cache[user_id] = name
        return name

    @staticmethod
    def _reward_label(role: discord.Role | None, credits: int) -> str:
        """Human-readable description of what a code grants (role and/or credits)."""
        parts: list[str] = []
        if role is not None:
            parts.append(f"رتبة {role.mention}")
        if credits > 0:
            parts.append(f"**{credits:,}** كريدت")
        return " + ".join(parts) if parts else "لا شيء"

    @staticmethod
    def _parse_expiry(expires: str | None) -> str | None:
        """'30m' / '2h' / '7d' -> absolute ISO UTC expiry, or None when omitted.

        Raises ValueError carrying a ready Arabic error message on bad input.
        """
        if expires is None:
            return None
        try:
            delta = utilities.parse_duration(expires)
        except ValueError:
            raise ValueError(
                "❌ مدة الانتهاء غير صالحة. اكتب مثل `30m` أو `2h` أو `7d`."
            )
        return (utilities.utc_now() + delta).isoformat()

    @staticmethod
    def _code_terms(max_uses: int, expires_at: str | None) -> str:
        """Suffix describing a code's usage limit / expiry for confirmations."""
        parts: list[str] = []
        if max_uses > 1:
            parts.append(f"يستخدم **{max_uses}** مرة")
        if expires_at:
            ts = int(datetime.fromisoformat(expires_at).timestamp())
            parts.append(f"ينتهي <t:{ts}:R>")
        return (" — " + "، ".join(parts)) if parts else ""

    @app_commands.command(name="add-code", description="أضف كود يعطي رتبة و/أو كريدت (للمالك فقط).")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        role="(اختياري) الرتبة المربوطة بالكود",
        credits="(اختياري) عدد الكريدت اللي يعطيه الكود (يقبل 1k، الحد الأقصى 200k)",
        code="الكود (اختياري — يتولّد تلقائي لو تركته فاضي)",
        max_uses="(اختياري) كم شخص يقدر يستبدله (افتراضي 1، كل شخص مرة وحدة)",
        expires="(اختياري) مدة الصلاحية مثل 30m أو 2h أو 7d (افتراضي بدون انتهاء)",
    )
    @utilities.owner_only()
    async def add_code(
        self,
        interaction: discord.Interaction,
        role: discord.Role | None = None,
        credits: str | None = None,
        code: str | None = None,
        max_uses: app_commands.Range[int, 1, 1000] = 1,
        expires: str | None = None,
    ) -> None:
        try:
            credit_amount = self._parse_credit_input(credits)
            expires_at = self._parse_expiry(expires)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if role is None and credit_amount == 0:
            await interaction.response.send_message(
                "❌ لازم تحدد رتبة أو كريدت (أو الاثنين) للكود.", ephemeral=True
            )
            return

        if code:
            code = code.strip()
            if not CODE_PATTERN.match(code):
                await interaction.response.send_message(
                    "❌ صيغة الكود غلط. لازم يكون `NIGHTVOID-` وبعدها 15 حرف/رقم.",
                    ephemeral=True,
                )
                return
            generated = False
        else:
            code = generate_code()
            generated = True

        if not await self.db.add_code(
            code, role.id if role else 0, credit_amount, max_uses, expires_at
        ):
            await interaction.response.send_message(
                "❌ الكود هذا موجود من قبل. جرّب كود ثاني.", ephemeral=True
            )
            return

        note = "تم توليده تلقائياً" if generated else "تمت إضافته"
        terms = self._code_terms(max_uses, expires_at)
        await interaction.response.send_message(
            f"✅ {note} ويعطي {self._reward_label(role, credit_amount)}{terms}:\n```{code}```",
            ephemeral=True,
        )
        log.info(
            "Admin %s added code %s -> role %s, credits %s, max_uses %s, expires %s",
            interaction.user, code, role.id if role else 0, credit_amount,
            max_uses, expires_at,
        )

    @app_commands.command(name="generate-codes", description="ولّد عدة أكواد رتبة و/أو كريدت (للمالك فقط).")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        count="عدد الأكواد (1 إلى 50)",
        role="(اختياري) الرتبة المربوطة بالأكواد",
        credits="(اختياري) عدد الكريدت اللي يعطيه كل كود (يقبل 1k، الحد الأقصى 200k)",
        max_uses="(اختياري) كم شخص يقدر يستبدل كل كود (افتراضي 1)",
        expires="(اختياري) مدة الصلاحية مثل 30m أو 2h أو 7d (افتراضي بدون انتهاء)",
    )
    @utilities.owner_only()
    async def generate_codes(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 50],
        role: discord.Role | None = None,
        credits: str | None = None,
        max_uses: app_commands.Range[int, 1, 1000] = 1,
        expires: str | None = None,
    ) -> None:
        try:
            credit_amount = self._parse_credit_input(credits)
            expires_at = self._parse_expiry(expires)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if role is None and credit_amount == 0:
            await interaction.response.send_message(
                "❌ لازم تحدد رتبة أو كريدت (أو الاثنين) للأكواد.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        role_id = role.id if role else 0
        created: list[str] = []
        for _ in range(count):
            code = generate_code()
            # Retry on the astronomically unlikely collision.
            while not await self.db.add_code(
                code, role_id, credit_amount, max_uses, expires_at
            ):
                code = generate_code()
            created.append(code)

        body = "\n".join(created)
        terms = self._code_terms(max_uses, expires_at)
        await interaction.followup.send(
            f"✅ تم توليد **{count}** كود يعطي {self._reward_label(role, credit_amount)}{terms}:\n```{body}```",
            ephemeral=True,
        )
        log.info(
            "Admin %s generated %s codes -> role %s, credits %s, max_uses %s, expires %s",
            interaction.user, count, role_id, credit_amount, max_uses, expires_at,
        )

    @app_commands.command(name="delete-codes", description="احذف عدد من الأكواد غير المستخدمة (للمالك فقط).")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        amount="عدد الأكواد اللي تبي تحذفها",
        role="(اختياري) احذف فقط أكواد رتبة معينة",
    )
    @utilities.owner_only()
    async def delete_codes(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 10000],
        role: discord.Role | None = None,
    ) -> None:
        deleted = await self.db.delete_unused_codes(amount, role.id if role else None)
        if deleted == 0:
            scope = f" لرتبة {role.mention}" if role else ""
            await interaction.response.send_message(
                f"ℹ️ ما فيه أكواد غير مستخدمة تنحذف{scope}.", ephemeral=True
            )
            return

        scope = f" من رتبة {role.mention}" if role else ""
        await interaction.response.send_message(
            f"🗑️ تم حذف **{deleted}** كود غير مستخدم{scope}.", ephemeral=True
        )
        log.info(
            "Admin %s deleted %s unused codes (role=%s)",
            interaction.user, deleted, role.id if role else "any",
        )

    # ------------------------------------------------------------------ #
    # Redemption logic (driven by the modal)
    # ------------------------------------------------------------------ #
    async def process_redeem(self, interaction: discord.Interaction, code: str) -> None:
        await interaction.response.defer(ephemeral=True)
        user = interaction.user

        if self._rate_limited(user.id):
            await interaction.followup.send(
                f"⏳ محاولات كثيرة. حاول مرة ثانية خلال "
                f"{config.CODE_RATE_LIMIT_WINDOW} ثانية.",
                ephemeral=True,
            )
            return

        if not CODE_PATTERN.match(code):
            await interaction.followup.send(
                "❌ صيغة الكود غلط. لازم يكون `NIGHTVOID-` وبعدها 15 حرف/رقم.",
                ephemeral=True,
            )
            return

        row = await self.db.get_code(code)
        if row is None:
            await interaction.followup.send("❌ الكود هذا مو موجود.", ephemeral=True)
            return
        now_iso = utilities.utc_now_iso()
        if row["expires_at"] and row["expires_at"] <= now_iso:
            await interaction.followup.send(
                "❌ الكود هذا منتهية صلاحيته.", ephemeral=True
            )
            return
        if row["uses"] >= row["max_uses"]:
            await interaction.followup.send(
                "❌ الكود هذا مستبدل من قبل.", ephemeral=True
            )
            return
        # Multi-use codes: one redemption per person (checked against the
        # permanent log so it survives the code row's later auto-prune).
        if row["max_uses"] > 1 and await self.db.has_redeemed(code, user.id):
            await interaction.followup.send(
                "❌ استبدلت هذا الكود من قبل — كل شخص يستخدمه مرة وحدة.",
                ephemeral=True,
            )
            return

        credit_amount = row["credits"]
        role = interaction.guild.get_role(row["role_id"]) if row["role_id"] else None

        # A role code whose role was deleted can't be honoured.
        if row["role_id"] and role is None:
            log.error("Code %s linked to missing role %s", code, row["role_id"])
            await interaction.followup.send(
                "❌ الرتبة حق هذا الكود ما عادت موجودة. كلم الإدارة.",
                ephemeral=True,
            )
            return

        # Reject if the user already owns the role — without consuming the code,
        # so they can still use it on a different role.
        if role is not None and isinstance(user, discord.Member) and role in user.roles:
            await interaction.followup.send(
                f"❌ عندك رتبة **{role.name}** من قبل. لو تبي رتبة ثانية كلم الإدارة.\n"
                "**تقدر تختار رتبة من نفس فئة السعر والصلاحيات فقط، لا شي أكثر.**",
                ephemeral=True,
            )
            return

        # Atomic redemption, in two steps.
        # Multi-use codes: reserve this user's once-per-person slot FIRST (the
        # unique-index log insert), so two racing submissions from the same
        # user can't consume two uses. Then consume a use — which atomically
        # re-checks exhaustion and expiry inside the UPDATE. If the code ran
        # out in the race window, release the reserved slot.
        redeemed_at = now_iso
        log_id = None
        if row["max_uses"] > 1:
            log_id = await self.db.log_redemption(
                code, user.id, row["role_id"], credit_amount, redeemed_at
            )
            if log_id is None:
                await interaction.followup.send(
                    "❌ استبدلت هذا الكود من قبل — كل شخص يستخدمه مرة وحدة.",
                    ephemeral=True,
                )
                return

        if not await self.db.redeem_code(code, user.id, redeemed_at):
            if log_id is not None:
                await self.db.remove_redemption(log_id)
            await interaction.followup.send(
                "❌ الكود هذا مستبدل من قبل.", ephemeral=True
            )
            return

        # Single-use codes: redeem_code itself is the atomic gate; log after.
        if log_id is None:
            await self.db.log_redemption(
                code, user.id, row["role_id"], credit_amount, redeemed_at
            )

        # Grant credits first — an atomic upsert that can't fail on permissions.
        if credit_amount > 0:
            await self.db.add_credits(user.id, credit_amount)

        # Then the role, if any. Role assignment can fail on hierarchy/perms.
        role_failed = False
        if role is not None:
            role_failed = not await utilities.try_add_role(
                user, role, f"Redeemed code {code}"
            )

        if role_failed:
            extra = f" وأضفت لك **{credit_amount:,}** كريدت،" if credit_amount > 0 else ""
            await interaction.followup.send(
                f"⚠️ تم قبول كودك{extra} بس ما قدرت أعطيك الرتبة بسبب مشكلة صلاحيات. "
                "تم تنبيه الإدارة.",
                ephemeral=True,
            )
            return

        rewards: list[str] = []
        if role is not None:
            rewards.append(f"رتبة **{role.name}**")
        if credit_amount > 0:
            rewards.append(f"**{credit_amount:,}** كريدت")
        await interaction.followup.send(
            "✅ تم استبدال الكود! حصلت على " + " و".join(rewards) + ".",
            ephemeral=True,
        )
        log.info(
            "Code %s redeemed by %s -> role %s, credits %s",
            code, user, row["role_id"], credit_amount,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Codes(bot))
