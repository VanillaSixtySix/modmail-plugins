from discord.ext import commands
import discord
import asyncio

class MediaUploader(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.coll = bot.db.logs

    updated_attachments = {}

    async def _save_attachments(self, message):
        if message.attachments:
            for attachment in message.attachments:
                file_name = f"{message.id}-{attachment.id}-{attachment.filename}"
                await attachment.save(f"/var/www/s.neurosama.ai/attachments/{file_name}")
                self.updated_attachments[attachment.id] = attachment
                self.updated_attachments[attachment.id].filename = file_name
                self.updated_attachments[attachment.id].url = f"https://s.neurosama.ai/attachments/{file_name}"

    @commands.Cog.listener()
    async def on_thread_reply(self, thread, from_mod: bool, message: discord.Message, anonymous: bool, plain: bool):
        await self._save_attachments(message)

    @commands.Cog.listener()
    async def on_thread_close(self, thread, closer, silent: bool, delete_channel: bool, message: str, scheduled: bool):
        await self.update_attachment_urls(thread.channel.id)

    async def update_attachment_urls(self, thread_channel_id: int):
        await asyncio.sleep(5)
        channel_id = str(thread_channel_id)
        document = await self.coll.find_one({"channel_id": channel_id})
        if not document:
            return
        
        for msg in document.get("messages", []):
            for attachment in msg.get("attachments", []):
                attachment_id = attachment["id"]
                if attachment_id in self.updated_attachments:
                    attachment["filename"] = self.updated_attachments[attachment_id].filename
                    attachment["url"] = self.updated_attachments[attachment_id].url
                    del self.updated_attachments[attachment_id]
        
        await self.coll.update_one({"channel_id": channel_id}, {"$set": {"messages": document["messages"]}})


async def setup(bot):
    await bot.add_cog(MediaUploader(bot))