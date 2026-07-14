"""System 6 — Role shop.

Users spend credits to buy roles. The catalogue lives in the database
(`shop_items`) and is managed live by the owner via /shop-add and /shop-remove —
no file edit or redeploy needed. On first boot the table seeds itself once from
shop_config.json; after that the database is the source of truth.

/shop shows the catalogue with buy buttons; /buy deducts credits atomically and
assigns the role. The buy buttons are `DynamicItem`s matched by custom_id
pattern, so a role added to the shop today gets a working button immediately —
even on /shop messages posted before the last restart.
"""

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

import amounts
import branding
import config
import utilities

log = logging.getLogger("nightvoid.shop")


class BuyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"nv:shop:buy:(?P<role_id>\d+)",
):
    """Persistent buy button matched by custom_id pattern instead of being
    pre-registered per role. Any item — including one added live — routes here
    without a restart or a per-role view registration."""

    def __init__(self, role_id: int, label: str = "شراء") -> None:
        self.role_id = role_id
        super().__init__(
            discord.ui.Button(
                label=label[:80],
                style=discord.ButtonStyle.success,
                custom_id=f"nv:shop:buy:{role_id}",
            )
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: re.Match[str],
        /,
    ) -> "BuyButton":
        return cls(int(match["role_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Shop")
        if cog is None:
            await interaction.response.send_message(
                "❌ المتجر غير متاح حالياً. حاول بعد شوي.", ephemeral=True
            )
            return
        await cog.purchase_by_role(interaction, self.role_id)


def _build_view(items: list) -> discord.ui.View:
    """A view of buy buttons for the given catalogue rows (max 25 components)."""
    view = discord.ui.View(timeout=None)
    for item in items[:25]:
        view.add_item(BuyButton(item["role_id"], f"{item['name']} — {item['cost']:,}"))
    return view


class Shop(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db

    async def cog_load(self) -> None:
        # Seed the catalogue once from the JSON config; after the first boot the
        # DB is authoritative and this is a no-op.
        try:
            seeded = await self.db.seed_shop_items(
                config.SHOP_ITEMS, utilities.utc_now_iso()
            )
            if seeded:
                log.info("Seeded %s shop item(s) from shop_config.json.", seeded)
        except Exception:
            log.exception("Shop seed from config failed; continuing with DB contents.")
        # Register the template so buy-button clicks route even after a restart,
        # for any role_id. Guard against a double-register on cog reload.
        try:
            self.bot.add_dynamic_items(BuyButton)
        except ValueError:
            pass  # already registered in this process

    # ------------------------------------------------------------------ #
    # Purchase logic (shared by button + /buy)
    # ------------------------------------------------------------------ #
    async def purchase_by_role(self, interaction: discord.Interaction, role_id: int) -> None:
        """Look the item up fresh, then purchase. Handles a button clicked after
        the item was removed from the shop."""
        item = await self.db.get_shop_item(role_id)
        if item is None:
            await interaction.response.send_message(
                "❌ هذا العنصر ما عاد موجود في المتجر.", ephemeral=True
            )
            return
        await self.purchase(interaction, item)

    async def purchase(self, interaction: discord.Interaction, item) -> None:
        member = interaction.user
        role = interaction.guild.get_role(item["role_id"])
        if role is None:
            log.error("Shop item '%s' references missing role %s", item["name"], item["role_id"])
            await interaction.response.send_message(
                "❌ هذي الرتبة ما عادت موجودة. كلم الإدارة.", ephemeral=True
            )
            return

        if role in member.roles:
            await interaction.response.send_message(
                f"❌ الرتبة **{role.name}** عندك أصلاً.", ephemeral=True
            )
            return

        cost = item["cost"]
        balance = await self.db.get_balance(member.id)
        if balance < cost:
            await interaction.response.send_message(
                f"❌ كريدتك ما يكفي. رتبة **{role.name}** تكلّف {cost:,}، "
                f"وعندك {balance:,}.",
                ephemeral=True,
            )
            return

        # Atomic deduction guards against concurrent double-spend.
        if not await self.db.deduct_credits(member.id, cost):
            await interaction.response.send_message(
                "❌ كريدتك ما يكفي (الرصيد تغيّر). حاول مرة ثانية.", ephemeral=True
            )
            return

        # Re-check ownership AFTER the deduct: two rapid concurrent clicks for
        # the same role both clear the pre-check above, then both deduct —
        # charging twice for a role add_roles only grants once. If a racing
        # click already granted it, refund this charge and bail.
        if role in member.roles:
            await self.db.add_credits(member.id, cost)
            await interaction.response.send_message(
                f"❌ الرتبة **{role.name}** عندك أصلاً (رجّعنا لك كريدتك).",
                ephemeral=True,
            )
            return

        if not await utilities.try_add_role(
            member, role, f"Purchased via role shop ({cost} credits)"
        ):
            # Refund on assignment failure (hierarchy/permissions/HTTP error).
            await self.db.add_credits(member.id, cost)
            log.error("Refunded %s credits to %s for shop role %s.", cost, member, role.id)
            await interaction.response.send_message(
                "⚠️ فشلت العملية بسبب مشكلة صلاحيات. رجّعنا لك كريدتك. "
                "تم تنبيه الإدارة.",
                ephemeral=True,
            )
            return

        await self.db.add_purchase(
            member.id, "role", item["name"], cost, utilities.utc_now_iso()
        )
        # Re-read instead of computing balance - cost: the pre-check balance is
        # stale if the user earned/spent concurrently.
        new_balance = await self.db.get_balance(member.id)
        await interaction.response.send_message(
            f"✅ اشتريت **{role.name}** بـ {cost:,} كريدت. "
            f"باقي رصيدك: {new_balance:,}.",
            ephemeral=True,
        )
        log.info("%s purchased role %s for %s credits", member, role.id, cost)

    # ------------------------------------------------------------------ #
    # Member commands
    # ------------------------------------------------------------------ #
    @app_commands.command(name="shop", description="اعرض متجر الرتب.")
    async def shop(self, interaction: discord.Interaction) -> None:
        items = await self.db.list_shop_items()
        if not items:
            await interaction.response.send_message(
                "🛒 المتجر فاضي حالياً.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title="متجر رتب Night Void",
            description="اصرف كريدتك على الرتب تحت.",
            color=branding.BRAND,
        )
        utilities.brand_footer(embed, "متجر الرتب")
        for item in items[:25]:
            embed.add_field(
                name=f"{item['name']} — {item['cost']:,} كريدت",
                value=item["description"] or "​",
                inline=False,
            )
        await interaction.response.send_message(
            embed=embed, view=_build_view(items), ephemeral=True
        )

    async def _item_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        current = current.lower()
        items = await self.db.list_shop_items()
        return [
            app_commands.Choice(name=item["name"], value=str(item["role_id"]))
            for item in items
            if current in item["name"].lower()
        ][:25]

    @app_commands.command(name="buy", description="اشترِ رتبة من المتجر.")
    @app_commands.describe(role="الرتبة اللي تبي تشتريها")
    @app_commands.autocomplete(role=_item_autocomplete)
    async def buy(self, interaction: discord.Interaction, role: str) -> None:
        try:
            role_id = int(role)
        except ValueError:
            await interaction.response.send_message(
                "❌ اختر رتبة من الاقتراحات.", ephemeral=True
            )
            return
        item = await self.db.get_shop_item(role_id)
        if item is None:
            await interaction.response.send_message(
                "❌ هذي الرتبة مو موجودة في المتجر.", ephemeral=True
            )
            return
        await self.purchase(interaction, item)

    # ------------------------------------------------------------------ #
    # Owner catalogue management (live — no redeploy)
    # ------------------------------------------------------------------ #
    @app_commands.command(
        name="shop-add", description="أضف أو عدّل رتبة في المتجر (للمالك فقط)."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        role="الرتبة اللي تنباع",
        cost="السعر بالكريدت (يقبل 1k / 2.5m)",
        name="(اختياري) الاسم المعروض — الافتراضي اسم الرتبة",
        description="(اختياري) وصف مختصر يظهر في المتجر",
    )
    @utilities.owner_only()
    async def shop_add(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        cost: str,
        name: str | None = None,
        description: str | None = None,
    ) -> None:
        try:
            price = amounts.parse_amount(cost)
        except ValueError:
            await interaction.response.send_message(
                "❌ السعر غير صالح. اكتب رقم مثل `10000` أو `1k`.", ephemeral=True
            )
            return
        if price <= 0:
            await interaction.response.send_message(
                "❌ السعر لازم يكون أكبر من صفر.", ephemeral=True
            )
            return

        display_name = (name or role.name).strip()
        desc = (description or "").strip()
        newly_added = await self.db.upsert_shop_item(
            role.id, display_name, price, desc, utilities.utc_now_iso()
        )

        # Warn (but still save) if the bot can't actually hand out this role, so
        # the price isn't set on something purchases will refund on.
        warn = ""
        if not role.is_assignable():
            warn = (
                "\n⚠️ تنبيه: ما أقدر أعطي هذي الرتبة (فوق رتبتي أو مُدارة). "
                "ارفع رتبتي فوقها عشان الشراء يشتغل."
            )
        verb = "أُضيفت" if newly_added else "تم تعديلها"
        await interaction.response.send_message(
            f"✅ {verb} **{display_name}** بسعر {price:,} كريدت.{warn}", ephemeral=True
        )
        log.info(
            "Owner %s %s shop item role=%s cost=%s",
            interaction.user, "added" if newly_added else "edited", role.id, price,
        )

    @app_commands.command(
        name="shop-remove", description="احذف رتبة من المتجر (للمالك فقط)."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(item="العنصر اللي تبي تشيله من المتجر")
    @app_commands.autocomplete(item=_item_autocomplete)
    @utilities.owner_only()
    async def shop_remove(self, interaction: discord.Interaction, item: str) -> None:
        try:
            role_id = int(item)
        except ValueError:
            await interaction.response.send_message(
                "❌ اختر عنصر من الاقتراحات.", ephemeral=True
            )
            return
        row = await self.db.get_shop_item(role_id)
        if row is None:
            await interaction.response.send_message(
                "❌ هذا العنصر مو موجود في المتجر.", ephemeral=True
            )
            return
        await self.db.remove_shop_item(role_id)
        await interaction.response.send_message(
            f"✅ حذفت **{row['name']}** من المتجر.", ephemeral=True
        )
        log.info("Owner %s removed shop item role=%s", interaction.user, role_id)

    @app_commands.command(
        name="shop-list", description="اعرض عناصر المتجر وأسعارها (للمالك فقط)."
    )
    @app_commands.default_permissions(administrator=True)
    @utilities.owner_only()
    async def shop_list(self, interaction: discord.Interaction) -> None:
        items = await self.db.list_shop_items()
        if not items:
            await interaction.response.send_message("🛒 المتجر فاضي.", ephemeral=True)
            return
        lines = [
            f"• **{item['name']}** — {item['cost']:,} كريدت  (`{item['role_id']}`)"
            for item in items
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Shop(bot))
