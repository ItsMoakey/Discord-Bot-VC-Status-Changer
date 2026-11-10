"""Optional karaoke separation, with cancellable preparation and LRCLIB lyrics."""

from __future__ import annotations

import asyncio
import bisect
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from cogs.music import Track
from cogs.messages import PublicMessageError

log = logging.getLogger("musicbot.karaoke")
LRCLIB_URL = "https://lrclib.net/api/search"
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
STAMP = re.compile(r"\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]")
DECORATION = re.compile(
    r"\s*[\[(][^\])]*(?:official|lyrics?|audio|video|visuali[sz]er|hd|4k)[^\])]*[\])]",
    re.IGNORECASE,
)


class PreparationError(Exception):
    """An error with a safe, actionable explanation for the command's user."""


def youtube_video_url(value: str) -> str:
    """Canonicalize only actual YouTube video links, never arbitrary download URLs."""
    try:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme.lower() not in {"https", "http"}
            or parsed.hostname not in {
                "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"
            }
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
        ):
            raise ValueError
        parts = parsed.path.strip("/").split("/")
        if parsed.hostname == "youtu.be" and len(parts) == 1:
            video_id = parts[0]
        elif parsed.hostname != "youtu.be" and parsed.path.rstrip("/") == "/watch":
            values = parse_qs(parsed.query).get("v", [])
            if len(values) != 1:
                raise ValueError
            video_id = values[0]
        elif parsed.hostname != "youtu.be" and len(parts) == 2 and parts[0] in {"shorts", "live", "embed"}:
            video_id = parts[1]
        else:
            raise ValueError
        if not VIDEO_ID.fullmatch(video_id):
            raise ValueError
    except (ValueError, TypeError):
        raise PreparationError(
            "Use a normal YouTube video link (youtube.com/watch?v=… or youtu.be/…). "
            "Playlist-only links and other sites are not supported."
        ) from None
    return f"https://www.youtube.com/watch?v={video_id}"


def safe_text(value: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(value))


def clean_title(value: str) -> str:
    value = DECORATION.sub("", value)
    return re.sub(r"\s+", " ", value).strip(" -–—|")


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.findall(r"[^\W_]+", value, re.UNICODE))


def similarity(left: str, right: str) -> float:
    left, right = normalized(left), normalized(right)
    if not left or not right:
        return 0.0
    return max(
        SequenceMatcher(None, left, right).ratio(),
        SequenceMatcher(None, " ".join(sorted(left.split())), " ".join(sorted(right.split()))).ratio(),
    )


def parse_lrc(value: str) -> list[tuple[float, str]]:
    """Parse global millisecond offset, multiple timestamps and decimal fractions."""
    offsets = re.findall(r"\[offset:\s*([+-]?\d+)\s*\]", value, re.IGNORECASE)
    offset = int(offsets[-1]) / 1000 if offsets else 0.0
    lines: list[tuple[float, str]] = []
    for raw in value.splitlines():
        stamps = list(STAMP.finditer(raw))
        if not stamps:
            continue
        text = STAMP.sub("", raw).strip()
        text = re.sub(r"<\d+:\d{1,2}(?:[.:]\d{1,3})?>", "", text).strip()
        if not text:
            continue
        for stamp in stamps:
            minutes, seconds, fraction = stamp.groups()
            if int(seconds) >= 60:
                continue
            time = int(minutes) * 60 + int(seconds)
            if fraction:
                time += int(fraction) / 10 ** len(fraction)
            lines.append((max(0.0, time + offset), text))
    return sorted(set(lines), key=lambda line: line[0])


def lyric_pages(lines: list[str]) -> list[str]:
    """Keep pages comfortably below Discord's embed and field limits, even for long lines."""
    pages: list[str] = []
    current: list[str] = []
    size = 0
    for raw in lines:
        # Split before escaping, then bound the escaped result as well.
        escaped = safe_text(raw) or "\u200b"
        while escaped:
            chunk, escaped = escaped[:900], escaped[900:]
            if current and (len(current) >= 12 or size + len(chunk) + 1 > 3000):
                pages.append("\n".join(current))
                current, size = [], 0
            current.append(chunk)
            size += len(chunk) + 1
    if current:
        pages.append("\n".join(current))
    return pages


