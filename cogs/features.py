import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("features")

FEATURE_LABELS = {
    "vc_status": "Voice channel statuses",
    "instagram": "Instagram embeds",
    "music": "Music",
    "karaoke": "Karaoke",
    "join_sound": "Join sound",
}


class FeatureStore:
    """Per-guild switches, persisted before a new value becomes visible."""

    def __init__(self):
        self.path = Path(os.getenv("FEATURE_SETTINGS_FILE") or "feature_settings.json")
        self._settings: dict[str, dict[str, bool]] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("Feature settings must be a JSON object")
            for guild_id, settings in raw.items():
                if not guild_id.isdecimal() or not isinstance(settings, dict):
                    raise ValueError("Invalid guild feature settings")
                if any(key not in FEATURE_LABELS or not isinstance(value, bool)
                       for key, value in settings.items()):
                    raise ValueError("Invalid feature name or enabled value")
            self._settings = raw
        else:
            legacy_path = Path(
                os.getenv("JOIN_SOUND_SETTINGS_FILE") or "join_sound_settings.json"
            )
            migrated = {}
            if legacy_path.exists():
                raw = json.loads(legacy_path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or any(
                    not guild_id.isdecimal() or not isinstance(enabled, bool)
                    for guild_id, enabled in raw.items()
                ):
                    raise ValueError("Invalid legacy join sound settings")
                migrated = {
                    guild_id: {"join_sound": enabled}
                    for guild_id, enabled in raw.items()
                }
            self._save(migrated)
            self._settings = migrated

    def enabled(self, guild_id: int, feature: str) -> bool:
        if feature not in FEATURE_LABELS:
            raise ValueError(f"Unknown feature: {feature}")
        return self._settings.get(str(guild_id), {}).get(feature, True)

    def set_enabled(self, guild_id: int, feature: str, enabled: bool) -> None:
        if feature not in FEATURE_LABELS:
            raise ValueError(f"Unknown feature: {feature}")
        if not isinstance(enabled, bool):
            raise ValueError("Feature enabled value must be a boolean")
        guild_key = str(guild_id)
        settings = {
            **self._settings,
            guild_key: {**self._settings.get(guild_key, {}), feature: enabled},
        }
        self._save(settings)
        self._settings = settings

    def _save(self, settings: dict[str, dict[str, bool]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            ) as stream:
                temporary = stream.name
                json.dump(settings, stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    log.warning("Could not remove temporary feature settings %s", temporary)


class FeatureTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        command = interaction.command
        if interaction.guild_id is None or command is None:
            return True
        if command.name in {"features", "toggle", "joinsound"}:
            return True
        binding = getattr(command, "binding", None)
        cog_name = getattr(binding, "qualified_name", None)
        store = self.client.features
        feature = {"VCStatus": "vc_status", "Karaoke": "karaoke"}.get(cog_name)
        if cog_name == "Music":
            if command.name == "play":
                allowed = store.enabled(interaction.guild_id, "music")
            else:
                allowed = (
                    store.enabled(interaction.guild_id, "music")
                    or store.enabled(interaction.guild_id, "karaoke")
                )
            label = "Music" if command.name == "play" else "Music and karaoke"
        elif feature is not None:
            allowed = store.enabled(interaction.guild_id, feature)
            label = FEATURE_LABELS[feature]
        else:
            return True
        if not allowed:
            await interaction.response.send_message(
                f"{label} is disabled for this server. A member with Manage Server "
                "can enable it with `/toggle`.", ephemeral=True,
            )
        return allowed


class FeatureToggleButton(discord.ui.Button):
    def __init__(self, key: str, label: str, enabled: bool):
        super().__init__(
            label=f"{label}: {'on' if enabled else 'off'}",
            style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary,
            custom_id=f"features:{key}",
        )
        self.feature = key

    async def callback(self, interaction: discord.Interaction):
        await self.view.cog.set_feature(interaction, self.feature)


class FeatureSettingsView(discord.ui.View):
    def __init__(self, cog, guild_id: int):
        super().__init__(timeout=900)
        self.cog = cog
        self.guild_id = guild_id
        for key, label in FEATURE_LABELS.items():
            self.add_item(FeatureToggleButton(
                key, label, cog.bot.features.enabled(guild_id, key)
            ))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message("This panel belongs to another server.", ephemeral=True)
            return False
        if not interaction.permissions.manage_guild:
            await interaction.response.send_message(
                "You need Manage Server to change feature settings.", ephemeral=True,
            )
            return False
        return True


class Features(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._toggle_locks: dict[int, asyncio.Lock] = {}

    def status_embed(self, guild_id: int) -> discord.Embed:
        embed = discord.Embed(title="Server features", color=discord.Color.blurple())
        for key, label in FEATURE_LABELS.items():
            embed.add_field(
                name=label, value="On" if self.bot.features.enabled(guild_id, key) else "Off",
                inline=True,
            )
        embed.set_footer(text="Manage Server is required to change settings.")
        return embed

    @app_commands.command(name="features", description="Show this server's feature settings and toggle buttons")
    @app_commands.guild_only()
    async def features(self, interaction: discord.Interaction):
        view = FeatureSettingsView(self, interaction.guild_id) if interaction.permissions.manage_guild else None
        await self.bot.messages.reply(
            interaction, embed=self.status_embed(interaction.guild_id), view=view, ephemeral=True,
        )

    @app_commands.command(name="toggle", description="Enable or disable a system for this server")
    @app_commands.describe(
        system="The system to configure", enabled="Turn it on or off; omit to flip its setting",
    )
    @app_commands.choices(system=[
        app_commands.Choice(name=label, value=key) for key, label in FEATURE_LABELS.items()
    ])
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def toggle(
        self, interaction: discord.Interaction, system: app_commands.Choice[str],
        enabled: bool | None = None,
    ):
        await self.set_feature(interaction, system.value, enabled)

    async def set_feature(self, interaction: discord.Interaction, feature: str, enabled: bool | None = None):
        if not interaction.permissions.manage_guild:
            return await interaction.response.send_message(
                "You need Manage Server to change feature settings.", ephemeral=True,
            )
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        lock = self._toggle_locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            if feature not in FEATURE_LABELS:
                return await interaction.followup.send("Unknown feature.", ephemeral=True)
            value = not self.bot.features.enabled(guild_id, feature) if enabled is None else enabled
            try:
                self.bot.features.set_enabled(guild_id, feature, value)
            except OSError:
                log.exception("Could not save feature settings for guild %s", guild_id)
                return await interaction.followup.send(
                    "Couldn't save the setting. No feature setting was changed.", ephemeral=True,
                )
            failed = False
            for cog in tuple(self.bot.cogs.values()):
                hook = getattr(cog, "on_feature_toggle", None)
                if hook is None:
                    continue
                try:
                    await hook(interaction.guild, feature, value)
                except Exception:
                    failed = True
                    log.exception("Feature effect failed for %s in guild %s", cog.qualified_name, guild_id)
            text = f"{FEATURE_LABELS[feature]} is now **{'on' if value else 'off'}** for this server."
            if failed:
                text += (
                    " The setting was saved, but an immediate effect failed; "
                    "existing activity may not fully reflect it yet. Check the bot logs."
                )
            if interaction.type is discord.InteractionType.component:
                await interaction.edit_original_response(
                    embed=self.status_embed(guild_id),
                    view=FeatureSettingsView(self, guild_id),
                )
            await self.bot.messages.reply(interaction, text, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Features(bot))
