import json
import logging
import os
import re
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("vcstatus")

# stored in the working directory (next to vc_status_bot.py), same as the original script
DATA_FILE = Path(os.getenv("VC_EMOJI_FILE") or "vc_emojis.json")
DEFAULT_EMOJI = ""
EMPTY_STATUS = ""

CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:[A-Za-z0-9_]+:\d+>")


def load_assignments() -> dict[int, str]:
    if DATA_FILE.exists():
        raw = json.loads(DATA_FILE.read_text())
        return {int(uid): emoji for uid, emoji in raw.items()}
    return {}


def save_assignments(assignments: dict[int, str]) -> None:
    DATA_FILE.write_text(json.dumps({str(uid): e for uid, e in assignments.items()}, indent=2))


def readable_emoji(emoji: str) -> str:
    match = re.search(r":([A-Za-z0-9_]+):", emoji)
    return f":{match.group(1)}:" if match else emoji


def looks_like_emoji(text: str) -> bool:
    text = text.strip()
    if CUSTOM_EMOJI_PATTERN.fullmatch(text):
        return True
    return not text.isascii()


def has_manage_channels(interaction: discord.Interaction) -> bool:
    member = interaction.guild.get_member(interaction.user.id)
    return bool(member and member.guild_permissions.manage_channels)


