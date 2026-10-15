### A Discord bot that updates voice channel statuses to show custom emojis for who's currently in them.

- To use, create a bot in the Discord Developer Portal, invite it to your server with the "Manage Channels" permission, and set its token as an environment variable named `DISCORD_TOKEN`.
Users can set their own emoji with the `/vcadd` command, and optionally specify another user if they have the "Manage Channels" permission. Emojis can be either custom server emojis (e.g. `<:thonkang:219069250692841473>`) or any unicode emoji (e.g. `🐸`). When a user joins a voice channel, the bot will update the channel's status to show the assigned emojis of all users currently in that channel. If no one in the channel has an assigned emoji, it will show a default emoji (which is currently blank but can be customized). If everyone in the channel has their emoji removed, it will clear the status entirely

- Add token in the code or set it as an environment variable before running the script. Note that the bot needs the "Manage Channels" permission to update voice channel statuses, and users need that permission to set emojis for others or to manually refresh statuses.

### Instagram embeds

Instagram links no longer embed in Discord, so when someone posts an Instagram post, reel, or share link the bot replies with the same link on an embed proxy (default `kkinstagram.com`) and hides the original empty preview.

- Enable **Message Content Intent** for the bot in the Developer Portal (Bot → Privileged Gateway Intents), otherwise it can't see links.
- Give the bot **Send Messages** and **Manage Messages** (the latter is only needed to hide the original preview; without it the bot still replies).
- Set `EMBED_DOMAIN` to use a different or self-hosted proxy, e.g. `EMBED_DOMAIN=ig.example.com`.
- Wrap a link in `<...>` to stop the bot from embedding it.
