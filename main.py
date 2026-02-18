import os
import json
import asyncio
import logging
import tempfile
import httpx
import time
import re
import random
import cv2
import numpy as np
from PIL import Image, ImageDraw

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError


# ==========================================================
# ENV
# ==========================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
BASE_URL = os.getenv("BASE_URL")
PORT = int(os.getenv("PORT", 8080))

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_FILE = "token.json"

QUEUE_FILE = "queue.json"
STATS_FILE = "stats.json"

logging.basicConfig(level=logging.INFO)

upload_queue = []
BOT_APP = None


# ==========================================================
# JSON SAFE
# ==========================================================
def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f)


# ==========================================================
# DAILY COUNTER
# ==========================================================
def update_stats(success=True):
    stats = load_json(STATS_FILE, {
        "today_uploads": 0,
        "success": 0,
        "failed": 0,
        "last_reset": time.time()
    })

    if time.time() - stats["last_reset"] > 86400:
        stats = {
            "today_uploads": 0,
            "success": 0,
            "failed": 0,
            "last_reset": time.time()
        }

    stats["today_uploads"] += 1
    if success:
        stats["success"] += 1
    else:
        stats["failed"] += 1

    save_json(STATS_FILE, stats)


# ==========================================================
# MONETIZATION SAFE FILTER
# ==========================================================
BANNED_WORDS = ["kill", "blood", "sex", "nude", "weapon", "drug"]

def monetization_safe(text):
    for word in BANNED_WORDS:
        if word in text.lower():
            return False
    return True

def monetization_risk_score(text):

    score = 0
    for word in BANNED_WORDS:
        if word in text.lower():
            score += 15

    return min(score, 100)

def generate_pinned_comment(title):

    templates = [
        f"🔥 What do you think about '{title}'?",
        "💬 Drop your opinion below!",
        "🚀 Would you try this?",
        "👇 Comment YES if you agree!"
    ]

    return random.choice(templates)

# ==========================================================
# TREND SCORE
# ==========================================================
def trend_score(title):
    trend_words = ["2026", "viral", "ai", "secret", "new", "trend"]
    score = 0
    for w in trend_words:
        if w in title.lower():
            score += 5
    score += random.randint(1, 5)
    return score


# ==========================================================
# METADATA AI
# ==========================================================
async def generate_metadata(keyword):

    if not monetization_safe(keyword):
        keyword = "Amazing Viral Short"

    prompt = f"""
Generate:
- 5 viral YouTube Shorts titles
- 1 SEO description including #shorts
- 12 hashtags

Topic: {keyword}

Return JSON.
"""

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "openai/gpt-4o-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.9
    }

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload)

    content = r.json()["choices"][0]["message"]["content"]
    content = re.sub(r"```json|```", "", content)

    try:
        data = json.loads(re.search(r"\{.*\}", content, re.DOTALL).group())
        best_title = max(data["titles"], key=trend_score)
    except:
        return {
            "title": "Amazing Viral Short 2026",
            "description": "#shorts Amazing Viral Content",
            "hashtags": ["shorts", "viral", "trend"]
        }

    return {
        "title": best_title[:90],
        "description": data["description"],
        "hashtags": data["hashtags"]
    }


# ==========================================================
# YOUTUBE AUTH
# ==========================================================
def get_youtube_service():
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


async def upload_video(path, metadata, progress_callback=None):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        _upload_sync,
        path,
        metadata,
        progress_callback
    )

youtube.commentThreads().insert(
    part="snippet",
    body={
        "snippet": {
            "videoId": video_id,
            "topLevelComment": {
                "snippet": {
                    "textOriginal": pinned_comment
                }
            }
        }
    }
).execute()

def queue_position():
    return len(upload_queue) + 1

def _upload_sync(path, metadata, progress_callback=None):

    youtube = get_youtube_service()

    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata["hashtags"],
            "categoryId": metadata.get("category", "22")
        },
        "status": {"privacyStatus": "public"}
    }

    media = MediaFileUpload(path, chunksize=1024*1024, resumable=True)

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    response = None
    start_time = time.time()

    while response is None:
        status, response = request.next_chunk()

        if status and progress_callback:
            uploaded = status.resumable_progress
            total = os.path.getsize(path)
            percent = int(uploaded / total * 100)

            speed = uploaded / (time.time() - start_time + 0.1)
            remaining = (total - uploaded) / (speed + 1)

            progress_callback(percent, round(remaining,1))

    return f"https://youtube.com/watch?v={response['id']}"


def detect_category(keyword):

    keyword = keyword.lower()

    if any(x in keyword for x in ["game","minecraft","pubg"]):
        return "20"
    if any(x in keyword for x in ["tech","ai","robot"]):
        return "28"
    if any(x in keyword for x in ["learn","how","tutorial"]):
        return "27"

    return "24"

