"""Optional karaoke separation, with cancellable preparation and LRCLIB lyrics."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from cogs.music import Track, is_playlist_url
from cogs.messages import PublicMessageError
from cogs.lyrics import Lyrics, LyricsSession, lookup_lyrics

log = logging.getLogger("musicbot.karaoke")


class PreparationError(Exception):
    """An error with a safe, actionable explanation for the command's user."""


# Use the same download defaults as music, but in a killable interpreter rather
# than a thread: cancellation must not leave a download writing into a deleted directory.
DOWNLOAD_WORKER = r'''
import json, math, sys
from pathlib import Path
import yt_dlp
from cogs.music import YDL_OPTS
root = Path(sys.argv[2])
def finite_song(info, *, incomplete=False):
    if info.get("is_live") or info.get("is_upcoming") or info.get("live_status") in {"is_live", "is_upcoming"}:
        return "Live streams and upcoming media cannot be prepared as karaoke."
    duration = info.get("duration")
    if isinstance(duration, (int, float)) and (not math.isfinite(duration) or duration <= 0):
        return "A finite song is required for karaoke."
options = {**YDL_OPTS, "noplaylist": True, "playlistend": 1,
           "outtmpl": str(root / "source.%(ext)s"),
           "overwrites": True, "match_filter": finite_song}
with yt_dlp.YoutubeDL(options) as ydl:
    info = ydl.extract_info(sys.argv[1], download=False)
    if isinstance(info, dict) and "entries" in info:
        if info.get("extractor_key") != "YoutubeSearch":
            raise RuntimeError("Playlist links are unsupported; choose one song or enter search words.")
        entries = [entry for entry in info["entries"] or [] if entry]
        if len(entries) != 1:
            raise RuntimeError("No single song was found for this search.")
        info = entries[0]
    if not isinstance(info, dict) or info.get("_type", "video") != "video" or "entries" in info:
        raise RuntimeError("A single finite song is required; playlists are unsupported.")
    reason = finite_song(info)
    if reason:
        raise RuntimeError(reason)
    if not info.get("webpage_url"):
        raise RuntimeError("The selected song has no usable source link.")
    # Download the resolved format, without extracting the chosen search result again.
    ydl.process_info(info)
    audio = info.get("filepath") or ydl.prepare_filename(info)
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
            "Audio could not be downloaded. Use search words or one supported song link, not a playlist "
            "or live/upcoming stream. The media may be private, restricted, or unavailable; "
            "check the console and update yt-dlp if extraction is failing."
        )


async def prepare_audio(query: str, root: Path) -> tuple[dict, str, Lyrics]:
    if is_playlist_url(query):
        raise PreparationError("Karaoke prepares one song at a time. Use a song link or search words, not a playlist link.")
    await run_process([sys.executable, "-c", DOWNLOAD_WORKER, query, str(root)], root, "download")
    info = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    source = Path(info["download_path"]).resolve()
    if not source.is_relative_to(root.resolve()) or not source.is_file():
        raise PreparationError("The download did not produce a usable local audio file.")
    lyrics_task = asyncio.create_task(lookup_lyrics(info))
    try:
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
        lyrics = await lyrics_task
        return info, str(instrumental), lyrics
    except BaseException:
        # Reap the lookup too, including repeated cancellation, before the caller removes root.
        lyrics_task.cancel()
        cleanup = asyncio.gather(lyrics_task, return_exceptions=True)
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        cleanup.result()
        raise


class KaraokeSession(LyricsSession):
    """Prepared instrumental resources, with the shared optional lyrics display."""

    def __init__(self, audio_path, temporary, interaction, title, lyrics):
        super().__init__(interaction.client, interaction.channel, title, lyrics, enabled=True)
        self.audio_path = audio_path
        self.temporary = temporary

    async def finish(self, reason: str = "Finished") -> None:
        try:
            await super().finish(reason)
        finally:
            # FFmpeg holds the WAV open on Windows.
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
            "Karaoke ready."
            if accepted else "Karaoke not queued."
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

    @app_commands.command(name="karaoke", description="Play a song without vocals, with lyrics")
    @app_commands.describe(query="Song link or search words (one song, not a playlist)")
    @app_commands.guild_only()
    async def karaoke(self, interaction: discord.Interaction, query: str) -> None:
        guild = interaction.guild
        music = self.bot.get_cog("Music")
        if music is None:
            return await interaction.response.send_message("Music is unavailable.", ephemeral=True)
        async with music.preparing_track(guild):
            await self._karaoke(interaction, query, music)

    async def _karaoke(self, interaction: discord.Interaction, query: str, music) -> None:
        guild = interaction.guild
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
        query = query.strip()
        if is_playlist_url(query):
            return await interaction.response.send_message(
                "Use a single song, not a playlist.", ephemeral=True,
            )
        if guild.id in self.preparations:
            return await interaction.response.send_message("Already preparing karaoke. Please wait.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        task = asyncio.current_task()
        # No await between the duplicate check and registration after defer: another request
        # may have entered during defer, so check once more before taking ownership.
        if guild.id in self.preparations:
            return await music.fail(interaction, "Already preparing karaoke. Please wait.")
        self.preparations[guild.id] = task
        temporary = None
        session = None
        accepted = False
        progress = PreparationProgress(interaction)
        try:
            await progress.update(
                "Waiting to prepare karaoke. This may take a few minutes."
            )
            async with self.heavy_jobs:
                if not self.bot.features.enabled(guild.id, "karaoke"):
                    raise PreparationError("Karaoke was disabled before preparation could start.")
                temporary = tempfile.TemporaryDirectory(prefix="discord-karaoke-")
                await progress.update("Preparing the instrumental and lyrics...")
                info, audio_path, lyrics = await prepare_audio(query, Path(temporary.name))
            if not self.bot.features.enabled(guild.id, "karaoke"):
                raise PreparationError("Karaoke was disabled while this track was being prepared.")
            title = str(info.get("title") or "Karaoke")
            session = KaraokeSession(audio_path, temporary, interaction, title, lyrics)
            temporary = None  # From this point only the session owns the directory.
            await session.publish()
            track = Track(
                title=title, webpage_url=str(info["webpage_url"]), duration=info.get("duration"),
                requester=interaction.user.mention, karaoke=session, lyrics=session,
            )
            accepted = bool(await music.enqueue_prepared(interaction, track))
        except asyncio.CancelledError:
            # Each subprocess has been killed/reaped by run_process before reaching this cleanup.
            try:
                await music.fail(interaction, "Karaoke preparation cancelled.")
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
            await music.fail(interaction, "Couldn't prepare karaoke. Check the bot logs.")
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
