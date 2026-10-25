import asyncio
import logging
import random
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlparse

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("musicbot")

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}
FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -af dynaudnorm=f=150:g=15",
}


@dataclass
class Track:
    title: str
    webpage_url: str
    duration: int | None
    requester: str


def fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return "live/unknown"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def extract_info(query: str) -> dict:
    """Blocking: look up a URL or search term, return the first result's info."""
    with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
        info = ydl.extract_info(query, download=False)
    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise ValueError("No results found.")
        info = entries[0]
    return info


def resolve_stream(webpage_url: str) -> str:
    """Blocking: get a fresh direct stream URL (they expire, so do it at play time)."""
    return extract_info(webpage_url)["url"]


MAX_PLAYLIST = 50
UNAVAILABLE_TITLES = {"[Private video]", "[Deleted video]"}


def is_playlist_url(query: str) -> bool:
    """True for pure playlist links (YouTube /playlist?list=..., SoundCloud /sets/...).
    A normal video link with &list=... on it is NOT a playlist link and plays just that video."""
    if not query.startswith(("http://", "https://")):
        return False
    u = urlparse(query)
    host = u.netloc.lower()
    if "youtube.com" in host:
        return u.path.startswith("/playlist")
    if "soundcloud.com" in host:
        return "/sets/" in u.path
    return False


def extract_playlist(url: str) -> tuple[str, list[dict]]:
    """Blocking: list a playlist's entries (titles/links only, no stream URLs) up to MAX_PLAYLIST."""
    opts = {**YDL_OPTS, "noplaylist": False, "extract_flat": "in_playlist", "playlistend": MAX_PLAYLIST}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = [e for e in (info.get("entries") or []) if e]
    if not entries:
        raise ValueError("No songs found in that playlist.")
    return info.get("title") or "Playlist", entries


