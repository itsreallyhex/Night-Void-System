"""Copy me to add a new drop-in command.

1. Copy this file to something like `cogs/extras/mycommand.py`
   (no leading underscore — underscore files are NOT loaded).
2. Rename the cog class and write your commands.
3. Restart the bot. That's it — bot.py finds and loads it automatically.

Prefix commands appear instantly; slash commands sync on startup.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import branding
import config
import utilities

log = logging.getLogger("nightvoid.extras.template")


class Template(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db = bot.db  # shared database, same as every other cog

    # ---- prefix command example (owner-only, silent to everyone else) ----
    @commands.command(name="example")
    @commands.is_owner()
    async def example(self, ctx: commands.Context) -> None:
        await ctx.send("it works!")

    # ---- slash command example (admin-only) ----
    @app_commands.command(name="example", description="مثال — احذفني")
    @app_commands.default_permissions(administrator=True)
    @utilities.admin_only()
    async def example_slash(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("it works!", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Template(bot))
