from discord.ext import commands
import discord
import asyncio
import boto3
import io
import os

class MediaUploader(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.coll = bot.db.logs
        
        # Initialize R2 client
        self.r2_client = boto3.client(
            's3',
            endpoint_url=os.getenv('S3_ENDPOINT'),
            aws_access_key_id=os.getenv('S3_ACCESS_KEY_ID'),
            aws_secret_access_key=os.getenv('S3_SECRET_ACCESS_KEY'),
            region_name=os.getenv('S3_REGION', 'auto')
        )
        self.bucket_name = os.getenv('S3_BUCKET')
        self.base_url = os.getenv('S3_CUSTOM_DOMAIN')

    updated_attachments = {}

    async def _save_attachments(self, message):
        if message.attachments:
            for attachment in message.attachments:
                file_name = f"{message.id}-{attachment.id}-{attachment.filename}"
                
                # Read attachment data into memory
                attachment_data = await attachment.read()
                
                # Upload to R2
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: self.r2_client.put_object(
                        Bucket=self.bucket_name,
                        Key=f"attachments/{file_name}",
                        Body=attachment_data,
                        ContentType=attachment.content_type or 'application/octet-stream'
                    )
                )
                
                # Update attachment info
                self.updated_attachments[attachment.id] = attachment
                self.updated_attachments[attachment.id].filename = file_name
                self.updated_attachments[attachment.id].url = f"{self.base_url}/attachments/{file_name}"

    @commands.Cog.listener()
    async def on_thread_reply(self, thread, from_mod: bool, message: discord.Message, anonymous: bool, plain: bool):
        await self._save_attachments(message)
        # Update attachment URLs in database if message has attachments
        if message.attachments:
            await self.update_attachment_urls(thread.channel.id)

    @commands.Cog.listener()
    async def on_thread_close(self, thread, closer, silent: bool, delete_channel: bool, message: str, scheduled: bool):
        # No longer updating attachment URLs here to avoid channel validity issues
        pass

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
