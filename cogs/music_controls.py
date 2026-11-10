"""Persistent controls resolve the current cog and guild state on every interaction."""

import logging

import discord
from cogs.messages import PublicMessageError

log = logging.getLogger("musicbot")


class PlaybackInput(discord.ui.Modal):
    query = discord.ui.TextInput(
        label="Song URL or search", placeholder="Paste a song link or enter search words", max_length=1000
    )

    def __init__(self, *, karaoke: bool):
        super().__init__(title="Start karaoke" if karaoke else "Play a song")
        self.karaoke = karaoke

    async def on_submit(self, interaction: discord.Interaction):
        music = interaction.client.get_cog("Music")
        if music is None:
            return await interaction.response.send_message(
                embed=discord.Embed(description="The music service is unavailable."), ephemeral=True
            )
        feature = "karaoke" if self.karaoke else "music"
        if not await music.control_guard(interaction, feature=feature, allow_disconnected=True):
            return
        if self.karaoke:
            karaoke = interaction.client.get_cog("Karaoke")
            if karaoke is None:
                return await music.fail(interaction, "The karaoke service is unavailable.")
            await karaoke.karaoke.callback(karaoke, interaction, str(self.query).strip())
        else:
            await music.enqueue(interaction, str(self.query).strip())

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error("Playback input failed", exc_info=(type(error), error, error.__traceback__))
        music = interaction.client.get_cog("Music")
        if music:
            text = str(error) if isinstance(error, PublicMessageError) else "Couldn't start playback. Check voice/webhook permissions and the bot console."
            await music.fail(interaction, text)


class PlaybackButton(discord.ui.Button):
    def __init__(self, action: str, label: str, row: int, style=discord.ButtonStyle.secondary):
        super().__init__(label=label, custom_id=f"playback:{action}", row=row, style=style)
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        music = interaction.client.get_cog("Music")
        if music is None:
            return await interaction.response.send_message(
                embed=discord.Embed(description="The music service is unavailable."), ephemeral=True
            )
        await music.dispatch_control(interaction, self.action)


class PlaybackView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for action, label, row, style in (
            ("play", "Play", 0, discord.ButtonStyle.success),
            ("karaoke", "Karaoke", 0, discord.ButtonStyle.primary),
            ("queue", "Queue", 0, discord.ButtonStyle.secondary),
            ("pause_resume", "Pause / Resume", 1, discord.ButtonStyle.secondary),
            ("skip", "Skip", 1, discord.ButtonStyle.secondary),
            ("loop", "Loop", 1, discord.ButtonStyle.secondary),
            ("shuffle", "Shuffle", 2, discord.ButtonStyle.secondary),
            ("leave", "Leave", 2, discord.ButtonStyle.danger),
            ("controls", "Bring controls here", 3, discord.ButtonStyle.primary),
        ):
            self.add_item(PlaybackButton(action, label, row, style))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        log.error("Playback control failed", exc_info=(type(error), error, error.__traceback__))
        music = interaction.client.get_cog("Music")
        if music:
            text = str(error) if isinstance(error, PublicMessageError) else "Couldn't complete that playback action. Check voice/webhook permissions and the bot console."
            await music.fail(interaction, text)