@dataclass
class Lyrics:
    synced: list[tuple[float, str]] = field(default_factory=list)
    plain: str = ""
    status: str = "No confidently matching lyrics were found on LRCLIB. Instrumental playback is still available."


async def lookup_lyrics(info: dict) -> Lyrics:
    """Reject loose search hits rather than singing an unrelated song."""
    video_title = clean_title(str(info.get("title") or ""))
    artist = str(info.get("artist") or "").strip()
    track = str(info.get("track") or "").strip()
    if not track and re.search(r"\s[-–—]\s", video_title):
        inferred_artist, inferred_track = re.split(r"\s[-–—]\s", video_title, maxsplit=1)
        artist, track = artist or inferred_artist, inferred_track
    # An uploader/channel is not necessarily the recording artist.
    expected = clean_title(track) if track else video_title
    duration = info.get("duration")
    queries: list[dict[str, str]] = []
    if artist and expected:
        queries.append({"artist_name": artist, "track_name": expected})
    if video_title:
        queries.append({"q": video_title})
    candidates: list[tuple[float, dict, bool]] = []
    failed = False
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "DiscordVCKaraoke/1.0"}) as client:
            for query in queries:
                try:
                    async with client.get(LRCLIB_URL, params=query) as response:
                        response.raise_for_status()
                        results = await response.json()
                    if not isinstance(results, list):
                        failed = True
                        continue
                    for item in results:
                        if not isinstance(item, dict):
                            continue
                        result_track = str(item.get("trackName") or "")
                        title_score = similarity(expected, result_track)
                        artist_score = similarity(artist, str(item.get("artistName") or "")) if artist else 0.0
                        if title_score < 0.83 or (artist and artist_score < 0.72):
                            continue
                        # A different-length music video can still use the same plain lyrics,
                        # but must never inherit that recording's timestamps.
                        result_duration = item.get("duration")
                        timing_matches = False
                        duration_score = 0.0
                        if (
                            isinstance(duration, (float, int)) and duration > 0
                            and isinstance(result_duration, (float, int))
                        ):
                            difference = abs(duration - result_duration)
                            tolerance = max(10.0, min(25.0, duration * 0.07))
                            timing_matches = difference <= tolerance
                            duration_score = max(0.0, 1 - difference / tolerance)
                        if not timing_matches and (title_score < 0.95 or artist_score < 0.9):
                            continue
                        if not artist and title_score < 0.95:
                            continue
                        if item.get("plainLyrics") or item.get("syncedLyrics"):
                            score = title_score * 0.6 + artist_score * 0.25 + duration_score * 0.15
                            candidates.append((score, item, timing_matches))
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    failed = True
                    log.info("LRCLIB lookup failed", exc_info=True)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        failed = True
        log.info("LRCLIB connection failed", exc_info=True)
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    if candidates:
        # Prefer usable synchronization among comparably close matches, not a worse recording.
        best_score = candidates[0][0]
        comparable = [item for score, item, timing_matches in candidates
                      if timing_matches and score >= best_score - 0.06]
        for item in comparable:
            synced = parse_lrc(str(item.get("syncedLyrics") or ""))
            within_audio = not isinstance(duration, (int, float)) or not synced or synced[-1][0] <= duration + 10
            if len(synced) >= 2 and synced[-1][0] > synced[0][0] and within_audio:
                plain = str(item.get("plainLyrics") or "") or "\n".join(text for _, text in synced)
                return Lyrics(synced, plain, "Synchronized lyrics from LRCLIB")
        for _, item, _ in candidates:
            plain = str(item.get("plainLyrics") or "").strip()
            if not plain:
                # Unusable timing need not discard the lyric text itself.
                plain = "\n".join(
                    STAMP.sub("", line).strip()
                    for line in str(item.get("syncedLyrics") or "").splitlines()
                    if STAMP.search(line)
                ).strip()
                plain = re.sub(r"<\d+:\d{1,2}(?:[.:]\d{1,3})?>", "", plain)
            if plain:
                return Lyrics([], plain, "Plain lyrics from LRCLIB; synchronized lyrics are unavailable.")
    if failed:
        return Lyrics(status="Lyrics lookup was unavailable or no safe match was found. Instrumental playback will continue without lyrics.")
    return Lyrics()


