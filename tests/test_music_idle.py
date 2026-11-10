import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from cogs.music import GuildState, Music


class IdleProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.voice = SimpleNamespace(is_playing=lambda: False, is_paused=lambda: False)
        self.guild = SimpleNamespace(id=1, voice_client=self.voice)
        self.music = object.__new__(Music)
        self.music.states = {1: GuildState()}
        self.music.control_guard = AsyncMock(return_value=True)
        self.music.defer = AsyncMock()
        self.music.join_voice = AsyncMock(return_value=self.voice)

        async def disconnect(guild):
            guild.voice_client = None

        self.music.disconnect = disconnect
        self.timer = patch("cogs.music.IDLE_LEAVE_SECONDS", 0)
        self.timer.start()
        self.addCleanup(self.timer.stop)

    async def asyncTearDown(self):
        for state in self.music.states.values():
            if state.idle_task:
                state.idle_task.cancel()
                await asyncio.gather(state.idle_task, return_exceptions=True)

    async def test_playlist_lookup_survives_an_idle_deadline(self):
        started = asyncio.Event()
        finish = asyncio.Event()

        async def playlist(interaction, query):
            started.set()
            await finish.wait()
            self.voice.is_playing = lambda: True

        self.music.enqueue_playlist = playlist
        interaction = SimpleNamespace(guild=self.guild, user=SimpleNamespace(voice=SimpleNamespace(channel=object())))
        task = asyncio.create_task(self.music.enqueue(interaction, "https://www.youtube.com/playlist?list=PLoffline"))
        try:
            await asyncio.wait_for(started.wait(), 2)
            await self.music.leave_after_idle(self.guild)
            self.assertIs(self.guild.voice_client, self.voice)
            finish.set()
            await asyncio.wait_for(task, 2)
            self.assertIs(self.guild.voice_client, self.voice)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_last_pending_request_restarts_idle_countdown(self):
        async with self.music.preparing_track(self.guild):
            async with self.music.preparing_track(self.guild):
                await self.music.leave_after_idle(self.guild)
                self.assertIs(self.guild.voice_client, self.voice)
            await self.music.leave_after_idle(self.guild)
            self.assertIs(self.guild.voice_client, self.voice)
        await self.music.states[1].idle_task
        self.assertIsNone(self.guild.voice_client)

    async def test_failed_preparation_does_not_leave_voice_connected_forever(self):
        with self.assertRaises(ValueError):
            async with self.music.preparing_track(self.guild):
                raise ValueError("Preparation failed")
        await self.music.states[1].idle_task
        self.assertIsNone(self.guild.voice_client)

    async def test_cancelled_preparation_releases_idle_protection(self):
        started = asyncio.Event()

        async def prepare():
            async with self.music.preparing_track(self.guild):
                started.set()
                await asyncio.Future()

        task = asyncio.create_task(prepare())
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.music.states[1].idle_task
        self.assertIsNone(self.guild.voice_client)

    async def test_completed_old_request_does_not_disconnect_replacement_state(self):
        async with self.music.preparing_track(self.guild):
            self.music.states[1] = GuildState()
        self.assertIsNone(self.music.states[1].idle_task)
        self.assertIs(self.guild.voice_client, self.voice)


if __name__ == "__main__":
    unittest.main()