# ==========================================================
# AUTO RETRY ENGINE
# ==========================================================
async def auto_retry_engine():
    while True:
        await asyncio.sleep(600)

        if not upload_queue:
            continue

        item = upload_queue.pop(0)

        try:
            url = await upload_video(item["file"], item["meta"])
            update_stats(True)
            os.remove(item["file"])

            if ADMIN_CHAT_ID:
                await BOT_APP.bot.send_message(
                    ADMIN_CHAT_ID,
                    f"✅ Auto Retry Success\n{url}"
                )

        except Exception as e:
            upload_queue.append(item)
            update_stats(False)

        save_json(QUEUE_FILE, upload_queue)


# ==========================================================
# HANDLER
# ==========================================================
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
async def progress_updater(percent, eta):
    await progress_msg.edit_text(
        f"🚀 Uploading...\n\n"
        f"Progress: {percent}%\n"
        f"ETA: {eta}s"
    )

    if not update.message or not update.message.video:
        return

    video = update.message.video
    caption = update.message.caption or "Viral Short 2026"

    start_time = time.time()
    progress_msg = await update.message.reply_text("🚀 Processing your Short...\n")

    try:
        # =========================
        # STEP 1 — DOWNLOAD
        # =========================
        await progress_msg.edit_text("📥 Step 1/5\nDownloading video...")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            temp_path = tmp.name

        file = await context.bot.get_file(video.file_id)
        await file.download_to_drive(temp_path)

        await progress_msg.edit_text("✅ Step 1/5 Completed\n\n🧠 Step 2/5\nAnalyzing trend...")

        # =========================
        # STEP 2 — TREND ANALYSIS
        # =========================
        trend_value = trend_score(caption)
        await asyncio.sleep(1)

        await progress_msg.edit_text(
            f"📈 Trend Score: {trend_value}/15\n\n"
            "🧠 Step 3/5\nGenerating AI Metadata..."
        )

        # =========================
        # STEP 3 — METADATA
        # =========================
        metadata = await generate_metadata(caption)
        metadata["category"] = detect_category(caption)

        await progress_msg.edit_text(
            f"🏷 Title Selected:\n{metadata['title'][:60]}...\n\n"
            "🖼 Step 4/5\nGenerating Thumbnail..."
        )

        # =========================
        # STEP 4 — THUMBNAIL
        # =========================
        thumb_path = generate_thumbnail(temp_path, metadata["title"])

        if thumb_path:
            await update.message.reply_photo(
                photo=open(thumb_path, "rb"),
                caption="🖼 Thumbnail Preview"
            )

        await progress_msg.edit_text(
            "🚀 Step 5/5\nUploading to YouTube...\n\n"
            "⏳ Please wait..."
        )

        # =========================
        # STEP 5 — UPLOAD
        # =========================
        url = await upload_video(
        temp_path,
        metadata,
        lambda p, e: asyncio.run_coroutine_threadsafe(
            progress_updater(p,e),
            asyncio.get_event_loop()
        )
    )

        total_time = round(time.time() - start_time, 2)

        await progress_msg.edit_text(
            "🎉 UPLOAD SUCCESS 🎉\n\n"
            f"🔗 {url}\n\n"
            f"⏱ Process Time: {total_time}s\n"
            f"🔥 Trend Score: {trend_value}"
        )

        update_stats(True)

        if ADMIN_CHAT_ID:
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"✅ Upload Success\n{url}\nTime: {total_time}s"
            )

        os.remove(temp_path)
        if thumb_path:
            os.remove(thumb_path)

    except Exception as e:

        print("PROCESS ERROR:", e)

        upload_queue.append({
            "file": temp_path,
            "meta": metadata if 'metadata' in locals() else {}
        })

        save_json(QUEUE_FILE, upload_queue)
        update_stats(False)

        await progress_msg.edit_text(
            "⚠️ Upload Failed\n"
            "Added to Smart Retry Queue.\n\n"
            f"Error: {str(e)[:100]}"
        )

        if ADMIN_CHAT_ID:
            await context.bot.send_message(
                ADMIN_CHAT_ID,
                f"❌ Upload Failed\nError: {str(e)}"
            )

# ==========================================================
# ADMIN COMMAND
# ==========================================================
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = load_json(STATS_FILE, {})
    queue_len = len(upload_queue)

    await update.message.reply_text(
        f"📊 Today: {stats.get('today_uploads',0)}\n"
        f"✅ Success: {stats.get('success',0)}\n"
        f"❌ Failed: {stats.get('failed',0)}\n"
        f"📦 Queue: {queue_len}"
    )


# ==========================================================
# ERROR HANDLER
# ==========================================================
async def error_handler(update, context):
    logging.error(f"Exception: {context.error}")


# ==========================================================
# STARTUP
# ==========================================================
async def on_startup(app):
    global BOT_APP, upload_queue
    BOT_APP = app
    upload_queue = load_json(QUEUE_FILE, [])

    asyncio.create_task(auto_retry_engine())
    print("✅ Background engine started")


# ==========================================================
# MAIN (WEBHOOK MODE)
# ==========================================================
def main():

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_error_handler(error_handler)

    app.post_init = on_startup

    print("🚀 WEBHOOK MODE ACTIVE")

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TELEGRAM_TOKEN,
        webhook_url=f"{BASE_URL}/{TELEGRAM_TOKEN}",
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()