# Use the same download defaults as music, but in a killable interpreter rather
# than a thread: cancellation must not leave a download writing into a deleted directory.
DOWNLOAD_WORKER = r'''
import json, sys
from pathlib import Path
import yt_dlp
from cogs.music import YDL_OPTS
root = Path(sys.argv[2])
def finite_video(info, *, incomplete=False):
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        return "Live streams and upcoming videos cannot be prepared as karaoke."
options = {**YDL_OPTS, "default_search": "error", "noplaylist": True,
           "allowed_extractors": ["youtube"], "outtmpl": str(root / "source.%(ext)s"),
           "overwrites": True, "match_filter": finite_video}
with yt_dlp.YoutubeDL(options) as ydl:
    info = ydl.extract_info(sys.argv[1], download=True)
    if not info or info.get("_type") == "playlist" or info.get("is_live"):
        raise RuntimeError("A finite YouTube video is required; live streams/playlists are unsupported.")
    downloads = info.get("requested_downloads") or []
    audio = downloads[0].get("filepath") if downloads else None
    audio = audio or ydl.prepare_filename(info)
    data = {key: info.get(key) for key in ("title", "track", "artist", "duration", "webpage_url")}
    data["download_path"] = audio
    (root / "metadata.json").write_text(json.dumps(data), encoding="utf-8")
'''


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if os.name == "nt":
        # Kill FFmpeg/data-loader descendants too, before TemporaryDirectory cleanup.
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(process.pid), "/T", "/F",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


async def run_process(args: list[str], root: Path, stage: str) -> None:
    """No duration cap; drain output to disk and reap the entire child tree on cancellation."""
    output_path = root / f"{stage}.log"
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with output_path.open("wb") as output:
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *args, stdout=output, stderr=asyncio.subprocess.STDOUT,
            cwd=Path(__file__).resolve().parents[1], **flags,
        ))
        process = None
        try:
            process = await asyncio.shield(spawn)
            code = await process.wait()
        except asyncio.CancelledError:
            # Shield process creation too: cancellation between spawn and assignment must not orphan it.
            while not spawn.done():
                try:
                    await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    continue
            process = process or spawn.result()
            cleanup = asyncio.create_task(terminate_process(process))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
            raise
        except FileNotFoundError:
            raise PreparationError(
                "FFmpeg was not found. Install FFmpeg and put ffmpeg on the bot's PATH, then try again."
                if stage == "ffmpeg" else f"Could not start the {stage} worker. Check the bot's Python installation."
            ) from None
    if code:
        with output_path.open("rb") as output:
            output.seek(0, 2)
            output.seek(max(0, output.tell() - 5000))
            details = output.read().decode("utf-8", errors="replace")
        log.error("Karaoke %s failed (exit %s): %s", stage, code, details)
        if stage == "separation":
            raise PreparationError(
                "Instrumental separation failed. Use the documented environment for KARAOKE_BACKEND "
                "with matching PyTorch and audio dependencies, and ensure FFmpeg is on PATH. "
                "The first run downloads model weights and needs internet access "
                "and free disk/RAM. Check KARAOKE_DEVICE and the console for the actual backend error; "
                "cuda requires compatible CUDA/ROCm PyTorch and drivers (Vulkan is unsupported). The original vocals were not played."
            )
        if stage == "ffmpeg":
            raise PreparationError("FFmpeg could not decode this audio. Check the bot's FFmpeg installation and console error.")
        raise PreparationError(
            "YouTube audio could not be downloaded. The video may be private, restricted, live, or unavailable; "
            "check the console and update yt-dlp if YouTube extraction is failing."
        )


async def prepare_audio(url: str, root: Path) -> tuple[dict, str]:
    await run_process([sys.executable, "-c", DOWNLOAD_WORKER, url, str(root)], root, "download")
    info = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    source = Path(info["download_path"]).resolve()
    if not source.is_relative_to(root.resolve()) or not source.is_file():
        raise PreparationError("The YouTube download did not produce a usable local audio file.")
    wav = root / "input.wav"
    await run_process([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source), "-vn", "-ar", "44100", "-ac", "2", str(wav),
    ], root, "ffmpeg")
    await run_process([
        sys.executable, str(Path(__file__).with_name("karaoke_worker.py")), str(root / "stems"), str(wav),
    ], root, "separation")
    instrumental = root / "stems" / "no_vocals.wav"
    if not instrumental.is_file() or instrumental.stat().st_size <= 44:
        raise PreparationError("Separation finished without a usable no_vocals.wav. Check its console output; no original audio was queued.")
    return info, str(instrumental)


