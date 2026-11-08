import asyncio
from typing import TYPE_CHECKING
import logging
import os
import random
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from cogs.karaoke import KaraokeSession

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
    "options": "-vn",
}


@dataclass
class Track:
    title: str
    webpage_url: str
    duration: int | None
    requester: str
    karaoke: "KaraokeSession | None" = None


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
IDLE_LEAVE_SECONDS = 10  # leave the voice channel this long after the music ends (0 = right away)
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
        self.idle_task: asyncio.Task | None = None
        self.np_id: int | None = None  # id of our latest "Now playing" message, so we can edit it


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildState] = {}
        self.join_sound_file = Path(os.getenv("JOIN_SOUND_FILE") or "join.mp3")
        self.voice_locks: dict[int, asyncio.Lock] = {}
        self.play_locks: dict[int, asyncio.Lock] = {}

    def state(self, guild: discord.Guild) -> GuildState:
        return self.states.setdefault(guild.id, GuildState())

    # ---------- playback core ----------

    async def leave_after_idle(self, guild: discord.Guild):
        """Disconnect once the music has ended, unless something new starts in the meantime."""
        await asyncio.sleep(IDLE_LEAVE_SECONDS)
        vc = guild.voice_client
        state = self.states.get(guild.id)
        if vc is None or vc.is_playing() or vc.is_paused() or (state and state.queue):
            return
        self.states.pop(guild.id, None)
        await vc.disconnect()

    async def announce(self, state: GuildState, text: str, first: discord.WebhookMessage | None = None):
        """Show the 'Now playing' text while keeping the channel tidy.

        - `first`: the reply the command already posted ("Loading...", "Queued N songs..."), so the
          loading and now-playing text end up in one message.
        - otherwise, if our last "Now playing" message is still the newest message in the channel,
          edit it instead of posting another one.
        - otherwise post a new message.
        """
        channel = state.text_channel
        if channel is None:
            return
        try:
            if first is not None:
                await first.edit(content=text)
                state.np_id = first.id
                return
            if state.np_id and getattr(channel, "last_message_id", None) == state.np_id:
                await channel.get_partial_message(state.np_id).edit(content=text)
                return
        except discord.HTTPException as e:
            log.info("Couldn't edit the now-playing message, sending a new one: %s", e)
        try:
            msg = await channel.send(text)
            state.np_id = msg.id
        except discord.HTTPException as e:
            log.warning("Couldn't send the now-playing message: %s", e)

    async def play_next(
        self, guild: discord.Guild, first: discord.WebhookMessage | None = None,
        header: str = "", completed: Track | None = None,
    ):
        async with self.play_locks.setdefault(guild.id, asyncio.Lock()):
            state = self.states.get(guild.id)
            if state is None or (completed is not None and state.current is not completed):
                return
            vc = guild.voice_client
            if completed is None and vc and (vc.is_playing() or vc.is_paused()):
                return
            if state.idle_task:
                state.idle_task.cancel()
                state.idle_task = None
            while True:
                previous = state.current
                track = previous if state.loop and previous else (
                    state.queue.popleft() if state.queue else None
                )
                state.current = track
                if previous is not track and previous and previous.karaoke:
                    await previous.karaoke.finish()
                    if self.states.get(guild.id) is not state or state.current is not track:
                        return
                vc = guild.voice_client
                if vc is None:
                    await self.clear_tracks(state, "Disconnected")
                    return
                if track is None:
                    if header:
                        await self.announce(state, header.rstrip(), first)
                    state.idle_task = asyncio.create_task(self.leave_after_idle(guild))
                    return
                feature = "karaoke" if track.karaoke else "music"
                if not self.bot.features.enabled(guild.id, feature):
                    state.loop = False
                    continue
                source = None
                try:
                    stream_url = (
                        track.karaoke.audio_path if track.karaoke else
                        await asyncio.to_thread(resolve_stream, track.webpage_url)
                    )
                    # A stop, leave, or toggle may have happened during extraction.
                    if self.states.get(guild.id) is not state or state.current is not track:
                        return
                    options = {"options": "-vn"} if track.karaoke else FFMPEG_OPTS
                    audio = discord.FFmpegPCMAudio(stream_url, **options)
                    if track.karaoke:
                        audio = track.karaoke.wrap_source(audio)
                    source = discord.PCMVolumeTransformer(audio, volume=state.volume)

                    def after(err, played=track):
                        if err:
                            log.error("Player error: %s", err)
                        asyncio.run_coroutine_threadsafe(
                            self.play_next(guild, completed=played), self.bot.loop
                        )

                    vc.play(source, after=after)
                except Exception as error:
                    if source is not None:
                        source.cleanup()
                    log.warning("Failed to play %s: %s", track.webpage_url, error)
                    state.loop = False
                    header += f"Couldn't play **{track.title}**, skipping.\n"
                    continue
                if track.karaoke:
                    await track.karaoke.start()
                if first is not None or not state.loop:
                    await self.announce(
                        state,
                        f"{header}Now playing: **{track.title}** [{fmt_duration(track.duration)}] "
                        f"(requested by {track.requester})",
                        first,
                    )
                return

    async def clear_tracks(self, state: GuildState, reason: str):
        tracks = list(state.queue)
        if state.current:
            tracks.append(state.current)
        state.queue.clear()
        state.current = None
        state.loop = False
        if state.idle_task:
            state.idle_task.cancel()
            state.idle_task = None
        for track in tracks:
            if track.karaoke:
                await track.karaoke.finish(reason)

    async def disconnect(self, guild: discord.Guild, reason: str = "Disconnected"):
        state = self.states.pop(guild.id, None)
        vc = guild.voice_client
        if vc:
            vc.stop()
            if vc.source:
                vc.source.cleanup()
            await vc.disconnect()
        if state:
            await self.clear_tracks(state, reason)

    async def on_feature_toggle(self, guild: discord.Guild, feature: str, enabled: bool):
        if enabled or feature not in {"music", "karaoke"}:
            return
        state = self.states.get(guild.id)
        if state is None:
            return

        def matches(track):
            return bool(track.karaoke) == (feature == "karaoke")

        removed = [track for track in state.queue if matches(track)]
        state.queue = deque(track for track in state.queue if not matches(track))
        for track in removed:
            if track.karaoke:
                await track.karaoke.finish("Karaoke disabled")
        current = state.current
        if current and matches(current):
            state.loop = False
            vc = guild.voice_client
            if vc and (vc.is_playing() or vc.is_paused()):
                vc.stop()
            await self.play_next(guild, completed=current)

    async def enqueue_prepared(self, interaction: discord.Interaction, track: Track) -> bool:
        if not self.bot.features.enabled(interaction.guild.id, "karaoke"):
            await self.fail(interaction, "Karaoke was disabled while preparing this song.")
            return False
        voice = getattr(interaction.user, "voice", None)
        if not voice or not voice.channel:
            await self.fail(interaction, "Join a voice channel before starting karaoke.")
            return False
        vc = await self.join_voice(interaction.guild, voice.channel)
        if not self.bot.features.enabled(interaction.guild.id, "karaoke"):
            await self.fail(interaction, "Karaoke is disabled for this server.")
            return False
        state = self.state(interaction.guild)
        state.text_channel = interaction.channel
        state.queue.append(track)
        send = interaction.channel.send if interaction.is_expired() else interaction.followup.send
        if vc.is_playing() or vc.is_paused():
            try:
                await send(f"Queued karaoke: **{track.title}** (position {len(state.queue)})")
            except discord.HTTPException:
                log.warning("Could not announce queued karaoke", exc_info=True)
        else:
            first = None
            try:
                first = await send(f"Loading karaoke **{track.title}**...")
            except discord.HTTPException:
                log.warning("Could not announce loading karaoke", exc_info=True)
            await self.play_next(interaction.guild, first)
        return True

    async def play_join_sound(self, vc: discord.VoiceClient):
        """Finish the local arrival clip before /play starts using the audio player."""
        if not self.bot.features.enabled(vc.guild.id, "join_sound"):
            return
        if not self.join_sound_file.is_file():
            log.warning("Join sound file not found: %s; continuing without it", self.join_sound_file)
            return

        loop = asyncio.get_running_loop()
        finished = loop.create_future()

        def complete(error):
            if not finished.done():
                finished.set_result(error)

        def after(error):
            loop.call_soon_threadsafe(complete, error)

        source = None
        try:
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(str(self.join_sound_file), options="-vn"),
                volume=self.state(vc.guild).volume,
            )
            vc.play(source, after=after)
        except (OSError, discord.ClientException) as e:
            if source is not None:
                source.cleanup()
            log.warning("Couldn't start join sound %s: %s", self.join_sound_file, e)
            return
        try:
            error = await finished
            if error:
                log.warning("Join sound playback failed: %s", error)
        except asyncio.CancelledError:
            if vc.source is source:
                vc.stop()
            raise

    async def join_voice(self, guild: discord.Guild, channel: discord.abc.Connectable) -> discord.VoiceClient:
        # Other /play requests must wait for both the connection and its arrival clip.
        async with self.voice_locks.setdefault(guild.id, asyncio.Lock()):
            vc = guild.voice_client
            if vc is None:
                vc = await channel.connect()
                state = self.state(guild)
                if state.idle_task:
                    state.idle_task.cancel()
                    state.idle_task = None
                await self.play_join_sound(vc)
            elif vc.channel != channel:
                await vc.move_to(channel)
            return vc

    async def fail(self, interaction: discord.Interaction, text: str):
        """Tell only the person who ran the command that something went wrong.

        After a public defer() Discord ignores ephemeral on the first reply, so the "thinking..."
        message is deleted first and the error goes out as a fresh ephemeral follow-up."""
        if interaction.is_expired():
            await interaction.channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            return
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass
        await interaction.followup.send(text, ephemeral=True)

    # ---------- commands ----------

    @app_commands.command(name="joinsound", description="Toggle the arrival sound for this server")
    @app_commands.describe(enabled="Turn it on or off; omit to toggle")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def joinsound(self, interaction: discord.Interaction, enabled: bool | None = None):
        if not interaction.permissions.manage_channels:
            return await interaction.response.send_message(
                "You need Manage Channels to change the join sound.", ephemeral=True
            )
        guild_id = interaction.guild.id
        if enabled is None:
            enabled = not self.bot.features.enabled(guild_id, "join_sound")
        try:
            self.bot.features.set_enabled(guild_id, "join_sound", enabled)
        except OSError:
            log.exception("Couldn't save join sound settings")
            return await interaction.response.send_message(
                "Couldn't save the setting; nothing changed. Check the console.", ephemeral=True
            )
        text = f"Join sound is now **{'on' if enabled else 'off'}** for this server."
        if enabled and not self.join_sound_file.is_file():
            text += f" Add your audio file at `{self.join_sound_file}` before the next join."
        await interaction.response.send_message(text, ephemeral=True)

    async def enqueue(self, interaction: discord.Interaction, query: str):
        voice = getattr(interaction.user, "voice", None)
        if not voice or not voice.channel:
            return await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
        await interaction.response.defer()
        vc = await self.join_voice(interaction.guild, voice.channel)
        if is_playlist_url(query):
            return await self.enqueue_playlist(interaction, vc, query)
        try:
            info = await asyncio.to_thread(extract_info, query)
        except Exception as e:
            log.warning("Lookup failed for %r: %s", query, e)
            return await self.fail(interaction, "Couldn't find anything for that.")
        if not self.bot.features.enabled(interaction.guild.id, "music"):
            return await self.fail(interaction, "Music was disabled while looking up this song.")

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
            # this "Loading..." message is edited into the "Now playing" message, not followed by a second one
            first = await interaction.followup.send(f"Loading **{track.title}**...")
            await self.play_next(interaction.guild, first)

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
            return await self.fail(interaction, "Couldn't read that playlist. Is it a public playlist link?")

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
            return await self.fail(interaction, "Couldn't find any playable songs in that playlist.")
        if not self.bot.features.enabled(interaction.guild.id, "music"):
            return await self.fail(interaction, "Music was disabled while looking up this playlist.")

        state = self.state(interaction.guild)
        state.text_channel = interaction.channel
        state.queue.extend(tracks)

        header = f"Queued **{len(tracks)}** songs from **{title}**."
        if len(entries) >= MAX_PLAYLIST:
            header += f" (Limited to the first {MAX_PLAYLIST}.)"
        header += "\n"
        if vc.is_playing() or vc.is_paused():
            await interaction.followup.send(header.rstrip())
        else:
            # one message: the "Queued..." line plus the first song's "Now playing" line
            first = await interaction.followup.send(header + "Loading the first song...")
            await self.play_next(interaction.guild, first, header)

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
            await interaction.response.send_message("Skipped.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @app_commands.command(name="pause", description="Pause playback")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("Paused.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    @app_commands.command(name="resume", description="Resume playback")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("Nothing is paused.", ephemeral=True)

    @app_commands.command(name="stop", description="Stop playback and clear the queue")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        await interaction.response.defer()
        vc = interaction.guild.voice_client
        state = self.state(interaction.guild)
        # Invalidate callbacks before stop can schedule the next track.
        tracks = list(state.queue)
        if state.current:
            tracks.append(state.current)
        state.queue.clear()
        state.current = None
        state.loop = False
        if state.idle_task:
            state.idle_task.cancel()
        if vc:
            vc.stop()
            if vc.source:
                vc.source.cleanup()
        for track in tracks:
            if track.karaoke:
                await track.karaoke.finish("Stopped")
        state.idle_task = asyncio.create_task(self.leave_after_idle(interaction.guild))
        await interaction.followup.send("Stopped and cleared the queue.")

    @app_commands.command(name="leave", description="Disconnect from voice")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc is None:
            return await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)
        await interaction.response.defer()
        await self.disconnect(interaction.guild)
        await interaction.followup.send("Bye!")

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
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="nowplaying", description="Show the current song")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction):
        t = self.state(interaction.guild).current
        if not t:
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        await interaction.response.send_message(
            f"**{t.title}** [{fmt_duration(t.duration)}]\n{t.webpage_url}", ephemeral=True
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
        await interaction.response.send_message(f"Volume set to {level}%.", ephemeral=True)

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
        if self.bot.user and member.id == self.bot.user.id and before.channel and not after.channel:
            state = self.states.pop(member.guild.id, None)
            if state:
                await self.clear_tracks(state, "Disconnected")
            return
        vc = member.guild.voice_client
        if vc is None or vc.channel is None:
            return
        if not any(not m.bot for m in vc.channel.members):
            await self.disconnect(member.guild, "Everyone left")

    async def cog_unload(self):
        for guild_id in list(self.states):
            guild = self.bot.get_guild(guild_id)
            if guild:
                await self.disconnect(guild, "Bot shutting down")
            else:
                await self.clear_tracks(self.states.pop(guild_id), "Bot shutting down")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))