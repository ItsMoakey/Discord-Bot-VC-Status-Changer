import asyncio
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord

from cogs.karaoke import KaraokeSession
from cogs.lyrics import Lyrics, LyricsSession
from cogs.music import GuildState, Music, Track


class PCM(discord.AudioSource):
    def __init__(self):
        self.cleaned = False

    def read(self):
        return b"\0" * 3840

    def cleanup(self):
        self.cleaned = True


class LyricsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.messages = SimpleNamespace(send=AsyncMock(return_value=object()), edit=AsyncMock(), reply=AsyncMock())
        self.bot = SimpleNamespace(messages=self.messages)
        self.channel = object()

    async def test_late_toggle_uses_current_audio_position_and_reuses_message(self):
        session = LyricsSession(self.bot, self.channel, "Song")
        self.addAsyncCleanup(session.finish)
        pcm = PCM()
        source = session.wrap_source(pcm)
        lookup = AsyncMock(return_value=Lyrics([(0, "First"), (1, "Second")], "First\nSecond"))
        with patch("cogs.lyrics.lookup_lyrics", lookup):
            await session.start()
            for _ in range(75):
                source.read()
            lookup.assert_not_awaited()
            self.messages.send.assert_not_awaited()
            state = GuildState()
            state.current = Track("Song", "url", 5, "Listener", lyrics=session)
            music = Music.__new__(Music)
            music.bot = self.bot
            music.states = {1: state}
            music.play_locks = {}
            music.refresh_panel = AsyncMock()
            interaction = SimpleNamespace(guild=SimpleNamespace(id=1))
            await music.toggle_lyrics(interaction)
            await asyncio.sleep(0)
            self.assertEqual(session.embed().fields[1].value, "Second")
            await music.toggle_lyrics(interaction)
            self.assertIn("Lyrics are off", session.embed().description)
            self.assertTrue(session.view.mode.disabled)
            self.assertFalse(pcm.cleaned, "Turning lyrics off must not close playing audio")
            source.read()
            self.assertEqual(session.pcm_bytes, 76 * 3840)
            await music.toggle_lyrics(interaction)
            await asyncio.sleep(0)
            self.assertEqual(session.embed().fields[1].value, "Second")
            self.messages.send.assert_awaited_once()
            lookup.assert_awaited_once()

    async def test_turning_off_cancels_pending_lookup_without_stopping_audio(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def lookup(info):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        session = LyricsSession(self.bot, self.channel, "Song")
        self.addAsyncCleanup(session.finish)
        pcm = PCM()
        source = session.wrap_source(pcm)
        with patch("cogs.lyrics.lookup_lyrics", lookup):
            await session.set_enabled(True)
            await asyncio.wait_for(started.wait(), 1)
            await session.set_enabled(False)
        self.assertTrue(cancelled.is_set())
        self.assertIsNone(session.updater)
        source.read()
        self.assertEqual(session.pcm_bytes, 3840)
        self.assertFalse(pcm.cleaned)

    async def test_karaoke_lyrics_toggle_keeps_instrumental_until_finish(self):
        temporary = tempfile.TemporaryDirectory()
        interaction = SimpleNamespace(client=self.bot, channel=self.channel)
        session = KaraokeSession("instrumental.wav", temporary, interaction, "Song", Lyrics(plain="Words"))
        self.addAsyncCleanup(session.finish)
        pcm = PCM()
        session.wrap_source(pcm)
        await session.publish()
        await session.start()
        await asyncio.sleep(0)
        self.assertEqual(session.embed().description, "Words")
        await session.set_enabled(False)
        from pathlib import Path
        self.assertTrue(Path(temporary.name).exists())
        self.assertFalse(pcm.cleaned)
        await session.finish("Skipped")
        self.assertTrue(pcm.cleaned)
        self.assertFalse(Path(temporary.name).exists())
        self.assertTrue(all(item.disabled for item in session.view.children))
