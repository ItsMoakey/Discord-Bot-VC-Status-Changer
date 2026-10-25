import logging
import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

## A Discord bot that updates voice channel statuses to show custom emojis for who's currently in them,
## re-posts Instagram links through an embed proxy, and plays music in voice channels.
## Each feature is a cog in ./cogs, loaded below. Remove a line from EXTENSIONS to turn a feature off.
## Set the token as DISCORD_TOKEN in your environment or in a .env file (see .env.example).

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

EXTENSIONS = [
    "cogs.vc_status",
    "cogs.instagram_embed",
    "cogs.music",
]


class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.members = True          # privileged: enable "Server Members Intent" in the Developer Portal
        intents.message_content = True  # privileged: enable "Message Content Intent" (Instagram links)
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        for ext in EXTENSIONS:
            await self.load_extension(ext)

        dev_guild = os.getenv("DEV_GUILD_ID")
        if dev_guild:  # guild sync is instant, global sync can take a while
            guild = discord.Object(id=int(dev_guild))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
        else:
            synced = await self.tree.sync()
        log.info("Synced %d command(s): %s", len(synced), [c.name for c in synced])

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id)


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set DISCORD_TOKEN in your environment or .env file.")
    Bot().run(token)
