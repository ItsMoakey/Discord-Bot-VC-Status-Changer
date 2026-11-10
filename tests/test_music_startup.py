import asyncio
from array import array
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import math
from pathlib import Path
import shutil
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import wave

from cogs.music import GuildState, Music, Track


class PlaybackVoice:
    """Consume a real FFmpeg PCM frame without contacting Discord."""

    def __init__(self):
        self.playing = False
        self.rms = None

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return False

    def play(self, source, *, after):
        try:
            frame = source.read()
            if len(frame) != 3840:
                raise RuntimeError("Expected a complete 20 ms stereo PCM frame")
            samples = array("h", frame)
            self.rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
            self.playing = True
        finally:
            source.cleanup()


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for real-audio startup tests")
class MusicStartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for name, amplitude in [("quiet", 2000), ("loud", 8000)]:
            path = Path(self.directory.name) / (name + ".wav")
            samples = array("h")
            for index in range(9600):
                value = int(amplitude * math.sin(2 * math.pi * 1000 * index / 48000))
                samples.extend([value, value])
            with wave.open(str(path), "wb") as stream:
                stream.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                stream.writeframes(samples.tobytes())
        handler = partial(SimpleHTTPRequestHandler, directory=self.directory.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server_thread.join)
        self.addCleanup(self.server.shutdown)
        base = f"http://127.0.0.1:{self.server.server_port}"
        self.quiet_url = base + "/quiet.wav"
        self.loud_url = base + "/loud.wav"
        self.voice = PlaybackVoice()
        self.guild = SimpleNamespace(id=1, voice_client=self.voice)
        self.music = object.__new__(Music)
        self.music.bot = SimpleNamespace(
            features=SimpleNamespace(enabled=lambda *_: True),
            messages=SimpleNamespace(reply=AsyncMock(return_value=object())),
            loop=asyncio.get_running_loop(),
        )
        self.music.states = {1: GuildState()}
        self.music.play_locks = {}
        self.music.control_guard = AsyncMock(return_value=True)
        self.music.defer = AsyncMock()
        self.music.announce = AsyncMock()
        self.music.refresh_panel = AsyncMock()

    async def test_immediate_play_starts_during_voice_lookup_without_second_extraction(self):
        voice_started = threading.Event()
        lookup_started = threading.Event()
        looked_up = False

        async def join_voice(*_):
            voice_started.set()
            if not await asyncio.to_thread(lookup_started.wait, 5):
                raise RuntimeError("Voice setup serialized the media lookup")
            return self.voice

        def lookup(_):
            nonlocal looked_up
            if looked_up:
                raise ValueError("A second extractor request is no longer authorized")
            looked_up = True
            lookup_started.set()
            if not voice_started.wait(5):
                raise RuntimeError("Media lookup serialized voice setup")
            return {"title": "Immediate song", "webpage_url": "https://example.invalid/song", "duration": 1, "url": self.quiet_url}

        self.music.join_voice = join_voice
        interaction = SimpleNamespace(
            guild=self.guild,
            user=SimpleNamespace(voice=SimpleNamespace(channel=object()), display_name="Listener"),
            channel=object(),
        )
        with patch("cogs.music.extract_info", side_effect=lookup):
            await self.music.enqueue(interaction, "a song")
        self.assertEqual(self.music.states[1].current.title, "Immediate song")
        self.assertTrue(self.voice.playing)
        self.assertAlmostEqual(self.voice.rms, 2000 / math.sqrt(2), delta=100)

    async def test_older_queued_song_does_not_use_new_requests_stream(self):
        queued = Track("Queued song", "https://example.invalid/queued", 1, "Listener")
        newly_requested = Track("New song", "https://example.invalid/new", 1, "Listener")
        self.music.states[1].queue.extend([queued, newly_requested])
        with patch("cogs.music.resolve_stream", return_value=self.quiet_url):
            await self.music.play_next(self.guild, ready_stream=(newly_requested, self.loud_url))
        self.assertIs(self.music.states[1].current, queued)
        self.assertAlmostEqual(self.voice.rms, 2000 / math.sqrt(2), delta=100)

    async def test_loop_refreshes_stream_before_replaying(self):
        track = Track("Looped song", "https://example.invalid/looped", 1, "Listener")
        state = self.music.states[1]
        state.current = track
        state.loop = True
        with patch("cogs.music.resolve_stream", return_value=self.loud_url):
            await self.music.play_next(self.guild, completed=track)
        self.assertIs(state.current, track)
        self.assertAlmostEqual(self.voice.rms, 8000 / math.sqrt(2), delta=100)


if __name__ == "__main__":
    unittest.main()
