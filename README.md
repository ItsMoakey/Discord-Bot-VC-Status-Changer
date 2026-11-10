### Discord bot with VC emoji statuses, Instagram embeds, music, and karaoke.

- To use, create a bot in the Discord Developer Portal, invite it to your server with the "Manage Channels" permission, and set its token as an environment variable named `DISCORD_TOKEN` (or put it in a `.env` file, see `.env.example`).
Users can set their own emoji with the `/vcadd` command, and optionally specify another user if they have the "Manage Channels" permission. Emojis can be either custom server emojis (e.g. `<:thonkang:219069250692841473>`) or any unicode emoji (e.g. `🐸`). When a user joins a voice channel, the bot will update the channel's status to show the assigned emojis of all users currently in that channel. If no one in the channel has an assigned emoji, it will show a default emoji (which is currently blank but can be customized). If everyone in the channel has their emoji removed, it will clear the status entirely. Bots count too: use `/vcadd` and pick the bot as the user to give it an emoji, and it shows in the status while it's in the channel.

- The bot needs the "Manage Channels" permission to update voice channel statuses, and users need that permission to set emojis for others or to manually refresh statuses.
- Other commands: `/vcdelete`, `/vclist`, `/vcstatus <channel>` (manual refresh), `/vctoggle` (pause/resume updates for the VC you're in).
- Assignments are stored in `vc_emojis.json` in the folder you run the bot from. Set `VC_EMOJI_FILE` to change that.

### Feature toggles

Use `/features` to see this server's settings. Members with **Manage Server** get private on/off buttons for each system. `/toggle system:<system> enabled:False` disables a system, `enabled:True` enables it, or omitting `enabled` flips its setting. Buttons and slash commands share the same permission checks and saved settings.

The choices are **Voice channel statuses**, **Instagram embeds**, **Music**, **Karaoke**, and **Join sound**. All start enabled and are saved independently per server in `feature_settings.json`; set `FEATURE_SETTINGS_FILE` to change its path.

- Music and karaoke are independent: disabling music stops/removes normal tracks, but karaoke and its shared playback controls still work. Disabling karaoke cancels preparation and stops/removes karaoke tracks without removing normal music.
- Disabling Instagram stops new embed replies. Disabling VC statuses clears automatically managed statuses and stops updates; enabling it refreshes them. Channels paused with `/vctoggle` are left alone.
- `/vctoggle` remains a per-channel pause, while `/toggle` affects the whole server. `/joinsound` still works for members with **Manage Channels** and changes the same saved join-sound setting.
- Existing `join_sound_settings.json` preferences are imported when the feature settings file is first created. If the old location was customized, keep `JOIN_SOUND_SETTINGS_FILE` set for that first launch; subsequent changes use only the new feature settings file.


### Instagram embeds

Instagram links no longer embed in Discord, so when someone posts an Instagram post, reel, or share link the bot publishes the proxy link through a reusable webhook (default proxy `fxinstagram.com`) and hides the original empty preview.

- Enable **Message Content Intent** for the bot in the Developer Portal (Bot → Privileged Gateway Intents), otherwise it can't see links.
- Give the bot **Send Messages**, **Embed Links**, **Manage Webhooks**, and **Manage Messages** (the latter is only needed to hide the original preview).
- Set `EMBED_DOMAIN` to use a different or self-hosted proxy, e.g. `EMBED_DOMAIN=ig.example.com`.
- Wrap a link in `<...>` to stop the bot from embedding it.

### Public messages and buttons

Public playback updates, karaoke preparation/lyrics, and Instagram previews use a reusable bot-owned webhook per channel. Messages display the bot's actual server name and avatar; the underlying integration is named **VC Bot** for discovery. Give the bot **Manage Webhooks** in each channel where these features are used (on the parent channel for threads). Webhook credentials are not written to settings files. Missing access produces an actionable private error instead of silently reverting to normal bot messages.

Reusable webhooks do not have interaction tokens' 15-minute expiry. Private errors, queue queries, and administrative confirmations remain private interaction responses.


### Music

Plays audio from YouTube, SoundCloud and other sites supported by yt-dlp, with a per-server queue.

| Command | What it does |
|---|---|
| `/play <url or search>` | Play or queue a song. A playlist link (YouTube `/playlist?list=...`, SoundCloud sets) queues the first 50 songs; a video link with `&list=` on it plays just that video |
| `/karaoke <url or search>` | Search for one song or use a supported single-media link, then prepare a vocal-removed instrumental and queue it with synced or paginated lyrics |
| `/controls` | Post a playback panel with Play, Karaoke, Queue, Pause/Resume, Skip, Loop, Shuffle, Leave, and Bring controls here |
| `/move <position> [to]` | Move a song already in the queue to position 1 (next up), or to `to` |
| `/pause` / `/resume` | Pause or resume |
| `/skip` | Skip the current song |
| `/leave` | End playback, clear the queue, and disconnect |
| `/queue` | Show the queue |
| `/nowplaying` | Show the current song |
| `/loop` | Toggle looping the current song |
| `/shuffle` | Shuffle the queue |
| `/joinsound [enabled]` | Toggle the arrival clip for this server, or explicitly set on/off (Manage Channels required) |

- Give the bot **Connect** and **Speak**.
- Playback status is one rich webhook panel reused when songs start or change; temporary loading messages are removed once the panel updates. The nine buttons use short 3/3/2/1 rows, with Bring controls here on its own row. Buttons survive a bot restart and resolve current server state rather than a stale track. Playback-changing controls require being in the bot's voice channel; Queue is readable server-wide. Play/Karaoke open an input form and recheck feature/voice permissions when submitted.
- The canonical panel's channel/message IDs are saved in `playback_panels.json` (override with `PLAYBACK_PANEL_FILE`) so starting playback after a restart reuses it rather than creating duplicates. This contains no webhook credentials; queues themselves still reset on restart.
- **Bring controls here** (on playback and active lyrics messages) posts fresh controls at the bottom of chat, then deletes the previous controls. It also removes an old panel used after a restart; lyric messages themselves are not deleted. If posting fails, the old controls remain available. `/controls` does the same without needing to scroll to a button.
- Install [FFmpeg](https://ffmpeg.org/) and keep yt-dlp updated (`pip install -U yt-dlp`), since YouTube changes often.
- Normal `/play` streams audio directly; it does not run vocal separation or wait for the full track to download. Media lookup runs alongside voice connection/arrival playback, and immediate playback reuses that lookup's stream URL. Queued and looped songs resolve a fresh URL when they actually start. Network extraction, Discord voice negotiation, and the optional arrival clip can still add startup time.
- The bot leaves on its own when everyone else leaves the voice channel, and a few seconds after the music ends (`IDLE_LEAVE_SECONDS` in `cogs/music.py`). Pending song/playlist lookups and karaoke preparation do not count as idle time. Once the last pending request fails or is cancelled, a fresh idle countdown starts if nothing is playing or queued. Queues are in memory and reset on restart.

#### Join sound

When `/play` first connects the bot to voice, it plays your local audio clip before starting music. Adding songs or moving an existing connection does not replay the clip or interrupt the current song.

1. Put a short audio file named `join.mp3` in the folder you run the bot from. MP3, WAV, Ogg, and other FFmpeg-supported formats work; for a different name or location, set `JOIN_SOUND_FILE` in `.env` and restart the bot.
2. Join sounds are enabled by default. Use `/joinsound` to toggle them for the current server, or `/joinsound enabled:False` / `/joinsound enabled:True` to explicitly turn them off/on. Requires **Manage Channels**. Changes apply to future joins, not a clip already playing.
3. The toggle is saved with the other features in `feature_settings.json` and survives bot restarts and voice disconnects. Set `FEATURE_SETTINGS_FILE` to change the storage path. Relative audio/settings paths are resolved from the folder you run the bot from.

The clip requires FFmpeg, **Connect**, and **Speak**—no Soundboard permission or Discord sound ID. A missing clip or playback-start failure is logged and music continues without the announcement. Keep the clip short: the first song waits for it to finish.

### Karaoke

`/karaoke <url or search>` uses the same yt-dlp search and supported-site input handling as `/play`, but prepares just one finite song. Searches select one result; single-media links from YouTube, SoundCloud, and other yt-dlp-supported sites are accepted. Live streams, upcoming media, and pure playlist links are rejected; a video link with a playlist parameter still selects just that video. It downloads the audio, decodes it with FFmpeg, and separates vocals with the engine selected by `KARAOKE_BACKEND`: **`mdx`** (recommended default, dedicated two-stem `UVR-MDX-NET-Inst_Main.onnx`) or **`demucs`** (`htdemucs`, supported quality alternative). The resulting `no_vocals.wav` uses the existing music queue, pause/resume, skip, leave, and loop controls. Only use audio you have permission to download and process.

MDX is recommended when preparation speed matters. Demucs is not obsolete: it can separate some songs more cleanly, so both setups remain supported. Neither engine guarantees artifact-free audio.

`KARAOKE_DEVICE=auto` selects `cuda` when the installed PyTorch build reports an available CUDA/ROCm device, otherwise `cpu`. Set `cpu` to force CPU, or `cuda` to require a compatible GPU; an unavailable explicitly requested GPU fails with an explanation rather than silently using CPU. The selected engine, device, PyTorch build, and HIP version are recorded in the preparation worker log. Neither engine uses Vulkan.

A CPU-only PyTorch build cannot use a GPU, even when Vulkan or another application's GPU runtime works. Use the GPU setup below to install a compatible CUDA or ROCm build for your hardware.

Demucs preparation speed can be tuned without editing code (these settings do not affect MDX):

- `KARAOKE_SHIFTS=1` and `KARAOKE_OVERLAP=0.25` preserve Demucs's standard settings.
- For faster preparation, try `KARAOKE_SHIFTS=0` and `KARAOKE_OVERLAP=0.1`. This skips random-shift augmentation and reduces repeated work at segment boundaries, but can worsen separation quality or transitions. Speed gains depend on the hardware and track.
- Shifts must be a non-negative integer; overlap must be at least `0` and less than `1`. More shifts or overlap generally mean more processing.

Set these in `.env` (or the service/container environment) and restart the bot. Existing service environment variables take precedence over `.env`. Both engines still process the whole track before playback starts. Demucs's `--two-stems` combines its four estimated sources; MDX uses a model trained for two stems. The engines can produce different vocal bleed and musical artifacts, so compare representative songs before choosing one.

- LRCLIB lookup starts after download metadata is available and runs alongside FFmpeg decoding and vocal separation to reduce startup waiting. Unlike `/play` streaming, karaoke still downloads and processes the whole track before playback; it never streams partially separated audio. Compatible timestamped lyrics advance in a webhook message using the actual PCM playback clock, so pausing also freezes lyric progress. Updates are paced roughly two seconds apart; different recordings or video intros can still cause timing differences.
- If usable synchronization is unavailable, normal lyrics are shown with **Previous** and **Next** page buttons. A different-duration recording may supply plain lyrics, but not its timestamps. Synced songs also have a **Full lyrics** browsing button. These are shared controls: changing the page changes it for everyone.
- If no matching lyrics are found or the lyrics service is unavailable, the instrumental still plays with an explanatory message. Vocal-separation failure never falls back to the original vocal audio.
- Discord cannot force clients to scroll. Lyrics advance inside one durable webhook message instead, without a 15-minute handoff. Finished sessions disable their lyric controls.
- Separation can take several minutes, especially on CPU. The first run downloads model weights and needs internet access, free disk space, and sufficient RAM. Vocal bleed/artifacts are possible. One preparation runs at a time across the bot, with at most one pending request per server.
- Prepared audio is temporary and removed when the track finishes, is skipped, karaoke is disabled, or the bot disconnects. Model weights remain cached for reuse.

#### Setup 1: MDX (recommended default)

Use a separate **64-bit Python 3.11** environment. Keep any working Demucs environment available if you want to compare results or switch engines:

```bash
python3.11 -m venv .venv-mdx
source .venv-mdx/bin/activate
python -m pip install --upgrade pip
```

On Windows use `py -3.11 -m venv .venv-mdx`, then `.\.venv-mdx\Scripts\Activate.ps1`.

1. Install FFmpeg and put `ffmpeg` and `ffprobe` on PATH. If pip needs to build `diffq` (an audio-separator dependency), install a C/C++ compiler and development headers matching your Python interpreter first. Minimal Linux systems may also need their OpenMP runtime (`libgomp1` on Debian/Ubuntu).
2. For **GPU inference**, first install a vendor-matched **PyTorch 2.12.0 / torchvision 0.27.0 / torchaudio** stack using the [PyTorch](https://pytorch.org/get-started/previous-versions/) or [ROCm](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/frameworks/pytorch/install.html) instructions for your GPU and OS. The requirements accept torchaudio 2.11.x or 2.12.x because vendor bundles can use different version numbering; use the version supplied with that PyTorch build. Select your actual GPU target if the vendor uses target-specific extras. Do not install the Demucs CPU/GPU requirements in this environment.
3. Install the bot and MDX dependencies:

   ```bash
   python -m pip install -r requirements.txt -r requirements-karaoke-mdx.txt
   python -m pip check
   ```

   For **CPU inference**, omit step 2; pip installs the default PyTorch packages. The `audio-separator[cpu]` extra supplies ONNX loading/conversion dependencies, **not** the inference device: this worker deliberately converts the model to native PyTorch and verifies its device. An ONNX Runtime warning about a missing CUDA provider does not describe this native inference path. Fixed-statistics BatchNorm layers use torchvision's equivalent inference-only implementation, avoiding runtime BatchNorm kernel compilation.

   MDX requests mixed-precision autocast on GPU and uses full precision on CPU; the effective precision is logged. This is separate from Demucs's shifts/overlap tuning.

4. Set the following in `.env`:

   ```dotenv
   KARAOKE_BACKEND=mdx
   KARAOKE_DEVICE=cuda
   ```

   Use `cpu` instead of `cuda` for CPU-only use, or `auto` for automatic selection. The model downloads on first use and is cached under `~/.cache/discord-vc-bot/models`; set `KARAOKE_MODEL_DIR` to put that cache elsewhere.
5. Start with this environment's interpreter: `python vc_status_bot.py`. Test a short `/karaoke` request before a full track. GPU kernel compilation and model downloads can make the first request slower.

To switch to Demucs, stop the bot, set `KARAOKE_BACKEND=demucs`, and start it with the Demucs environment's interpreter. For services/containers, change the interpreter, environment, FFmpeg paths, and GPU permissions in the server configuration too; do not edit repository source. Keep tokens, local settings, model weights, and environments out of Git. A fresh clone needs the selected dependency setup, your `.env`, and these server-side runtime settings—not machine-specific code changes.



#### Setup 2: Demucs (supported quality alternative)

Use a **64-bit Python 3.11** environment for karaoke: the pinned dependencies do not support Python 3.14. Set `KARAOKE_BACKEND=demucs` in `.env`. Install FFmpeg on PATH, then on Windows:

```powershell
py -3.11 -m venv .venv-karaoke
.\.venv-karaoke\Scripts\Activate.ps1
python -m pip install -r requirements.txt -r requirements-karaoke.txt
python vc_status_bot.py
```

On other platforms, create the environment with `python3.11 -m venv .venv-karaoke` and activate it with `source .venv-karaoke/bin/activate`. Install both requirements files in the same environment used to run the bot. The optional dependency file is not needed for normal music, VC statuses, or Instagram embeds.

##### Demucs GPU acceleration (CUDA or ROCm)

Create a separate **64-bit Python 3.11** environment for GPU karaoke rather than upgrading a working environment in place:

```bash
python3.11 -m venv .venv-gpu
source .venv-gpu/bin/activate
python -m pip install --upgrade pip
```

On Windows, create it with `py -3.11 -m venv .venv-gpu` and activate it with `.\.venv-gpu\Scripts\Activate.ps1`.

1. Install the GPU driver and a matching **PyTorch 2.11.0 / torchaudio 2.11.0** pair using the instructions for your platform:
   - **NVIDIA:** select the CUDA build compatible with your driver from the [PyTorch installation instructions](https://pytorch.org/get-started/previous-versions/).
   - **AMD:** check your GPU and operating system against the [ROCm support matrix](https://rocm.docs.amd.com/en/latest/compatibility/compatibility-matrix.html), then follow the [ROCm PyTorch installation instructions](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/frameworks/pytorch/install.html). If the package distribution uses GPU-target extras, select the target reported by your hardware; do not copy another GPU's architecture or use an architecture override.
2. Install the matching CPU TorchCodec wheel for audio file I/O, then the bot's GPU dependencies:

   ```bash
   python -m pip install --index-url https://download.pytorch.org/whl/cpu torchcodec==0.11.0
   python -m pip install -r requirements.txt -r requirements-karaoke-gpu.txt
   python -m pip check
   ```

   Use `requirements-karaoke-gpu.txt` **instead of** `requirements-karaoke.txt`; their PyTorch versions must not be mixed. Install the vendor's PyTorch build first: the generic requirements file does not choose a GPU backend or install a GPU driver. Vendor suffixes such as `+rocm...` satisfy the base version pins. TorchCodec's CPU wheel handles file I/O only; it does not move model inference to the CPU.
3. Install FFmpeg with shared libraries. Both `ffmpeg` and `ffprobe` must be on PATH, and TorchCodec must be able to load the FFmpeg shared libraries. If using a custom FFmpeg distribution, configure its binary and library search paths for your platform. Minimal Linux installations may also need their distribution's OpenMP runtime package, such as `libgomp1` on Debian/Ubuntu.
4. Verify GPU discovery in the same environment that will run the bot:

   ```bash
   python -c "import torch; assert torch.cuda.is_available(), 'No compatible GPU available'; print(torch.__version__, torch.version.hip, torch.cuda.get_device_properties(0))"
   ```

5. Set `KARAOKE_DEVICE=cuda` in `.env` to require GPU inference, then start the bot with `python vc_status_bot.py`. PyTorch uses the `cuda` API name for both NVIDIA CUDA and AMD ROCm/HIP devices.

For a background service or container, use this environment's Python interpreter and provide the same device setting, FFmpeg paths, library paths, and GPU access permissions. Shell activation alone does not configure a service. Keep the previous environment and service configuration available for rollback.

GPU availability does not guarantee every model operation is supported. Test a short `/karaoke` request to verify separation, WAV export, and voice playback before processing longer tracks. No Vulkan inference backend is provided.

### Setup

The Discord voice dependencies are pinned as a tested pair (`discord.py 2.7.1` and `davey 0.1.0`). With `davey 0.2.0`, live testing found that DAVE negotiation never became ready and playback was silent despite valid PCM and outgoing UDP packets. Keep the pair pinned when reinstalling.

1. In the Developer Portal, enable the **Server Members Intent** and **Message Content Intent**.
2. Invite the bot with the `bot` and `applications.commands` scopes and the permissions above (View Channels, Send Messages, Embed Links, Manage Webhooks, Manage Messages, Manage Channels, Connect, Speak).
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
cogs/music_controls.py  persistent playback buttons and song input forms
cogs/messages.py        reusable public webhook delivery
cogs/karaoke.py         instrumental preparation and live/paginated lyrics
cogs/karaoke_worker.py  isolated Demucs/MDX separation worker
```