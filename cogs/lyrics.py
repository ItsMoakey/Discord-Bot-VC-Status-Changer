"""Shared lyrics lookup, playback clock, and webhook display for all songs."""
from __future__ import annotations

import asyncio
import bisect
import logging
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

import aiohttp
import discord

from cogs.messages import PublicMessageError

log = logging.getLogger("musicbot.lyrics")
LRCLIB_URL = "https://lrclib.net/api/search"
STAMP = re.compile(r"\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]")
DECORATION = re.compile(
    r"\s*[\[(][^\])]*(?:official|lyrics?|audio|video|visuali[sz]er|hd|4k)[^\])]*[\])]",
    re.IGNORECASE,
)


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
    status: str = "No confidently matching lyrics were found on LRCLIB. Playback is still available."


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
        return Lyrics(status="Lyrics lookup was unavailable or no safe match was found. Playback will continue without lyrics.")
    return Lyrics()


class ClockedPCM(discord.AudioSource):
    def __init__(self, source: discord.AudioSource, session: LyricsSession):
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
    def __init__(self, session: LyricsSession):
        super().__init__(timeout=None)
        self.session = session
        self.refresh()

    def refresh(self) -> None:
        session = self.session
        self.previous.disabled = session.finished or not session.enabled or session.live or session.page <= 0
        self.next_page.disabled = session.finished or not session.enabled or session.live or session.page >= len(session.pages) - 1
        self.mode.disabled = session.finished or not session.enabled or not bool(session.lyrics and session.lyrics.synced)
        self.mode.label = "Full lyrics" if session.live else "Live lyrics"
        self.controls.disabled = session.finished

    async def change(self, interaction: discord.Interaction, direction: int | None) -> None:
        await interaction.response.defer()
        async with self.session.lock:
            if self.session.finished or not self.session.enabled:
                return
            if direction is None:
                self.session.live = not self.session.live
            else:
                self.session.page = max(0, min(len(self.session.pages) - 1, self.session.page + direction))
            self.refresh()
            await self.session.client.messages.edit(self.session.message, embed=self.session.embed(), view=self)

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


class LyricsSession:
    """Owns the playback clock and optional lyrics display for one track."""

    def __init__(
        self, client, channel, title: str, lyrics: Lyrics | None = None,
        *, info: dict | None = None, enabled: bool = False,
    ):
        self.info = info or {}
        self.enabled = enabled
        self.lyrics = lyrics
        lyrics = lyrics or Lyrics()
        self.client = client
        self.channel = channel
        self.title = title
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
            raise TypeError("LyricsSession requires a decoded PCM source")
        if self.source:
            self.source.cleanup()
        self.pcm_bytes = 0
        self.source = ClockedPCM(source, self)
        return self.source

    def embed(self) -> discord.Embed:
        status = self.reason if self.finished else ("Playing" if self.playing else "Queued")
        if not self.enabled and not self.finished:
            status += " · Lyrics off"
        result = discord.Embed(title=safe_text(self.title)[:220], color=0x8E44AD)
        if not self.enabled:
            result.description = "Lyrics are off. Use Lyrics on the playback panel to enable them."
        elif self.lyrics is None:
            result.description = "Looking up lyrics…"
        elif self.live and self.lyrics.synced:
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

    async def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        if self.updater:
            self.updater.cancel()
            await asyncio.gather(self.updater, return_exceptions=True)
            self.updater = None
        if self.finished:
            return
        self.view.refresh()
        if enabled:
            if self.message is None:
                await self.publish()
            else:
                self.updater = asyncio.create_task(self.update_loop())
        elif self.message:
            await self.client.messages.edit(self.message, embed=self.embed(), view=self.view)

    async def publish(self) -> None:
        self.message = await self.client.messages.send(
            self.channel, embed=self.embed(), view=self.view,
        )
        self.updater = asyncio.create_task(self.update_loop())

    async def start(self) -> None:
        if self.finished:
            raise RuntimeError("This lyrics session has already been cleaned up")
        self.playing = True
        if not self.enabled:
            return
        if self.updater:
            self.updater.cancel()
            await asyncio.gather(self.updater, return_exceptions=True)
        self.updater = asyncio.create_task(self.update_loop())

    async def update_loop(self) -> None:
        previous = None
        try:
            if self.lyrics is None:
                self.lyrics = await lookup_lyrics(self.info)
                self.pages = lyric_pages(self.lyrics.plain.splitlines())
                self.times = [time for time, _ in self.lyrics.synced]
                self.live = bool(self.lyrics.synced)
            while not self.finished and self.enabled:
                async with self.lock:
                    rendered = self.embed()
                    signature = rendered.to_dict()
                    if self.message and signature != previous:
                        self.view.refresh()
                        try:
                            await self.client.messages.edit(self.message, embed=rendered, view=self.view)
                            previous = signature
                        except PublicMessageError as error:
                            log.info("Couldn't update lyrics: %s", error)
                await asyncio.sleep(2.1)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A display failure must not interrupt audio playback.
            log.exception("Lyrics updater stopped")

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
                            await self.client.messages.edit(self.message, embed=self.embed(), view=self.view)
                        except PublicMessageError:
                            log.info("Couldn't disable terminal lyric controls", exc_info=True)
            finally:
                self.view.stop()