class GuildState:
    def __init__(self):
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.loop = False
        self.volume = 0.5
        self.text_channel: discord.abc.Messageable | None = None


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildState] = {}

    def state(self, guild: discord.Guild) -> GuildState:
        return self.states.setdefault(guild.id, GuildState())

    # ---------- playback core ----------

    async def play_next(self, guild: discord.Guild):
        state = self.state(guild)
        vc = guild.voice_client
        if vc is None:
            state.current = None
            state.queue.clear()
            return

        if state.loop and state.current:
            track = state.current
        elif state.queue:
            track = state.queue.popleft()
        else:
            state.current = None
            return

        try:
            stream_url = await asyncio.to_thread(resolve_stream, track.webpage_url)
        except Exception as e:
            log.warning("Failed to resolve %s: %s", track.webpage_url, e)
            state.current = None
            if state.text_channel:
                await state.text_channel.send(f"Couldn't play **{track.title}**, skipping.")
            return await self.play_next(guild)

        state.current = track
        source = discord.PCMVolumeTransformer(
            discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTS), volume=state.volume
        )

        def after(err):
            if err:
                log.error("Player error: %s", err)
            asyncio.run_coroutine_threadsafe(self.play_next(guild), self.bot.loop)

        vc.play(source, after=after)
        if state.text_channel and not state.loop:
            await state.text_channel.send(
                f"Now playing: **{track.title}** [{fmt_duration(track.duration)}] "
                f"(requested by {track.requester})"
            )

    async def ensure_voice(self, interaction: discord.Interaction) -> discord.VoiceClient | None:
        user_vc = getattr(interaction.user, "voice", None)
        if not user_vc or not user_vc.channel:
            await interaction.followup.send("Join a voice channel first.", ephemeral=True)
            return None
        vc = interaction.guild.voice_client
        if vc is None:
            vc = await user_vc.channel.connect()
        elif vc.channel != user_vc.channel:
            await vc.move_to(user_vc.channel)
        return vc

    # ---------- commands ----------

    async def enqueue(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()
        vc = await self.ensure_voice(interaction)
        if vc is None:
            return
        if is_playlist_url(query):
            return await self.enqueue_playlist(interaction, vc, query)
        try:
            info = await asyncio.to_thread(extract_info, query)
        except Exception as e:
            log.warning("Lookup failed for %r: %s", query, e)
            return await interaction.followup.send("Couldn't find anything for that.")

        track = Track(
            title=info.get("title", "Unknown"),
            webpage_url=info.get("webpage_url") or info.get("url"),
            duration=info.get("duration"),
            requester=interaction.user.display_name,
        )
        state = self.state(interaction.guild)
        state.text_channel = interaction.channel
        state.queue.append(track)

        if vc.is_playing() or vc.is_paused():
            await interaction.followup.send(
                f"Queued: **{track.title}** (position {len(state.queue)})"
            )
        else:
            await interaction.followup.send(f"Loading **{track.title}**...")
            await self.play_next(interaction.guild)

    @app_commands.command(name="play", description="Play a song from a URL or search term")
    @app_commands.describe(query="Song link, playlist link, or search words")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str):
        await self.enqueue(interaction, query)

    async def enqueue_playlist(self, interaction: discord.Interaction, vc: discord.VoiceClient, url: str):
        try:
            title, entries = await asyncio.to_thread(extract_playlist, url)
        except Exception as e:
            log.warning("Playlist lookup failed for %r: %s", url, e)
            return await interaction.followup.send("Couldn't read that playlist. Is it a public playlist link?")

        tracks = []
        for e in entries:
            link = e.get("webpage_url") or e.get("url")
            if not link or e.get("title") in UNAVAILABLE_TITLES:
                continue  # skip private/deleted/unplayable entries
            tracks.append(
                Track(
                    title=e.get("title") or "Unknown",
                    webpage_url=link,
                    duration=e.get("duration"),
                    requester=interaction.user.display_name,
                )
            )
        if not tracks:
            return await interaction.followup.send("Couldn't find any playable songs in that playlist.")

        state = self.state(interaction.guild)
        state.text_channel = interaction.channel
        state.queue.extend(tracks)

        msg = f"Queued **{len(tracks)}** songs from **{title}**."
        if len(entries) >= MAX_PLAYLIST:
            msg += f" (Limited to the first {MAX_PLAYLIST}.)"
        await interaction.followup.send(msg)
        if not (vc.is_playing() or vc.is_paused()):
            await self.play_next(interaction.guild)

    @app_commands.command(name="move", description="Move a song already in the queue (default: to position 1, next up)")
    @app_commands.describe(position="Current position of the song in /queue", to="New position (default 1 = plays next)")
    @app_commands.guild_only()
    async def move(
        self,
        interaction: discord.Interaction,
        position: app_commands.Range[int, 1],
        to: app_commands.Range[int, 1] = 1,
    ):
        state = self.state(interaction.guild)
        items = list(state.queue)
        if not items:
            return await interaction.response.send_message("The queue is empty.", ephemeral=True)
        if position > len(items):
            return await interaction.response.send_message(
                f"There's no song at position {position} (queue has {len(items)}).", ephemeral=True
            )
        to = min(to, len(items))
        track = items.pop(position - 1)
        items.insert(to - 1, track)
        state.queue = deque(items)
        where = "next up" if to == 1 else f"position {to}"
        await interaction.response.send_message(f"Moved **{track.title}** to {where}.")

    @app_commands.command(name="skip", description="Skip the current song")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            state = self.state(interaction.guild)
            state.loop = False  # otherwise loop would replay the skipped track
            vc.stop()
            await interaction.response.send_message("Skipped.")
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @app_commands.command(name="pause", description="Pause playback")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("Paused.")
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume playback")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("Resumed.")
        else:
            await interaction.response.send_message("Nothing is paused.", ephemeral=True)

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        state = self.state(interaction.guild)
        state.queue.clear()
        state.loop = False
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        await interaction.response.send_message("Stopped and cleared the queue.")

    @app_commands.command(name="leave", description="Disconnect from voice")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc is None:
            return await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        self.states.pop(interaction.guild.id, None)
        await vc.disconnect()
        await interaction.response.send_message("Bye!")

    @app_commands.command(name="queue", description="Show the queue")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction):
        state = self.state(interaction.guild)
        if not state.current and not state.queue:
            return await interaction.response.send_message("The queue is empty.", ephemeral=True)
        lines = []
        if state.current:
            lines.append(f"**Now:** {state.current.title} [{fmt_duration(state.current.duration)}]")
        for i, t in enumerate(list(state.queue)[:10], 1):
            lines.append(f"{i}. {t.title} [{fmt_duration(t.duration)}]")
        if len(state.queue) > 10:
            lines.append(f"...and {len(state.queue) - 10} more")
        await interaction.response.send_message("\n".join(lines))

    @app_commands.command(name="nowplaying", description="Show the current song")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction):
        t = self.state(interaction.guild).current
        if not t:
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        await interaction.response.send_message(
            f"**{t.title}** [{fmt_duration(t.duration)}]\n{t.webpage_url}"
        )

    @app_commands.command(name="volume", description="Set volume (0-100)")
    @app_commands.describe(level="Volume from 0 to 100")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]):
        state = self.state(interaction.guild)
        state.volume = level / 100
        vc = interaction.guild.voice_client
        if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = state.volume
        await interaction.response.send_message(f"Volume set to {level}%.")

    @app_commands.command(name="loop", description="Toggle looping the current song")
    @app_commands.guild_only()
    async def loop(self, interaction: discord.Interaction):
        state = self.state(interaction.guild)
        state.loop = not state.loop
        await interaction.response.send_message(f"Loop is now {'on' if state.loop else 'off'}.")

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction):
        state = self.state(interaction.guild)
        items = list(state.queue)
        random.shuffle(items)
        state.queue = deque(items)
        await interaction.response.send_message("Queue shuffled.")

    # ---------- housekeeping ----------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Leave when everyone else has left the channel."""
        vc = member.guild.voice_client
        if vc is None or vc.channel is None:
            return
        if not any(not m.bot for m in vc.channel.members):
            self.states.pop(member.guild.id, None)
            await vc.disconnect()


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))