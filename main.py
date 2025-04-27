import os
import glob
import logging
import re
import yt_dlp
import instaloader
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackContext,
    CallbackQueryHandler,
    JobQueue,
)
from telegram.error import BadRequest
from datetime import datetime, timedelta

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_USERNAME = "@indown_channel"  # Change to your channel
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is not set.")

# Regex patterns for supported platforms
INSTAGRAM_REGEX = r"(https?:\/\/www\.instagram\.com\/(?:p|reel|tv|stories)\/[^\s]+)"
TIKTOK_REGEX = r"(https?:\/\/(?:www\.|vm\.|m\.)?tiktok\.com\/(?:@[^\s]+\/video\/\d+|t\/[^\s]+\/|v\/\d+\.html|[\w-]+\/))"
TWITTER_REGEX = r"(https?:\/\/(?:twitter\.com|x\.com)\/[^\s]+\/status\/\d+)"

# Initialize Instaloader
L = instaloader.Instaloader(
    download_pictures=True,
    download_videos=True,
    download_video_thumbnails=False,
    compress_json=False
)

# Command handler for /start
async def start(update: Update, context: CallbackContext):
    try:
        user_id = update.message.from_user.id
        
        # Check if user needs to verify subscription
        if not context.user_data.get(user_id, {}).get("verified", False):
            await show_subscription_buttons(update, context)
        else:
            await update.message.reply_text("You're already verified! Send me a link to download content.")
    except Exception as e:
        logger.error(f"Error in start command: {e}")
        await update.message.reply_text("An error occurred. Please try again later.")

# Show subscription buttons
async def show_subscription_buttons(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("📢 Subscribe", url="https://t.me/+RL-78Y_Y188wM2Jh")],
        [InlineKeyboardButton("✅ Verify Subscription", callback_data="verify_sub")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    start_message = "Send me a valid Instagram, TikTok, or Twitter video link, and I will send you the video. Please verify your subscription to use the bot."
    
    if update.callback_query:
        await update.callback_query.edit_message_text(start_message, reply_markup=reply_markup)
    else:
        await update.message.reply_text(start_message, reply_markup=reply_markup)

# Check if user is subscribed
async def is_user_subscribed(user_id: int, context: CallbackContext) -> bool:
    try:
        chat_member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return chat_member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]
    except Exception as e:
        logger.error(f"Error checking subscription: {e}")
        return False

# Verify subscription callback
async def handle_verify_subscription(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if await is_user_subscribed(user_id, context):
        # Store verification with timestamp
        context.user_data[user_id] = {
            "verified": True,
            "verified_at": datetime.now().timestamp()
        }
        
        # Schedule verification reset after 24 hours
        context.job_queue.run_once(
            reset_verification,
            when=timedelta(hours=24),
            user_id=user_id
        )
        
        await query.edit_message_text("✅ You are subscribed! You can now use the bot.")
    else:
        await query.answer("❌ You are not subscribed. Please subscribe first.")
        await verif_message(update, context)

# Reset verification status after 24 hours
async def reset_verification(context: CallbackContext):
    job = context.job
    user_id = job.user_id
    
    if user_id in context.user_data:
        context.user_data[user_id]["verified"] = False
        del context.user_data[user_id]["verified_at"]
        
        try:
            await context.bot.send_message(
                user_id,
                "🔔 Your subscription verification has expired. Please verify your subscription again to continue using the bot.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📢 Subscribe", url="https://t.me/+RL-78Y_Y188wM2Jh")],
                    [InlineKeyboardButton("✅ Verify Subscription", callback_data="verify_sub")],
                ])
            )
        except Exception as e:
            logger.error(f"Error sending verification reset message: {e}")

