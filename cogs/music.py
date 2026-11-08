import asyncio
import json
from typing import TYPE_CHECKING
import logging
import os
import random
import tempfile
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

from cogs.messages import PublicMessageError
from cogs.music_controls import PlaybackInput, PlaybackView

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
        self.text_channel: discord.abc.Messageable | None = None
        self.idle_task: asyncio.Task | None = None
        self.panel: discord.WebhookMessage | None = None
        self.notice = ""


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: dict[int, GuildState] = {}
        self.join_sound_file = Path(os.getenv("JOIN_SOUND_FILE") or "join.mp3")
        self.voice_locks: dict[int, asyncio.Lock] = {}
        self.play_locks: dict[int, asyncio.Lock] = {}
        self.panel_locks: dict[int, asyncio.Lock] = {}
        self.playback_view = PlaybackView()
        self.panel_file = Path(os.getenv("PLAYBACK_PANEL_FILE") or "playback_panels.json")
        try:
            self.panel_refs = json.loads(self.panel_file.read_text(encoding="utf-8")) if self.panel_file.exists() else {}
        except (OSError, ValueError):
            log.exception("Couldn't load saved playback panel references")
            self.panel_refs = {}

    async def restore_panel(self, guild, state):
        reference = self.panel_refs.get(str(guild.id))
        if state.panel is not None or not reference:
            return
        try:
            channel = self.bot.get_channel(reference["channel_id"]) or await self.bot.fetch_channel(reference["channel_id"])
            if channel.guild.id != guild.id:
                return
            _, hook = await self.bot.messages.webhook(channel)
            kwargs = {"thread": channel} if isinstance(channel, discord.Thread) else {}
            state.panel = await hook.fetch_message(reference["message_id"], **kwargs)
            if state.text_channel is None:
                state.text_channel = channel
        except (discord.HTTPException, PublicMessageError, KeyError, TypeError):
            log.info("Saved playback panel is unavailable for guild %s", guild.id)

    def remember_panel(self, guild_id, panel):
        reference = {"channel_id": panel.channel.id, "message_id": panel.id}
        if self.panel_refs.get(str(guild_id)) == reference:
            return
        references = {**self.panel_refs, str(guild_id): reference}
        self.panel_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.panel_file.parent,
                prefix=f".{self.panel_file.name}.", suffix=".tmp", delete=False,
            ) as stream:
                temporary = stream.name
                json.dump(references, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.panel_file)
            temporary = None
            self.panel_refs = references
        finally:
            if temporary is not None:
                os.unlink(temporary)

    async def cog_load(self):
        self.bot.add_view(self.playback_view)

    async def control_guard(
        self, interaction: discord.Interaction, *, feature: str | None = None,
        mutation: bool = True, allow_disconnected: bool = False,
    ) -> bool:
        guild = interaction.guild
        if guild is None:
            await self.fail(interaction, "Playback controls are only available in a server.")
            return False
        enabled = self.bot.features.enabled
        allowed = enabled(guild.id, feature) if feature else (
            enabled(guild.id, "music") or enabled(guild.id, "karaoke")
        )
        if not allowed:
            await self.fail(interaction, f"{feature.capitalize() if feature else 'Music and karaoke'} is disabled. A member with Manage Server can enable it with `/toggle`.")
            return False
        if not mutation:
            return True
        voice = getattr(interaction.user, "voice", None)
        vc = guild.voice_client
        if not voice or not voice.channel:
            await self.fail(interaction, "Join my voice channel before changing playback.")
            return False
        if vc is None:
            if allow_disconnected:
                return True
            await self.fail(interaction, "I'm not in a voice channel. Use Play or Karaoke to start.")
            return False
        if vc.channel != voice.channel:
            await self.fail(interaction, "You must be in my voice channel to change playback.")
            return False
        return True

    async def defer(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)

    async def dispatch_control(self, interaction: discord.Interaction, action: str):
        if action in {"play", "karaoke"}:
            feature = "music" if action == "play" else "karaoke"
            if await self.control_guard(interaction, feature=feature, allow_disconnected=True):
                await interaction.response.send_modal(PlaybackInput(karaoke=action == "karaoke"))
            return
        if not await self.control_guard(interaction, mutation=action not in {"queue", "controls"}):
            return
        await self.defer(interaction)
        if action == "pause_resume":
            vc = interaction.guild.voice_client
            action = "resume" if vc and vc.is_paused() else "pause"
        command = getattr(self, action)
        await command.callback(self, interaction)

    def dashboard_embed(self, guild: discord.Guild) -> discord.Embed:
        return self.panel_embed(guild, self.state(guild))

    def panel_embed(self, guild: discord.Guild, state: GuildState) -> discord.Embed:
        vc = guild.voice_client
        active = vc and (vc.is_playing() or vc.is_paused())
        status = "Disconnected" if vc is None else (
            "Paused" if vc.is_paused() else "Now playing" if active else "Idle"
        )
        lines = []
        if state.notice:
            lines.append(state.notice)
        if active and state.current:
            track = state.current
            lines.append(f"**{track.title}** [{fmt_duration(track.duration)}]\nRequested by {track.requester}")
        elif not active:
            lines.append("Use Play or Karaoke to add a song.")
        embed = discord.Embed(title=f"Playback · {status}", description="\n\n".join(lines)[:4096])
        embed.add_field(name="Loop", value="On" if state.loop else "Off")
        embed.add_field(name="Queued", value=str(len(state.queue)))
        if vc and vc.channel:
            embed.set_footer(text=f"Voice: {vc.channel.name}")
        return embed

    async def refresh_panel(self, guild: discord.Guild, state: GuildState | None = None):
        state = state or self.states.get(guild.id)
        if state is None or state.panel is None:
            return
        try:
            await self.bot.messages.edit(
                state.panel, embed=self.panel_embed(guild, state), view=self.playback_view
            )
        except PublicMessageError as error:
            log.warning("Couldn't refresh playback panel: %s", error)

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
        if state:
            state.idle_task = None
        await self.disconnect(guild)

    async def announce(self, state: GuildState, text: str, first: discord.WebhookMessage | None = None):
        """Keep one canonical panel, reusing it while songs change."""
        channel = state.text_channel
        if channel is None:
            return
        async with self.panel_locks.setdefault(channel.guild.id, asyncio.Lock()):
            await self.restore_panel(channel.guild, state)
            await self.update_panel(state, text, first)

    async def update_panel(self, state: GuildState, text: str, first: discord.WebhookMessage | None):
        channel = state.text_channel
        state.notice = text
        guild = channel.guild
        previous = state.panel
        panel = previous if previous and previous.channel.id == channel.id else first
        if panel is not None:
            try:
                await self.bot.messages.edit(
                    panel, embed=self.panel_embed(guild, state), view=self.playback_view
                )
            except PublicMessageError as error:
                log.info("Couldn't edit playback panel: %s", error)
                panel = None
        if panel is None:
            try:
                panel = await self.bot.messages.send(
                    channel, embed=self.panel_embed(guild, state), view=self.playback_view
                )
            except PublicMessageError as error:
                log.warning("Couldn't publish playback panel: %s", error)
                return
        state.panel = panel
        self.remember_panel(guild.id, panel)
        obsolete = {message.id: message for message in (previous, first)
                    if message is not None and message.id != panel.id}
        for message in obsolete.values():
            try:
                await self.bot.messages.delete(message)
            except PublicMessageError as error:
                log.warning("Couldn't remove obsolete playback message: %s", error)

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
                    await self.refresh_panel(guild, state)
                    return
                if track is None:
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
                    # A leave or feature toggle may have happened during extraction.
                    if self.states.get(guild.id) is not state or state.current is not track:
                        return
                    options = {"options": "-vn"} if track.karaoke else FFMPEG_OPTS
                    source = discord.FFmpegPCMAudio(stream_url, **options)
                    if track.karaoke:
                        source = track.karaoke.wrap_source(source)

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
                await self.announce(state, header.rstrip(), first)
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
            state.notice = reason
            await self.refresh_panel(guild, state)

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
        await self.refresh_panel(guild, state)

    async def enqueue_prepared(self, interaction: discord.Interaction, track: Track) -> bool:
        if not await self.control_guard(interaction, feature="karaoke", allow_disconnected=True):
            return False
        voice = interaction.user.voice
        vc = await self.join_voice(interaction.guild, voice.channel)
        if not await self.control_guard(interaction, feature="karaoke"):
            return False
        try:
            first = await self.bot.messages.send(
                interaction.channel,
                f"{'Queued' if vc.is_playing() or vc.is_paused() else 'Loading'} karaoke: **{track.title}**",
            )
        except PublicMessageError as error:
            await self.fail(interaction, str(error))
            return False
        if not await self.control_guard(interaction, feature="karaoke"):
            return False
        state = self.state(interaction.guild)
        state.text_channel = interaction.channel
        vc = interaction.guild.voice_client
        state.queue.append(track)
        if vc.is_playing() or vc.is_paused():
            await self.refresh_panel(interaction.guild, state)
        else:
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
            source = discord.FFmpegPCMAudio(str(self.join_sound_file), options="-vn")
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
                raise PublicMessageError("You must be in my voice channel to change playback.")
            return vc

    async def fail(self, interaction: discord.Interaction, text: str):
        """Errors never become public, including after an interaction expires."""
        try:
            await self.bot.messages.reply(interaction, text, ephemeral=True)
        except discord.HTTPException:
            log.warning("Couldn't deliver a private playback response: %s", text, exc_info=True)

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        original = getattr(error, "original", error)
        if isinstance(original, PublicMessageError):
            return await self.fail(interaction, str(original))
        log.error("Music command failed", exc_info=(type(original), original, original.__traceback__))
        await self.fail(interaction, "Couldn't complete playback. Check the bot's voice/webhook permissions and console.")

    # ---------- commands ----------
    @app_commands.command(name="controls", description="Post a fresh playback dashboard")
    @app_commands.guild_only()
    async def controls(self, interaction: discord.Interaction):
        if not await self.control_guard(interaction, mutation=False):
            return
        await self.defer(interaction)
        async with self.panel_locks.setdefault(interaction.guild.id, asyncio.Lock()):
            state = self.state(interaction.guild)
            await self.restore_panel(interaction.guild, state)
            previous = state.panel
            source = getattr(interaction, "message", None)
            source_is_panel = source is not None and any(
                (getattr(component, "custom_id", None) or "").startswith("playback:")
                for row in source.components for component in getattr(row, "children", [])
            )
            panel = await self.bot.messages.reply(
                interaction, embed=self.dashboard_embed(interaction.guild), view=self.playback_view
            )
            if panel is not None:
                state.text_channel = interaction.channel
                state.panel = panel
                self.remember_panel(interaction.guild.id, panel)
                obsolete = {message.id: message for message in (
                    previous, source if source_is_panel else None
                ) if message is not None and message.id != panel.id}
                for message in obsolete.values():
                    try:
                        await self.bot.messages.delete(message)
                    except PublicMessageError as error:
                        await self.fail(interaction, str(error))


    @app_commands.command(name="joinsound", description="Toggle the arrival sound for this server")
    @app_commands.describe(enabled="Turn it on or off; omit to toggle")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def joinsound(self, interaction: discord.Interaction, enabled: bool | None = None):
        if not interaction.permissions.manage_channels:
            return await self.fail(interaction, "You need Manage Channels to change the join sound.")
        guild_id = interaction.guild.id
        if enabled is None:
            enabled = not self.bot.features.enabled(guild_id, "join_sound")
        try:
            self.bot.features.set_enabled(guild_id, "join_sound", enabled)
        except OSError:
            log.exception("Couldn't save join sound settings")
            return await self.fail(interaction, "Couldn't save the setting; nothing changed. Check the console.")
        text = f"Join sound is now **{'on' if enabled else 'off'}** for this server."
        if enabled and not self.join_sound_file.is_file():
            text += f" Add your audio file at `{self.join_sound_file}` before the next join."
        await self.bot.messages.reply(interaction, text, ephemeral=True)

    async def enqueue(self, interaction: discord.Interaction, query: str):
        if not await self.control_guard(interaction, feature="music", allow_disconnected=True):
            return
        await self.defer(interaction)
        if not await self.control_guard(interaction, feature="music", allow_disconnected=True):
            return
        await self.join_voice(interaction.guild, interaction.user.voice.channel)
        if is_playlist_url(query):
            return await self.enqueue_playlist(interaction, query)
        try:
            info = await asyncio.to_thread(extract_info, query)
        except Exception as e:
            log.warning("Lookup failed for %r: %s", query, e)
            return await self.fail(interaction, "Couldn't find anything for that.")
        if not await self.control_guard(interaction, feature="music"):
            return

        track = Track(
            title=info.get("title", "Unknown"),
            webpage_url=info.get("webpage_url") or info.get("url"),
            duration=info.get("duration"),
            requester=interaction.user.display_name,
        )
        await self.enqueue_tracks(interaction, [track], f"Queued: **{track.title}**")

    @app_commands.command(name="play", description="Play a song from a URL or search term")
    @app_commands.describe(query="Song link, playlist link, or search words")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str):
        await self.enqueue(interaction, query)

    async def enqueue_tracks(self, interaction: discord.Interaction, tracks: list[Track], header: str):
        if not await self.control_guard(interaction, feature="music"):
            return
        vc = interaction.guild.voice_client
        loading = not (vc.is_playing() or vc.is_paused())
        first = await self.bot.messages.reply(interaction, header + "\nLoading..." if loading else header)
        if first is None:
            return
        if not await self.control_guard(interaction, feature="music"):
            return
        state = self.state(interaction.guild)
        state.text_channel = interaction.channel
        state.queue.extend(tracks)
        vc = interaction.guild.voice_client
        active = vc.is_playing() or vc.is_paused()
        if active:
            if loading:
                try:
                    await self.bot.messages.edit(first, header)
                except PublicMessageError as error:
                    await self.fail(interaction, str(error))
            await self.refresh_panel(interaction.guild, state)
        else:
            await self.play_next(interaction.guild, first)


    async def enqueue_playlist(self, interaction: discord.Interaction, url: str):
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
        if not await self.control_guard(interaction, feature="music"):
            return

        header = f"Queued **{len(tracks)}** songs from **{title}**."
        if len(entries) >= MAX_PLAYLIST:
            header += f" (Limited to the first {MAX_PLAYLIST}.)"
        await self.enqueue_tracks(interaction, tracks, header)

    @app_commands.command(name="move", description="Move a song already in the queue (default: to position 1, next up)")
    @app_commands.describe(position="Current position of the song in /queue", to="New position (default 1 = plays next)")
    @app_commands.guild_only()
    async def move(
        self,
        interaction: discord.Interaction,
        position: app_commands.Range[int, 1],
        to: app_commands.Range[int, 1] = 1,
    ):
        if not await self.control_guard(interaction):
            return
        await self.defer(interaction)
        state = self.state(interaction.guild)
        items = list(state.queue)
        if not items:
            return await self.fail(interaction, "The queue is empty.")
        if position > len(items):
            return await self.fail(interaction, f"There's no song at position {position} (queue has {len(items)}).")
        to = min(to, len(items))
        track = items.pop(position - 1)
        items.insert(to - 1, track)
        state.queue = deque(items)
        where = "next up" if to == 1 else f"position {to}"
        await self.bot.messages.reply(interaction, f"Moved **{track.title}** to {where}.")
        await self.refresh_panel(interaction.guild, state)

    @app_commands.command(name="skip", description="Skip the current song")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction):
        if not await self.control_guard(interaction):
            return
        await self.defer(interaction)
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            state = self.state(interaction.guild)
            state.loop = False  # otherwise loop would replay the skipped track
            vc.stop()
            await self.bot.messages.reply(interaction, "Skipped.", ephemeral=True)
            await self.refresh_panel(interaction.guild, state)
        else:
            await self.fail(interaction, "Nothing is playing.")

    @app_commands.command(name="pause", description="Pause playback")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
        if not await self.control_guard(interaction):
            return
        await self.defer(interaction)
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await self.bot.messages.reply(interaction, "Paused.", ephemeral=True)
            await self.refresh_panel(interaction.guild)
        else:
            await self.fail(interaction, "Nothing is playing.")

    @app_commands.command(name="resume", description="Resume playback")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction):
        if not await self.control_guard(interaction):
            return
        await self.defer(interaction)
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await self.bot.messages.reply(interaction, "Resumed.", ephemeral=True)
            await self.refresh_panel(interaction.guild)
        else:
            await self.fail(interaction, "Nothing is paused.")

    @app_commands.command(name="leave", description="Disconnect from voice")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction):
        if not await self.control_guard(interaction):
            return
        await self.defer(interaction)
        await self.disconnect(interaction.guild)
        await self.bot.messages.reply(interaction, "Disconnected from voice.")

    @app_commands.command(name="queue", description="Show the queue")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction):
        if not await self.control_guard(interaction, mutation=False):
            return
        state = self.state(interaction.guild)
        if not state.current and not state.queue:
            return await self.fail(interaction, "The queue is empty.")
        lines = []
        if state.current:
            lines.append(f"**Now:** {state.current.title} [{fmt_duration(state.current.duration)}]")
        for i, t in enumerate(list(state.queue)[:10], 1):
            lines.append(f"{i}. {t.title} [{fmt_duration(t.duration)}]")
        if len(state.queue) > 10:
            lines.append(f"...and {len(state.queue) - 10} more")
        await self.bot.messages.reply(interaction, "\n".join(lines), ephemeral=True)

    @app_commands.command(name="nowplaying", description="Show the current song")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction):
        if not await self.control_guard(interaction, mutation=False):
            return
        t = self.state(interaction.guild).current
        if not t:
            return await self.fail(interaction, "Nothing is playing.")
        await self.bot.messages.reply(
            interaction, f"**{t.title}** [{fmt_duration(t.duration)}]\n{t.webpage_url}", ephemeral=True
        )

    @app_commands.command(name="loop", description="Toggle looping the current song")
    @app_commands.guild_only()
    async def loop(self, interaction: discord.Interaction):
        if not await self.control_guard(interaction):
            return
        await self.defer(interaction)
        state = self.state(interaction.guild)
        state.loop = not state.loop
        await self.bot.messages.reply(interaction, f"Loop is now {'on' if state.loop else 'off'}.")
        await self.refresh_panel(interaction.guild, state)

    @app_commands.command(name="shuffle", description="Shuffle the queue")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction):
        if not await self.control_guard(interaction):
            return
        await self.defer(interaction)
        state = self.state(interaction.guild)
        items = list(state.queue)
        random.shuffle(items)
        state.queue = deque(items)
        await self.bot.messages.reply(interaction, "Queue shuffled.")
        await self.refresh_panel(interaction.guild, state)

    # ---------- housekeeping ----------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Leave when everyone else has left the channel."""
        if self.bot.user and member.id == self.bot.user.id and before.channel and not after.channel:
            state = self.states.pop(member.guild.id, None)
            if state:
                await self.clear_tracks(state, "Disconnected")
                state.notice = "Disconnected"
                await self.refresh_panel(member.guild, state)
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
        self.playback_view.stop()


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))