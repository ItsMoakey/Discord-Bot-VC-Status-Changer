### A Discord bot that updates voice channel statuses to show custom emojis for who's currently in them.

- To use, create a bot in the Discord Developer Portal, invite it to your server with the "Manage Channels" permission, and set its token as an environment variable named `DISCORD_TOKEN` (or put it in a `.env` file, see `.env.example`).
Users can set their own emoji with the `/vcadd` command, and optionally specify another user if they have the "Manage Channels" permission. Emojis can be either custom server emojis (e.g. `<:thonkang:219069250692841473>`) or any unicode emoji (e.g. `🐸`). When a user joins a voice channel, the bot will update the channel's status to show the assigned emojis of all users currently in that channel. If no one in the channel has an assigned emoji, it will show a default emoji (which is currently blank but can be customized). If everyone in the channel has their emoji removed, it will clear the status entirely. Bots count too: use `/vcadd` and pick the bot as the user to give it an emoji, and it shows in the status while it's in the channel.

- The bot needs the "Manage Channels" permission to update voice channel statuses, and users need that permission to set emojis for others or to manually refresh statuses.
- Other commands: `/vcdelete`, `/vclist`, `/vcstatus <channel>` (manual refresh), `/vctoggle` (pause/resume updates for the VC you're in).
- Assignments are stored in `vc_emojis.json` in the folder you run the bot from. Set `VC_EMOJI_FILE` to change that.

### Instagram embeds

Instagram links no longer embed in Discord, so when someone posts an Instagram post, reel, or share link the bot replies with the same link on an embed proxy (default `fxinstagram.com`) and hides the original empty preview.

- Enable **Message Content Intent** for the bot in the Developer Portal (Bot → Privileged Gateway Intents), otherwise it can't see links.
- Give the bot **Send Messages** and **Manage Messages** (the latter is only needed to hide the original preview; without it the bot still replies).
- Set `EMBED_DOMAIN` to use a different or self-hosted proxy, e.g. `EMBED_DOMAIN=ig.example.com`.
- Wrap a link in `<...>` to stop the bot from embedding it.

### Music

Plays audio from YouTube, SoundCloud and other sites supported by yt-dlp, with a per-server queue.

| Command | What it does |
|---|---|
| `/play <url or search>` | Play or queue a song. A playlist link (YouTube `/playlist?list=...`, SoundCloud sets) queues the first 50 songs; a video link with `&list=` on it plays just that video |
| `/move <position> [to]` | Move a song already in the queue to position 1 (next up), or to `to` |
| `/pause` / `/resume` | Pause or resume |
| `/skip` | Skip the current song |
| `/stop` | Stop and clear the queue |
| `/leave` | Disconnect |
| `/queue` | Show the queue |
| `/nowplaying` | Show the current song |
| `/volume <0-100>` | Set volume |
| `/loop` | Toggle looping the current song |
| `/shuffle` | Shuffle the queue |

- Give the bot **Connect** and **Speak**.
- To keep the channel tidy, "Loading..." and "Now playing" are one message that gets edited, and the next "Now playing" edits it again as long as it's still the newest message in the channel. Plain confirmations and errors (`/skip`, `/pause`, `/resume`, `/volume`, `/queue`, `/nowplaying`, "Nothing is playing", etc.) are only visible to the person who ran the command. Queue changes (`/play` adding a song, `/move`, `/shuffle`, `/loop`) and `/stop`/`/leave` stay public.
- Install [FFmpeg](https://ffmpeg.org/) and keep yt-dlp updated (`pip install -U yt-dlp`), since YouTube changes often.
- The bot leaves on its own when everyone else leaves the voice channel, and a few seconds after the music ends (`IDLE_LEAVE_SECONDS` in `cogs/music.py`). Queues are in memory and reset on restart.

### Setup

1. In the Developer Portal, enable the **Server Members Intent** and **Message Content Intent**.
2. Invite the bot with the `bot` and `applications.commands` scopes and the permissions above (View Channels, Send Messages, Manage Messages, Manage Channels, Connect, Speak).
3. Install and run:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env             # then put your token in .env
python vc_status_bot.py
```

Set `DEV_GUILD_ID` in `.env` while testing so slash commands appear instantly instead of after global sync.

### Layout

```
vc_status_bot.py        entry point, loads the cogs
cogs/vc_status.py       VC emoji statuses
cogs/instagram_embed.py Instagram link embeds
cogs/music.py           music player
```