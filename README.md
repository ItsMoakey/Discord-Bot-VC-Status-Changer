### Discord bot with VC emoji statuses, Instagram embeds, music, and karaoke.

- To use, create a bot in the Discord Developer Portal, invite it to your server with the "Manage Channels" permission, and set its token as an environment variable named `DISCORD_TOKEN` (or put it in a `.env` file, see `.env.example`).
Users can set their own emoji with the `/vcadd` command, and optionally specify another user if they have the "Manage Channels" permission. Emojis can be either custom server emojis (e.g. `<:thonkang:219069250692841473>`) or any unicode emoji (e.g. `🐸`). When a user joins a voice channel, the bot will update the channel's status to show the assigned emojis of all users currently in that channel. If no one in the channel has an assigned emoji, it will show a default emoji (which is currently blank but can be customized). If everyone in the channel has their emoji removed, it will clear the status entirely. Bots count too: use `/vcadd` and pick the bot as the user to give it an emoji, and it shows in the status while it's in the channel.

- The bot needs the "Manage Channels" permission to update voice channel statuses, and users need that permission to set emojis for others or to manually refresh statuses.
- Other commands: `/vcdelete`, `/vclist`, `/vcstatus <channel>` (manual refresh), `/vctoggle` (pause/resume updates for the VC you're in).
- Assignments are stored in `vc_emojis.json` in the folder you run the bot from. Set `VC_EMOJI_FILE` to change that.

### Feature toggles

Use `/features` to see this server's settings. Members with **Manage Server** can use `/toggle system:<system> enabled:False` to disable a system, `enabled:True` to enable it, or omit `enabled` to flip the current setting.

The choices are **Voice channel statuses**, **Instagram embeds**, **Music**, **Karaoke**, and **Join sound**. All start enabled and are saved independently per server in `feature_settings.json`; set `FEATURE_SETTINGS_FILE` to change its path.

- Music and karaoke are independent: disabling music stops/removes normal tracks, but karaoke and its shared playback controls still work. Disabling karaoke cancels preparation and stops/removes karaoke tracks without removing normal music.
- Disabling Instagram stops new embed replies. Disabling VC statuses clears automatically managed statuses and stops updates; enabling it refreshes them. Channels paused with `/vctoggle` are left alone.
- `/vctoggle` remains a per-channel pause, while `/toggle` affects the whole server. `/joinsound` still works for members with **Manage Channels** and changes the same saved join-sound setting.
- Existing `join_sound_settings.json` preferences are imported when the feature settings file is first created. If the old location was customized, keep `JOIN_SOUND_SETTINGS_FILE` set for that first launch; subsequent changes use only the new feature settings file.


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
| `/karaoke <youtube-link>` | Prepare a vocal-removed instrumental and queue it with synced or paginated lyrics |
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
| `/joinsound [enabled]` | Toggle the arrival clip for this server, or explicitly set on/off (Manage Channels required) |

- Give the bot **Connect** and **Speak**.
- To keep the channel tidy, "Loading..." and "Now playing" are one message that gets edited, and the next "Now playing" edits it again as long as it's still the newest message in the channel. Plain confirmations and errors (`/skip`, `/pause`, `/resume`, `/volume`, `/queue`, `/nowplaying`, "Nothing is playing", etc.) are only visible to the person who ran the command. Queue changes (`/play` adding a song, `/move`, `/shuffle`, `/loop`) and `/stop`/`/leave` stay public.
- Install [FFmpeg](https://ffmpeg.org/) and keep yt-dlp updated (`pip install -U yt-dlp`), since YouTube changes often.
- The bot leaves on its own when everyone else leaves the voice channel, and a few seconds after the music ends (`IDLE_LEAVE_SECONDS` in `cogs/music.py`). Queues are in memory and reset on restart.

#### Join sound

When `/play` first connects the bot to voice, it plays your local audio clip before starting music. Adding songs or moving an existing connection does not replay the clip or interrupt the current song.

1. Put a short audio file named `join.mp3` in the folder you run the bot from. MP3, WAV, Ogg, and other FFmpeg-supported formats work; for a different name or location, set `JOIN_SOUND_FILE` in `.env` and restart the bot.
2. Join sounds are enabled by default. Use `/joinsound` to toggle them for the current server, or `/joinsound enabled:False` / `/joinsound enabled:True` to explicitly turn them off/on. Requires **Manage Channels**. Changes apply to future joins, not a clip already playing.
3. The toggle is saved with the other features in `feature_settings.json` and survives bot restarts and voice disconnects. Set `FEATURE_SETTINGS_FILE` to change the storage path. Relative audio/settings paths are resolved from the folder you run the bot from.

The clip uses the music player's current volume and requires FFmpeg, **Connect**, and **Speak**—no Soundboard permission or Discord sound ID. A missing clip or playback-start failure is logged and music continues without the announcement. Keep the clip short: the first song waits for it to finish.

### Karaoke

`/karaoke <youtube-link>` downloads a single YouTube video's audio, decodes it with FFmpeg, and separates vocals using Demucs's `htdemucs` model on CPU. The resulting `no_vocals.wav` uses the existing music queue, volume, pause/resume, skip, stop, and loop controls. Only use audio you have permission to download and process.

- LRCLIB is searched for matching lyrics. Compatible timestamped lyrics advance in a webhook message using the actual PCM playback clock, so pausing also freezes lyric progress. Updates are paced roughly two seconds apart; different recordings or video intros can still cause timing differences.
- If usable synchronization is unavailable, normal lyrics are shown with **Previous** and **Next** page buttons. A different-duration recording may supply plain lyrics, but not its timestamps. Synced songs also have a **Full lyrics** browsing button. These are shared controls: changing the page changes it for everyone.
- If no matching lyrics are found or the lyrics service is unavailable, the instrumental still plays with an explanatory message. Vocal-separation failure never falls back to the original vocal audio.
- Discord cannot force clients to scroll. Lyrics advance inside one message instead. Interaction webhooks expire after 15 minutes, so queued/long-running lyric and preparation messages transfer to a normal bot message shortly before expiry. Give the bot **Send Messages** and **Embed Links**; no **Manage Webhooks** permission is needed.
- Separation can take several minutes, especially on CPU. The first run downloads model weights and needs internet access, free disk space, and sufficient RAM. Vocal bleed/artifacts are possible. One preparation runs at a time across the bot, with at most one pending request per server.
- Prepared audio is temporary and removed when the track finishes, is skipped/stopped, karaoke is disabled, or the bot disconnects. Model weights remain cached for reuse.

#### Karaoke dependencies

Use a **64-bit Python 3.11** environment for karaoke: the pinned PyTorch/torchaudio versions do not support Python 3.14. Install FFmpeg on PATH, then on Windows:

```powershell
py -3.11 -m venv .venv-karaoke
.\.venv-karaoke\Scripts\Activate.ps1
python -m pip install -r requirements.txt -r requirements-karaoke.txt
python vc_status_bot.py
```

On other platforms, create the environment with `python3.11 -m venv .venv-karaoke` and activate it with `source .venv-karaoke/bin/activate`. Install both requirements files in the same environment used to run the bot. The optional dependency file is not needed for normal music, VC statuses, or Instagram embeds.


### Setup

1. In the Developer Portal, enable the **Server Members Intent** and **Message Content Intent**.
2. Invite the bot with the `bot` and `applications.commands` scopes and the permissions above (View Channels, Send Messages, Embed Links, Manage Messages, Manage Channels, Connect, Speak).
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
cogs/features.py        saved server feature switches and slash-command gates
cogs/instagram_embed.py Instagram link embeds
cogs/music.py           music player
cogs/karaoke.py         instrumental preparation and live/paginated lyrics
```