class VCStatus(commands.Cog):
    """Voice channel status emojis."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.user_emojis = load_assignments()
        # channel IDs where status updates are paused (/vctoggle)
        self.disabled_channels: set[int] = set()
        self._session: aiohttp.ClientSession | None = None

    async def cog_load(self):
        log.info("%d VC emoji assignment(s) loaded", len(self.user_emojis))

    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ---------- status logic ----------

    def pick_status_for(self, channel: discord.VoiceChannel) -> str:
        # every member counts, including bots: give the bot an emoji with /vcadd and it shows up too
        members = channel.members
        if not members:
            return EMPTY_STATUS
        emojis = [self.user_emojis[m.id] for m in members if m.id in self.user_emojis]
        if not emojis:
            return DEFAULT_EMOJI
        return " ".join(emojis)

    async def update_channel_status(self, channel: discord.abc.GuildChannel, status: str) -> bool:
        url = f"https://discord.com/api/v10/channels/{channel.id}/voice-status"
        headers = {
            "Authorization": f"Bot {self.bot.http.token}",
            "Content-Type": "application/json",
        }
        async with self.session().put(url, json={"status": status}, headers=headers) as resp:
            ok = resp.status in (200, 204)
            label = readable_emoji(status) if status else "(cleared)"
            if ok:
                log.info("#%s -> %s", channel.name, label)
            else:
                log.warning("#%s -> failed (%s): %s", channel.name, resp.status, await resp.text())
            return ok

    async def refresh_channel(self, channel: discord.abc.GuildChannel):
        if channel.id in self.disabled_channels:
            return
        await self.update_channel_status(channel, self.pick_status_for(channel))

    # ---------- listeners ----------

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        to_refresh = set()
        if before.channel and before.channel != after.channel:
            to_refresh.add(before.channel)
        if after.channel and after.channel != before.channel:
            to_refresh.add(after.channel)
        for ch in to_refresh:
            await self.refresh_channel(ch)

    # ---------- commands ----------

    @app_commands.command(name="vcadd", description="Give yourself (or someone else) a VC emoji.")
    @app_commands.describe(user="Who to assign the emoji to", emoji="A server emoji or unicode emoji")
    @app_commands.guild_only()
    async def vcadd(self, interaction: discord.Interaction, user: discord.Member, emoji: str):
        if user.id != interaction.user.id and not has_manage_channels(interaction):
            return await interaction.response.send_message(
                "You can only set your own emoji. Setting others' requires **Manage Channels**.",
                ephemeral=True,
            )

        emoji = emoji.strip()
        if not looks_like_emoji(emoji):
            return await interaction.response.send_message("Thats not an emoji 💀", ephemeral=True)

        updated = user.id in self.user_emojis
        self.user_emojis[user.id] = emoji
        save_assignments(self.user_emojis)

        live_note = ""
        if user.voice and user.voice.channel:
            await self.refresh_channel(user.voice.channel)
            live_note = " They're in a VC right now, take a look!"

        verb = "Updated" if updated else "Done"
        await interaction.response.send_message(
            f"{verb}! {user.mention} → {emoji}{live_note}", ephemeral=True
        )

    @app_commands.command(name="vcdelete", description="Remove your (or someone else's) VC emoji.")
    @app_commands.describe(user="Who to remove")
    @app_commands.guild_only()
    async def vcdelete(self, interaction: discord.Interaction, user: discord.Member):
        if user.id != interaction.user.id and not has_manage_channels(interaction):
            return await interaction.response.send_message(
                "You can only remove your own emoji. Removing others' requires **Manage Channels**.",
                ephemeral=True,
            )

        if user.id not in self.user_emojis:
            return await interaction.response.send_message(
                f"{user.mention} doesn't have an emoji assigned.", ephemeral=True
            )

        removed = self.user_emojis.pop(user.id)
        save_assignments(self.user_emojis)

        if user.voice and user.voice.channel:
            await self.refresh_channel(user.voice.channel)

        await interaction.response.send_message(
            f"Removed {removed} from {user.mention}, they'll show as blank next time they're in a VC.",
            ephemeral=True,
        )

    @app_commands.command(
        name="vclist",
        description="See VC emoji assignments. Shows your current channel if you're in one, otherwise the whole server.",
    )
    @app_commands.guild_only()
    async def vclist(self, interaction: discord.Interaction):
        if not self.user_emojis:
            return await interaction.response.send_message(
                "Nobody has an emoji yet - use `/vcadd` to set one!", ephemeral=True
            )

        member = interaction.guild.get_member(interaction.user.id)
        vc = member.voice.channel if member and member.voice else None

        if vc:
            shown = [m for m in vc.members if m.id in self.user_emojis]
            if not shown:
                return await interaction.response.send_message(
                    f"Nobody in #{vc.name} has an emoji assigned yet.", ephemeral=True
                )
            header = f"**Emojis in #{vc.name}:**"
            lines = [f"{self.user_emojis[m.id]}  {m.mention}" for m in shown]
        else:
            header = "**VC emoji assignments:**"
            lines = []
            for uid, emoji in self.user_emojis.items():
                m = interaction.guild.get_member(uid)
                if m:  # skip users no longer in the server
                    lines.append(f"{emoji}  {m.mention}")
            if not lines:
                return await interaction.response.send_message(
                    "No assignments for anyone currently in this server.", ephemeral=True
                )

        await interaction.response.send_message(header + "\n" + "\n".join(lines), ephemeral=True)

    @app_commands.command(name="vcstatus", description="Manually refresh a voice channel's status.")
    @app_commands.describe(channel="The voice channel to refresh")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def vcstatus(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        status = self.pick_status_for(channel)
        if await self.update_channel_status(channel, status):
            label = readable_emoji(status) if status else "(cleared)"
            await interaction.response.send_message(
                f"Done! **{channel.name}** is now showing {label}.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"Something went wrong with **{channel.name}**, check the console.", ephemeral=True
            )

    @app_commands.command(name="vctoggle", description="Pause or resume the bot for the VC you're currently in.")
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def vctoggle(self, interaction: discord.Interaction):
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.voice or not member.voice.channel:
            return await interaction.response.send_message(
                "You need to be in a voice channel to use this.", ephemeral=True
            )

        channel = member.voice.channel
        if channel.id in self.disabled_channels:
            self.disabled_channels.discard(channel.id)
            msg = f"Resumed **#{channel.name}** - statuses will update automatically again."
        else:
            self.disabled_channels.add(channel.id)
            msg = f"Paused **#{channel.name}** - statuses won't update until you toggle it back on."
        await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(VCStatus(bot))