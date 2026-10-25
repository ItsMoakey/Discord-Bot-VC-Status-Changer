import logging
import os
import re

import discord
from discord.ext import commands

log = logging.getLogger("instagram_embed")

# Instagram no longer embeds in Discord, so posted links are re-posted with the host swapped
# for an embed proxy. Set EMBED_DOMAIN to point at your own self-hosted instance instead
# (e.g. "ig.itsmoakey.net").
EMBED_DOMAIN = os.getenv("EMBED_DOMAIN") or "fxinstagram.com"

# Matches post / reel / tv / share links. The lookbehind skips links wrapped in <...>,
# which is how users deliberately suppress an embed.
INSTAGRAM_LINK_PATTERN = re.compile(
    r"(?<!<)https?://(?:www\.)?instagram\.com/"
    r"((?:[A-Za-z0-9_.]+/)?(?:p|reels?|tv|share(?:/p|/reel)?)/[A-Za-z0-9_-]+)/?",
    re.IGNORECASE,
)


def find_instagram_embeds(text: str) -> list[str]:
    """Embeddable URLs for every Instagram post/reel link in `text`, de-duplicated in order."""
    urls = []
    for match in INSTAGRAM_LINK_PATTERN.finditer(text):
        url = f"https://{EMBED_DOMAIN}/{match.group(1)}/"
        if url not in urls:
            urls.append(url)
    return urls


class InstagramEmbed(commands.Cog):
    """Re-posts Instagram links through an embed proxy."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        urls = find_instagram_embeds(message.content)
        if not urls:
            return
        try:
            await message.reply("\n".join(urls), mention_author=False)
        except discord.HTTPException as e:
            log.warning("couldn't post Instagram embed in #%s: %s", message.channel, e)
            return
        # hide Instagram's own (empty) preview so the embed isn't doubled; needs Manage Messages
        try:
            await message.edit(suppress=True)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(InstagramEmbed(bot))