# Verification message
async def verif_message(update: Update, context: CallbackContext):
    keyboard = [
        [InlineKeyboardButton("📢 Subscribe", url="https://t.me/+RL-78Y_Y188wM2Jh")],
        [InlineKeyboardButton("✅ Verify Subscription", callback_data="verify_sub")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = 'Please subscribe to our channel to use this bot.\n\nAfter subscribing, click "✅ Verify Subscription" to continue.'
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text=text, reply_markup=reply_markup)
    else:
        sent_message = await update.message.reply_text(text, reply_markup=reply_markup)
        context.user_data["verif_message_id"] = sent_message.message_id

# Download Instagram media
def download_instagram_media(url):
    try:
        shortcode = url.split("/p/")[1].split("/")[0] if "/p/" in url else url.split("/reel/")[1].split("/")[0]
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        caption = post.caption[:1024] if post.caption else ""

        L.download_post(post, target=f"ig_{shortcode}")
        downloaded_files = glob.glob(f"ig_{shortcode}/*")

        return downloaded_files, caption
    except Exception as e:
        logger.error(f"Instagram download failed: {e}")
        return None, None

# Download TikTok/Twitter media (updated)
def download_media(url):
    ydl_opts = {
        'outtmpl': 'dl_%(id)s.%(ext)s',
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'extractor_args': {
            'tiktok': {
                'video_data': 'direct',
            },
        },
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Referer': 'https://www.tiktok.com/',
        },
        'retries': 3,
        'socket_timeout': 30,
        'extract_timeout': 60,
        'quiet': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return None
            
            # Handle multi-item posts (e.g., TikTok albums)
            if 'entries' in info:
                return [entry['requested_downloads'][0]['filepath'] for entry in info['entries']]
            else:
                return info['requested_downloads'][0]['filepath']
    except Exception as e:
        logger.error(f"YT-DLP Error: {e}")
        return None

# Middleware to check subscription
async def check_subscription(update: Update, context: CallbackContext):
    if not update.message or not update.message.from_user:
        return

    user_id = update.message.from_user.id

    if context.user_data.get(user_id, {}).get("verified", False):
        await handle_message(update, context)
    else:
        await verif_message(update, context)

# Handle media downloads
async def handle_message(update: Update, context: CallbackContext):
    if not update.message or not update.message.text:
        return

    text = update.message.text
    platform = None

    if re.search(INSTAGRAM_REGEX, text):
        platform = "Instagram"
    elif re.search(TIKTOK_REGEX, text):
        platform = "TikTok"
    elif re.search(TWITTER_REGEX, text):
        platform = "Twitter"

    if not platform:
        await update.message.reply_text("❌ Invalid link! Send Instagram/TikTok/Twitter links only.")
        return

    sent_message = await update.message.reply_text(f"⏳ Downloading {platform} content...")

    try:
        if platform == "Instagram":
            media_files, caption = download_instagram_media(text)
            if not media_files:
                await update.message.reply_text("❌ Failed to download Instagram content.")
                return

            await sent_message.delete()

            for index, file in enumerate(media_files):
                try:
                    if file.endswith(('.mp4', '.mkv')):
                        if index == 0 and caption:
                            await update.message.reply_video(video=open(file, 'rb'), caption=caption)
                        else:
                            await update.message.reply_video(video=open(file, 'rb'))
                    elif file.endswith(('.jpg', '.jpeg', '.png')):
                        await update.message.reply_photo(photo=open(file, 'rb'))
                finally:
                    if os.path.exists(file):
                        os.remove(file)

        else:  # TikTok or Twitter
            media_file = download_media(text)
            if not media_file:
                await update.message.reply_text("❌ Failed to download. The video may be private/too long.")
                return

            await sent_message.delete()

            if isinstance(media_file, list):  # Multiple files (TikTok album)
                for file in media_file:
                    await update.message.reply_document(document=open(file, 'rb'))
            else:  # Single file
                await update.message.reply_video(video=open(media_file, 'rb'))

            if os.path.exists(media_file):
                os.remove(media_file)

    except Exception as e:
        logger.error(f"Error handling {platform} content: {e}")
        await update.message.reply_text("❌ An error occurred. Please try again.")

# Error handler
async def error_handler(update: Update, context: CallbackContext):
    logger.error(f"Update {update} caused error: {context.error}")
    if update.message:
        await update.message.reply_text("⚠️ An error occurred. Please try again later.")

# Main function
def main():
    application = Application.builder() \
        .token(BOT_TOKEN) \
        .read_timeout(60) \
        .write_timeout(60) \
        .build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(handle_verify_subscription, pattern="verify_sub"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_subscription))
    application.add_error_handler(error_handler)

    logger.info("Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()
