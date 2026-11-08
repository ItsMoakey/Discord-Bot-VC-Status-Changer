"""Bot-owned, reusable incoming webhooks for durable public feature messages."""

import asyncio

import discord


class PublicMessageError(Exception):
    """Safe, actionable public-message failure for a requester or operator."""


class PublicMessages:
    WEBHOOK_NAME = "VC Bot"

    def __init__(self, bot):
        self.bot = bot
        self.webhooks = {}
        self.locks = {}

    @staticmethod
    def payload(content=None, *, embed=None, raw=False):
        if content is not None and embed is None and not raw:
            embed = discord.Embed(description=str(content), color=0x5865F2)
            content = None
        return {"content": content, "embed": embed, "allowed_mentions": discord.AllowedMentions.none()}

    async def webhook(self, channel):
        parent = channel.parent if isinstance(channel, discord.Thread) else channel
        if parent is None or not hasattr(parent, "webhooks"):
            raise PublicMessageError("Public webhook messages require a server text channel or thread.")
        member = parent.guild.me
        if member is None or not parent.permissions_for(member).manage_webhooks:
            raise PublicMessageError("Grant the bot Manage Webhooks in this channel (and its parent for threads), then try again.")
        async with self.locks.setdefault(parent.id, asyncio.Lock()):
            cached = self.webhooks.get(parent.id)
            if cached is not None:
                return parent.id, cached
            hooks = await parent.webhooks()
            hook = next((hook for hook in hooks if hook.name == self.WEBHOOK_NAME
                         and hook.user is not None and hook.user.id == self.bot.user.id
                         and hook.token is not None), None)
            if hook is None:
                hook = await parent.create_webhook(name=self.WEBHOOK_NAME, reason="Reusable public feature messages")
            self.webhooks[parent.id] = hook
            return parent.id, hook

    async def send(self, channel, content=None, *, embed=None, view=None, raw=False):
        parent_id = None
        try:
            parent_id, hook = await self.webhook(channel)
            kwargs = self.payload(content, embed=embed, raw=raw)
            parent = channel.parent if isinstance(channel, discord.Thread) else channel
            author = parent.guild.me or self.bot.user
            kwargs["username"] = author.display_name
            kwargs["avatar_url"] = str(author.display_avatar.url)
            if view is not None:
                kwargs["view"] = view
            if isinstance(channel, discord.Thread):
                kwargs["thread"] = channel
            return await hook.send(**kwargs, wait=True)
        except discord.NotFound as error:
            if parent_id is not None:
                self.webhooks.pop(parent_id, None)
            raise PublicMessageError("The public webhook or channel was deleted. Try the command again to recreate the webhook.") from error
        except discord.Forbidden as error:
            raise PublicMessageError("Grant the bot Manage Webhooks and access to this channel, then try again.") from error
        except discord.HTTPException as error:
            raise PublicMessageError("Discord could not publish the webhook message. Check channel access and try again.") from error

    async def private(self, interaction, content=None, *, embed=None, view=None, raw=False):
        kwargs = self.payload(content, embed=embed, raw=raw)
        if view is not None:
            kwargs["view"] = view
        if interaction.is_expired():
            return await interaction.user.send(**kwargs)
        if interaction.response.is_done():
            return await interaction.followup.send(**kwargs, ephemeral=True, wait=True)
        await interaction.response.send_message(**kwargs, ephemeral=True)
        return await interaction.original_response()

    async def reply(self, interaction, content=None, *, embed=None, view=None, ephemeral=False, raw=False):
        if ephemeral:
            return await self.private(interaction, content, embed=embed, view=view, raw=raw)
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            message = await self.send(interaction.channel, content, embed=embed, view=view, raw=raw)
        except PublicMessageError as error:
            await self.private(interaction, str(error))
            return None
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass
        return message

    async def edit(self, message, content=None, *, embed=None, view=discord.utils.MISSING):
        kwargs = self.payload(content, embed=embed)
        if view is not discord.utils.MISSING:
            kwargs["view"] = view
        try:
            return await message.edit(**kwargs)
        except discord.NotFound as error:
            for channel_id, hook in list(self.webhooks.items()):
                if hook.id == getattr(message, "webhook_id", None):
                    self.webhooks.pop(channel_id, None)
            raise PublicMessageError("The public webhook or message was deleted. Run the command again.") from error
        except discord.HTTPException as error:
            raise PublicMessageError("Discord could not update the webhook message. Check channel access and Manage Webhooks permission.") from error

    async def delete(self, message):
        """Delete only a message belonging to this channel's managed webhook."""
        try:
            _, hook = await self.webhook(message.channel)
            if getattr(message, "webhook_id", None) != hook.id:
                raise PublicMessageError("That message does not belong to the bot's managed webhook.")
            kwargs = {"thread": message.channel} if isinstance(message.channel, discord.Thread) else {}
            await hook.delete_message(message.id, **kwargs)
        except discord.NotFound:
            return
        except discord.HTTPException as error:
            raise PublicMessageError("The new controls were posted, but Discord could not remove the old panel.") from error
