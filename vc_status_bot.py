import os
import re
import json
import aiohttp
import discord
from discord.ext import commands
from pathlib import Path

## A Discord bot that updates voice channel statuses to show custom emojis for who's currently in them.
## To use, create a bot in the Discord Developer Portal, invite it to your server with the "Manage Channels" permission, and set its token as an environment variable named `DISCORD_TOKEN`.
## Users can set their own emoji with the `/vcadd` command, and optionally specify another user if they have the "Manage Channels" permission. Emojis can be either custom server emojis (e.g. `<:thonkang:219069250692841473>`) or any unicode emoji (e.g. `🐸`). When a user joins a voice channel, the bot will update the channel's status to show the assigned emojis of all users currently in that channel. If no one in the channel has an assigned emoji, it will show a default emoji (which is currently blank but can be customized). If everyone in the channel has their emoji removed, it will clear the status entirely

## Add token here or set it as an environment variable before running the script. Note that the bot needs the "Manage Channels" permission to update voice channel statuses, and users need that permission to set emojis for others or to manually refresh statuses.
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "__YOUR_TOKEN_HERE__")
DATA_FILE = Path("vc_emojis.json")
DEFAULT_EMOJI = ""
EMPTY_STATUS = ""

# channel IDs where the bot is paused — use /vctoggle while in a VC to add/remove
disabled_channels: set[int] = set()

CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:[A-Za-z0-9_]+:\d+>")


def load_assignments():
    if DATA_FILE.exists():
        raw = json.loads(DATA_FILE.read_text())
        return {int(uid): emoji for uid, emoji in raw.items()}
    return {}


def save_assignments(assignments):
    DATA_FILE.write_text(json.dumps({str(uid): e for uid, e in assignments.items()}, indent=2))


user_emojis = load_assignments()

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


def readable_emoji(emoji):
    match = re.search(r":([A-Za-z0-9_]+):", emoji)
    return f":{match.group(1)}:" if match else emoji


def looks_like_emoji(text):
    text = text.strip()
    if CUSTOM_EMOJI_PATTERN.fullmatch(text):
        return True
    return not text.isascii()


def pick_status_for(channel):
    humans = channel.members
    if not humans:
        return EMPTY_STATUS
    emojis = [user_emojis[m.id] for m in humans if m.id in user_emojis]
    if not emojis:
        return DEFAULT_EMOJI
    return " ".join(emojis)


