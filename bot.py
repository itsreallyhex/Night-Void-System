"""Night Void Store — entry point.

Responsibilities are intentionally narrow: configure logging, open the database
connection, load every cog, sync the slash-command tree, and report startup
state. All feature logic lives in the cogs.
"""

import asyncio
import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

import config
from database import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nightvoid")

COGS = (
    "cogs.tickets",
    "cogs.codes",
    "cogs.reviews",
    "cogs.announcements",
    "cogs.credits",
    "cogs.shop",
    "cogs.services",
    "cogs.owner",
)


def discover_extras() -> list[str]:
    """Every .py file in cogs/extras/ is a drop-in extension — no registration
    needed here. Names starting with `_` (like _template.py) are skipped."""
    extras_dir = Path(__file__).parent / "cogs" / "extras"
    return sorted(
        f"cogs.extras.{p.stem}"
        for p in extras_dir.glob("*.py")
        if not p.name.startswith("_")
    )


class NightVoidBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True   # credit message earning
        intents.members = True           # role checks / member fetch
        intents.voice_states = True      # voice credit earning
        super().__init__(
            command_prefix=commands.when_mentioned_or(config.COMMAND_PREFIX),
            intents=intents,
            help_command=None,
            # Pin ownership to a single account. Without this, is_owner() treats
            # EVERY member of the app's Developer-Portal team as an owner, which
            # let a team-member admin run the owner-only code commands.
            owner_id=config.OWNER_USER_ID,
        )
        self.db = Database(config.DATABASE_PATH)

    async def setup_hook(self) -> None:
        await self.db.connect()
        log.info("Database connected at %s", config.DATABASE_PATH)

        for ext in (*COGS, *discover_extras()):
            try:
                await self.load_extension(ext)
                log.info("Loaded cog: %s", ext)
            except Exception:
                log.exception("Failed to load cog: %s", ext)

        self.tree.on_error = self.on_app_command_error

        guild = discord.Object(id=config.GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        try:
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d application commands to guild %s", len(synced), config.GUILD_ID)
        except discord.Forbidden:
            # 50001 Missing Access: the app was invited without the
            # applications.commands OAuth2 scope. Don't crash-loop the whole
            # bot over it — buttons, panels and prefix commands still work.
            log.error(
                "Could not sync slash commands to guild %s (403 Missing Access). "
                "Re-invite the bot with BOTH scopes: "
                "https://discord.com/oauth2/authorize?client_id=%s"
                "&scope=bot+applications.commands — slash commands stay "
                "unavailable until then; everything else keeps working.",
                config.GUILD_ID, self.application_id,
            )
        except discord.HTTPException:
            log.exception(
                "Command sync to guild %s failed; continuing startup without it.",
                config.GUILD_ID,
            )

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        # Unknown text command / failed owner check: stay silent.
        if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
            return
        # Anything handled by a cog-level handler won't reach here.
        log.exception("Prefix command error in %s", ctx.command, exc_info=error)

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        # Permission gates (utilities.owner_only / admin_only / staff_only)
        # already sent their own ephemeral rejection — stay silent.
        if isinstance(error, app_commands.CheckFailure):
            return
        log.exception("Slash command error in %s", interaction.command, exc_info=error)
        message = "⚠️ صار فيه خطأ وأنا أشغّل الأمر. تم تنبيه الإدارة."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_ready(self) -> None:
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        log.info("Serving %d guild(s)", len(self.guilds))

    async def close(self) -> None:
        await self.db.close()
        await super().close()


async def main() -> None:
    bot = NightVoidBot()
    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutdown requested by operator.")