class ClockedPCM(discord.AudioSource):
    def __init__(self, source: discord.AudioSource, session: KaraokeSession):
        self.source = source
        self.session = session
        self.cleaned = False
        self.cleanup_lock = threading.Lock()

    def read(self) -> bytes:
        data = self.source.read()
        # Discord's PCM stream is 48 kHz, stereo, signed 16-bit (192000 bytes/s).
        # AudioPlayer does not call read while paused: no wall-clock drift.
        self.session.pcm_bytes += len(data)
        return data

    def is_opus(self) -> bool:
        return False

    def cleanup(self) -> None:
        with self.cleanup_lock:
            if not self.cleaned:
                self.cleaned = True
                self.source.cleanup()


class LyricsView(discord.ui.View):
    def __init__(self, session: KaraokeSession):
        super().__init__(timeout=None)
        self.session = session
        self.refresh()

    def refresh(self) -> None:
        session = self.session
        self.previous.disabled = session.finished or session.live or session.page <= 0
        self.next_page.disabled = session.finished or session.live or session.page >= len(session.pages) - 1
        self.mode.disabled = session.finished or not bool(session.lyrics.synced)
        self.mode.label = "Full lyrics" if session.live else "Live lyrics"
        self.controls.disabled = session.finished

    async def change(self, interaction: discord.Interaction, direction: int | None) -> None:
        await interaction.response.defer()
        async with self.session.lock:
            if self.session.finished:
                return
            if direction is None:
                self.session.live = not self.session.live
            else:
                self.session.page = max(0, min(len(self.session.pages) - 1, self.session.page + direction))
            self.refresh()
            await self.session.interaction.client.messages.edit(self.session.message, embed=self.session.embed(), view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.change(interaction, -1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.change(interaction, 1)

    @discord.ui.button(label="Full lyrics", style=discord.ButtonStyle.primary)
    async def mode(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.change(interaction, None)

    @discord.ui.button(label="Bring controls here", style=discord.ButtonStyle.secondary)
    async def controls(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        music = interaction.client.get_cog("Music")
        if music is None:
            return await interaction.response.send_message("The music service is unavailable.", ephemeral=True)
        await music.controls.callback(music, interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.warning("Lyric button failed: %s", error)
        try:
            await interaction.followup.send("Couldn't update the lyrics message. Check channel permissions and try again.", ephemeral=True)
        except discord.HTTPException:
            pass


class KaraokeSession:
    """Owns prepared files and a lyrics message, until Music explicitly finishes it."""

    def __init__(
        self, audio_path: str, temporary: tempfile.TemporaryDirectory, interaction: discord.Interaction,
        title: str, lyrics: Lyrics,
    ):
        self.audio_path = audio_path
        self.temporary = temporary
        self.interaction = interaction
        self.title = title
        self.lyrics = lyrics
        self.pages = lyric_pages(lyrics.plain.splitlines())
        self.times = [time for time, _ in lyrics.synced]
        self.pcm_bytes = 0
        self.page = 0
        self.live = bool(lyrics.synced)
        self.playing = False
        self.finished = False
        self.reason = "Finished"
        self.notice = ""
        self.message: discord.Message | discord.WebhookMessage | None = None
        self.lock = asyncio.Lock()
        self.finish_lock = asyncio.Lock()
        self.view = LyricsView(self)
        self.updater: asyncio.Task | None = None
        self.source: ClockedPCM | None = None

    def wrap_source(self, source: discord.AudioSource) -> discord.AudioSource:
        if source.is_opus():
            raise TypeError("KaraokeSession requires a decoded PCM source")
        if self.source:
            self.source.cleanup()
        self.pcm_bytes = 0
        self.source = ClockedPCM(source, self)
        return self.source

    def embed(self) -> discord.Embed:
        status = self.reason if self.finished else ("Playing instrumental" if self.playing else "Instrumental ready · queued")
        result = discord.Embed(title=safe_text(self.title)[:220], color=0x8E44AD)
        if self.live and self.lyrics.synced:
            index = bisect.bisect_right(self.times, self.pcm_bytes / 192000) - 1
            previous = self.lyrics.synced[index - 1][1] if index > 0 else ""
            current = self.lyrics.synced[index][1] if index >= 0 else "Waiting for the first lyric…"
            following = self.lyrics.synced[index + 1][1] if index + 1 < len(self.lyrics.synced) else ""
            result.add_field(name="Previous", value=safe_text(previous)[:900] or "—", inline=False)
            result.add_field(name="Sing", value=safe_text(current)[:900] or "—", inline=False)
            result.add_field(name="Next", value=safe_text(following)[:900] or "—", inline=False)
        elif self.pages:
            result.description = self.pages[self.page]
        else:
            result.description = self.lyrics.status
        footer = f"{status} · LRCLIB"
        if not self.live and self.pages:
            footer += f" · Page {self.page + 1}/{len(self.pages)} · unsynchronized browsing"
        if self.notice:
            footer += f" · {self.notice}"
        result.set_footer(text=footer[:1800])
        return result

    async def publish(self) -> None:
        self.message = await self.interaction.client.messages.send(
            self.interaction.channel, embed=self.embed(), view=self.view,
        )
        self.updater = asyncio.create_task(self.update_loop())

    async def start(self) -> None:
        if self.finished:
            raise RuntimeError("This karaoke session has already been cleaned up")
        self.playing = True
        if self.updater:
            self.updater.cancel()
            await asyncio.gather(self.updater, return_exceptions=True)
        self.updater = asyncio.create_task(self.update_loop())

    async def update_loop(self) -> None:
        previous = None
        try:
            while not self.finished:
                async with self.lock:
                    rendered = self.embed()
                    signature = rendered.to_dict()
                    if self.message and signature != previous:
                        self.view.refresh()
                        try:
                            await self.interaction.client.messages.edit(self.message, embed=rendered, view=self.view)
                            previous = signature
                        except PublicMessageError as error:
                            log.info("Couldn't update karaoke lyrics: %s", error)
                await asyncio.sleep(2.1)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A lyrics display failure must not end successfully prepared instrumental audio.
            log.exception("Karaoke lyrics updater stopped")

    async def finish(self, reason: str = "Finished") -> None:
        async with self.finish_lock:
            if self.finished:
                return
            self.finished = True
            self.reason = reason
            try:
                if self.updater:
                    self.updater.cancel()
                    await asyncio.gather(self.updater, return_exceptions=True)
                async with self.lock:
                    self.view.refresh()
                    if self.message:
                        try:
                            await self.interaction.client.messages.edit(self.message, embed=self.embed(), view=self.view)
                        except PublicMessageError:
                            log.info("Couldn't disable terminal lyric controls", exc_info=True)
            finally:
                self.view.stop()
                # FFmpeg holds the WAV open on Windows: close it before removing its directory.
                try:
                    if self.source:
                        self.source.cleanup()
                finally:
                    self.temporary.cleanup()


class PreparationProgress:
    """One durable webhook message for the entire preparation lifecycle."""

    def __init__(self, interaction: discord.Interaction):
        self.interaction = interaction
        self.message: discord.WebhookMessage | None = None

    async def update(self, text: str) -> None:
        messages = self.interaction.client.messages
        if self.message is None:
            self.message = await messages.send(self.interaction.channel, text)
            try:
                await self.interaction.delete_original_response()
            except discord.HTTPException:
                pass
        else:
            self.message = await messages.edit(self.message, text)

    async def finish(self, accepted: bool) -> None:
        if self.message is None:
            return
        text = (
            "Instrumental preparation complete; see the playback/queue and lyrics messages."
            if accepted else "Karaoke preparation ended without queueing audio; see the command error."
        )
        try:
            await self.interaction.client.messages.edit(self.message, text)
        except PublicMessageError:
            log.warning("Couldn't finalize karaoke preparation progress", exc_info=True)


class Karaoke(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.preparations: dict[int, asyncio.Task] = {}
        self.heavy_jobs = asyncio.Semaphore(1)

    @app_commands.command(name="karaoke", description="Prepare a YouTube instrumental with real vocal removal and lyrics")
    @app_commands.describe(url="A normal YouTube video link, not a playlist")
    @app_commands.guild_only()
    async def karaoke(self, interaction: discord.Interaction, url: str) -> None:
        guild = interaction.guild
        music = self.bot.get_cog("Music")
        if music is None:
            return await interaction.response.send_message("The music service is unavailable. Ask the bot owner to load it.", ephemeral=True)
        if not self.bot.features.enabled(guild.id, "karaoke"):
            return await interaction.response.send_message("Karaoke is disabled in this server.", ephemeral=True)
        voice = getattr(interaction.user, "voice", None)
        if not voice or not voice.channel:
            return await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
        active_voice = guild.voice_client
        if active_voice and active_voice.channel != voice.channel:
            return await self.bot.messages.reply(
                interaction, "Join my voice channel to prepare karaoke.", ephemeral=True,
            )
        try:
            url = youtube_video_url(url)
        except PreparationError as error:
            return await interaction.response.send_message(str(error), ephemeral=True)
        if guild.id in self.preparations:
            return await interaction.response.send_message("A karaoke track is already being prepared for this server. Please wait.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        task = asyncio.current_task()
        # No await between the duplicate check and registration after defer: another request
        # may have entered during defer, so check once more before taking ownership.
        if guild.id in self.preparations:
            return await music.fail(interaction, "A karaoke track is already being prepared for this server. Please wait.")
        self.preparations[guild.id] = task
        temporary = None
        session = None
        accepted = False
        progress = PreparationProgress(interaction)
        try:
            await progress.update(
                "Waiting for the karaoke preparation worker. Vocal removal can take several minutes; "
                "the first run also downloads model weights. The original vocal audio will never be used as a fallback."
            )
            async with self.heavy_jobs:
                if not self.bot.features.enabled(guild.id, "karaoke"):
                    raise PreparationError("Karaoke was disabled before preparation could start.")
                temporary = tempfile.TemporaryDirectory(prefix="discord-karaoke-")
                await progress.update("Downloading YouTube audio and preparing the instrumental; the selected separation engine and device are recorded in the worker log…")
                info, audio_path = await prepare_audio(url, Path(temporary.name))
            if not self.bot.features.enabled(guild.id, "karaoke"):
                raise PreparationError("Karaoke was disabled while this track was being prepared.")
            lyrics = await lookup_lyrics(info)
            if not self.bot.features.enabled(guild.id, "karaoke"):
                raise PreparationError("Karaoke was disabled while this track was being prepared.")
            title = str(info.get("title") or "YouTube karaoke")
            session = KaraokeSession(audio_path, temporary, interaction, title, lyrics)
            temporary = None  # From this point only the session owns the directory.
            await session.publish()
            track = Track(
                title=title, webpage_url=url, duration=info.get("duration"),
                requester=interaction.user.mention, karaoke=session,
            )
            accepted = bool(await music.enqueue_prepared(interaction, track))
        except asyncio.CancelledError:
            # Each subprocess has been killed/reaped by run_process before reaching this cleanup.
            try:
                await music.fail(interaction, "Karaoke preparation was cancelled (the feature was disabled or the cog was unloaded).")
            except discord.HTTPException:
                pass
            raise
        except (PreparationError, PublicMessageError) as error:
            await music.fail(interaction, str(error))
        except discord.HTTPException:
            log.exception("Karaoke Discord message/voice operation failed")
            try:
                await music.fail(interaction, "Couldn't send karaoke messages or join voice. Check the bot's Send Messages, Embed Links, Connect and Speak permissions.")
            except discord.HTTPException:
                pass
        except Exception:
            log.exception("Karaoke preparation failed")
            await music.fail(interaction, "Karaoke preparation failed. Check the console, FFmpeg, the selected separation engine's installation, available memory/disk, and network access. No original audio was queued.")
        finally:
            if self.preparations.get(guild.id) is task:
                self.preparations.pop(guild.id, None)
            try:
                if session is not None and not accepted:
                    await session.finish("Not queued")
            finally:
                if temporary is not None:
                    temporary.cleanup()
                await progress.finish(accepted)

    async def on_feature_toggle(self, guild: discord.Guild, feature: str, enabled: bool) -> None:
        if feature == "karaoke" and not enabled:
            task = self.preparations.get(guild.id)
            if task and task is not asyncio.current_task():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def cog_unload(self) -> None:
        tasks = list(self.preparations.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Karaoke(bot))