async def update_channel_status(channel, status):
    url = f"https://discord.com/api/v10/channels/{channel.id}/voice-status"
    headers = {
        "Authorization": f"Bot {DISCORD_TOKEN}",
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession() as session:
        async with session.put(url, json={"status": status}, headers=headers) as resp:
            ok = resp.status in (200, 204)
            label = readable_emoji(status) if status else "(cleared)"
            if ok:
                print(f"  ✓  #{channel.name}  →  {label}")
            else:
                print(f"  ✗  #{channel.name}  →  failed ({resp.status}): {await resp.text()}")
            return ok


async def refresh_channel(channel):
    if channel.id in disabled_channels:
        return
    await update_channel_status(channel, pick_status_for(channel))


@bot.event
async def on_ready():
    print(f"\n👋  Logged in as {bot.user}")
    print(f"📋  {len(user_emojis)} emoji assignment(s) loaded")
    print(f"🎤  Watching for voice state changes...\n")


@bot.event
async def on_voice_state_update(member, before, after):
    to_refresh = set()
    if before.channel and before.channel != after.channel:
        to_refresh.add(before.channel)
    if after.channel and after.channel != before.channel:
        to_refresh.add(after.channel)
    for ch in to_refresh:
        await refresh_channel(ch)


def has_manage_channels(interaction):
    member = interaction.guild.get_member(interaction.user.id)
    return member and member.guild_permissions.manage_channels


@bot.tree.command(name="vcadd", description="Give yourself (or someone else) a VC emoji.")
@discord.app_commands.describe(
    user="Who to assign the emoji to",
    emoji="A server emoji or unicode emoji",
)
async def vcadd(interaction: discord.Interaction, user: discord.Member, emoji: str):
    if user.id != interaction.user.id and not has_manage_channels(interaction):
        await interaction.response.send_message(
            "You can only set your own emoji. Setting others' requires **Manage Channels**.",
            ephemeral=True,
        )
        return

    emoji = emoji.strip()
    if not looks_like_emoji(emoji):
        await interaction.response.send_message("Thats not an emoji 💀", ephemeral=True)
        return

    updated = user.id in user_emojis
    user_emojis[user.id] = emoji
    save_assignments(user_emojis)

    live_note = ""
    if user.voice and user.voice.channel:
        await refresh_channel(user.voice.channel)
        live_note = " They're in a VC right now, take a look!"

    verb = "Updated" if updated else "Done"
    await interaction.response.send_message(f"{verb}! {user.mention} → {emoji}{live_note}", ephemeral=True)


@bot.tree.command(name="vcdelete", description="Remove your (or someone else's) VC emoji.")
@discord.app_commands.describe(user="Who to remove")
async def vcdelete(interaction: discord.Interaction, user: discord.Member):
    if user.id != interaction.user.id and not has_manage_channels(interaction):
        await interaction.response.send_message(
            "You can only remove your own emoji. Removing others' requires **Manage Channels**.",
            ephemeral=True,
        )
        return

    if user.id not in user_emojis:
        await interaction.response.send_message(f"{user.mention} doesn't have an emoji assigned.", ephemeral=True)
        return

    removed = user_emojis.pop(user.id)
    save_assignments(user_emojis)

    if user.voice and user.voice.channel:
        await refresh_channel(user.voice.channel)

    await interaction.response.send_message(
        f"Removed {removed} from {user.mention}, they'll show as blank next time they're in a VC.",
        ephemeral=True,
    )


@bot.tree.command(name="vclist", description="See VC emoji assignments. Shows your current channel if you're in one, otherwise the whole server.")
async def vclist(interaction: discord.Interaction):
    if not user_emojis:
        await interaction.response.send_message(
            "Nobody has an emoji yet — use `/vcadd` to set one!",
            ephemeral=True,
        )
        return

    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    # if the user is in a VC, only show people in that channel
    member = interaction.guild.get_member(interaction.user.id)
    vc = member.voice.channel if member and member.voice else None

    if vc:
        members_to_show = [m for m in vc.members if m.id in user_emojis]
        header = f"**Emojis in #{vc.name}:**"
    else:
        members_to_show = None
        header = "**VC emoji assignments:**"

    if members_to_show is not None:
        if not members_to_show:
            await interaction.response.send_message(
                f"Nobody in #{vc.name if vc else 'that channel'} has an emoji assigned yet.",
                ephemeral=True,
            )
            return
        lines = [f"{user_emojis[m.id]}  {m.mention}" for m in members_to_show]
    else:
        lines = []
        for uid, emoji in user_emojis.items():
            member = interaction.guild.get_member(uid) if interaction.guild else None
            if not member:
                continue  # skip users no longer in the server
            lines.append(f"{emoji}  {member.mention}")
        if not lines:
            await interaction.response.send_message("No assignments for anyone currently in this server.", ephemeral=True)
            return

    await interaction.response.send_message(header + "\n" + "\n".join(lines), ephemeral=True)


@bot.tree.command(name="vcstatus", description="Manually refresh a voice channel's status.")
@discord.app_commands.describe(channel="The voice channel to refresh")
@discord.app_commands.default_permissions(manage_channels=True)
async def vcstatus(interaction: discord.Interaction, channel: discord.VoiceChannel):
    status = pick_status_for(channel)
    ok = await update_channel_status(channel, status)
    if ok:
        label = readable_emoji(status) if status else "(cleared)"
        await interaction.response.send_message(f"Done! **{channel.name}** is now showing {label}.", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"Something went wrong with **{channel.name}**, check the console.",
            ephemeral=True,
        )


@bot.tree.command(name="vctoggle", description="Pause or resume the bot for the VC you're currently in.")
@discord.app_commands.default_permissions(manage_channels=True)
async def vctoggle(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    member = interaction.guild.get_member(interaction.user.id)
    if not member or not member.voice or not member.voice.channel:
        await interaction.response.send_message("You need to be in a voice channel to use this.", ephemeral=True)
        return

    channel = member.voice.channel

    if channel.id in disabled_channels:
        disabled_channels.discard(channel.id)
        await interaction.response.send_message(
            f"Resumed **#{channel.name}** — statuses will update automatically again.",
            ephemeral=True,
        )
    else:
        disabled_channels.add(channel.id)
        await interaction.response.send_message(
            f"Paused **#{channel.name}** — statuses won't update until you toggle it back on.",
            ephemeral=True,
        )


@bot.event
async def setup_hook():
    synced = await bot.tree.sync()
    print(f"Synced {len(synced)} command(s): {[c.name for c in synced]}")


bot.run(DISCORD_TOKEN)
