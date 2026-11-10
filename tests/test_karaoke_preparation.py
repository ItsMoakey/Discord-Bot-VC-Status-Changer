import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

from cogs import karaoke


def write_wav(path):
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x10\x00\xf0\xff" * 800)


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        media = self.root / "media"
        media.mkdir()
        self.wav = media / "song.wav"
        write_wav(self.wav)
        handler = partial(SimpleHTTPRequestHandler, directory=str(media))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/song.wav"

    async def download(self, query, root, prefix=""):
        root.mkdir()
        await asyncio.wait_for(karaoke.run_process(
            [sys.executable, "-c", prefix + karaoke.DOWNLOAD_WORKER, query, str(root)],
            root, "download",
        ), 30)
        return json.loads((root / "metadata.json").read_text(encoding="utf-8"))

    async def test_supported_non_youtube_link_downloads_real_audio(self):
        root = self.root / "direct"
        info = await self.download(self.url, root)
        downloaded = Path(info["download_path"])
        self.assertTrue(downloaded.resolve().is_relative_to(root.resolve()))
        self.assertEqual(downloaded.read_bytes(), self.wav.read_bytes())
        self.assertEqual(info["webpage_url"], self.url)

    def extractor_fixture(self, kind):
        # Only extraction is synthetic: yt-dlp still processes the search
        # container, resolves the chosen entry, and downloads real HTTP audio.
        # A second extraction of the chosen song fails, like an expired page.
        return f'''
import yt_dlp
from yt_dlp.extractor.common import InfoExtractor
class FixtureSongIE(InfoExtractor):
    _VALID_URL = r"fixturesong:(?P<id>chosen)"
    extracted = False
    def _real_extract(self, url):
        if type(self).extracted:
            raise RuntimeError("The chosen result must not be extracted twice")
        type(self).extracted = True
        return {{"id": "chosen", "title": "Offline song", "url": {self.url!r},
                 "ext": "wav", "webpage_url": "fixturesong:chosen", "duration": 0.2,
                 "is_live": {kind == "live"!r}}}
class FixtureContainerIE(InfoExtractor):
    _VALID_URL = {("offlineplaylist:container" if kind == "playlist" else "ytsearch1?:offline fixture")!r}
    @classmethod
    def ie_key(cls):
        return {("Generic" if kind == "playlist" else "YoutubeSearch")!r}
    def _real_extract(self, url):
        return self.playlist_result([
            self.url_result("fixturesong:chosen", ie="FixtureSong")
        ], "offline", "Offline results")
OriginalYoutubeDL = yt_dlp.YoutubeDL
class FixtureYoutubeDL(OriginalYoutubeDL):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_info_extractor(FixtureSongIE())
        self.add_info_extractor(FixtureContainerIE())
yt_dlp.YoutubeDL = FixtureYoutubeDL
'''

    async def test_search_downloads_chosen_finite_result_without_reextracting(self):
        info = await self.download(
            "offline fixture", self.root / "search", self.extractor_fixture("search"),
        )
        self.assertEqual(info["title"], "Offline song")
        self.assertEqual(info["webpage_url"], "fixturesong:chosen")
        self.assertEqual(Path(info["download_path"]).read_bytes(), self.wav.read_bytes())

    async def test_live_result_and_nonsearch_playlist_produce_no_audio(self):
        for kind in ("live", "playlist"):
            with self.subTest(kind=kind):
                root = self.root / kind
                with self.assertRaises(karaoke.PreparationError):
                    query = "offlineplaylist:container" if kind == "playlist" else "ytsearch1:offline fixture"
                    await self.download(query, root, self.extractor_fixture(kind))
                self.assertFalse((root / "metadata.json").exists())
                self.assertEqual(list(root.glob("source.*")), [])


class PreparationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.info = {
            "title": "Chosen song", "webpage_url": "https://example.invalid/chosen",
            "duration": 0.2, "download_path": str(self.root / "source.wav"),
        }

    def downloaded(self):
        write_wav(self.root / "source.wav")
        (self.root / "metadata.json").write_text(json.dumps(self.info), encoding="utf-8")

    def separated(self):
        stems = self.root / "stems"
        stems.mkdir()
        write_wav(stems / "no_vocals.wav")

    async def test_lyrics_overlap_decode_and_separation_but_result_waits_for_whole_track(self):
        lyrics_started = asyncio.Event()
        separation_started = asyncio.Event()
        finish_separation = asyncio.Event()
        lyric_result = karaoke.Lyrics(plain="The chosen song's lyrics")

        async def lookup(info):
            lyrics_started.set()
            await asyncio.wait_for(separation_started.wait(), 2)
            return lyric_result

        async def process(args, root, stage):
            if stage == "download":
                self.downloaded()
            elif stage == "ffmpeg":
                await asyncio.wait_for(lyrics_started.wait(), 2)
                write_wav(root / "input.wav")
            elif stage == "separation":
                separation_started.set()
                await finish_separation.wait()
                self.separated()

        with patch.object(karaoke, "run_process", process), patch.object(karaoke, "lookup_lyrics", lookup):
            task = asyncio.create_task(karaoke.prepare_audio("chosen song search", self.root))
            try:
                await asyncio.wait_for(separation_started.wait(), 2)
                self.assertFalse(task.done())
                self.assertFalse((self.root / "stems" / "no_vocals.wav").exists())
                finish_separation.set()
                await asyncio.wait_for(task, 2)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def assert_lookup_reaped(self, cancel):
        lyrics_started = asyncio.Event()
        decode_started = asyncio.Event()
        cleanup_started = asyncio.Event()
        allow_cleanup = asyncio.Event()
        cleanup_finished = asyncio.Event()

        async def lookup(info):
            lyrics_started.set()
            try:
                await asyncio.Future()
            finally:
                cleanup_started.set()
                await allow_cleanup.wait()
                self.assertTrue((self.root / "source.wav").exists())
                cleanup_finished.set()

        async def process(args, root, stage):
            if stage == "download":
                self.downloaded()
            else:
                await lyrics_started.wait()
                decode_started.set()
                if cancel:
                    await asyncio.Future()
                raise karaoke.PreparationError("Decode failed")

        with patch.object(karaoke, "run_process", process), patch.object(karaoke, "lookup_lyrics", lookup):
            task = asyncio.create_task(karaoke.prepare_audio("song", self.root))
            try:
                await asyncio.wait_for(decode_started.wait(), 2)
                if cancel:
                    task.cancel()
                await asyncio.wait_for(cleanup_started.wait(), 2)
                self.assertFalse(task.done(), "Preparation returned while lyric cleanup was still running")
                if cancel:
                    task.cancel()  # Repeated cancellation must not abandon cleanup.
                allow_cleanup.set()
                with self.assertRaises(asyncio.CancelledError if cancel else karaoke.PreparationError):
                    await asyncio.wait_for(task, 2)
                self.assertTrue(cleanup_finished.is_set())
            finally:
                allow_cleanup.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_decode_failure_reaps_lyrics_before_returning(self):
        await self.assert_lookup_reaped(cancel=False)

    async def test_repeated_cancellation_reaps_lyrics_before_returning(self):
        await self.assert_lookup_reaped(cancel=True)

    async def test_missing_instrumental_never_returns_original_audio(self):
        async def process(args, root, stage):
            if stage == "download":
                self.downloaded()
            # A backend that exits successfully but emits no stem is not success.

        async def lookup(info):
            return karaoke.Lyrics()

        with patch.object(karaoke, "run_process", process), patch.object(karaoke, "lookup_lyrics", lookup):
            with self.assertRaises(karaoke.PreparationError):
                await karaoke.prepare_audio("song", self.root)
        self.assertTrue((self.root / "source.wav").exists())
        self.assertFalse((self.root / "stems" / "no_vocals.wav").exists())

    async def test_pure_playlist_rejected_before_creating_preparation_files(self):
        with self.assertRaises(karaoke.PreparationError):
            await karaoke.prepare_audio("https://www.youtube.com/playlist?list=PLoffline", self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_cancelled_real_worker_is_reaped_before_temp_cleanup(self):
        started = asyncio.Event()
        child = None
        original_spawn = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            nonlocal child
            process = await original_spawn(*args, **kwargs)
            # taskkill is another subprocess on Windows, not the worker.
            if args[0] == sys.executable:
                child = process
                started.set()
            return process

        script = "import time; time.sleep(60)"
        with patch.object(asyncio, "create_subprocess_exec", spawn):
            task = asyncio.create_task(karaoke.run_process(
                [sys.executable, "-c", script], self.root, "download",
            ))
            try:
                await asyncio.wait_for(started.wait(), 5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 10)
                self.assertIsNotNone(child.returncode, "Cancellation returned with a live worker")
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if child is not None and child.returncode is None:
                    await karaoke.terminate_process(child)
        # On Windows this also proves the log's open handle has been closed.
        (self.root / "download.log").unlink()


if __name__ == "__main__":
    unittest.main()
