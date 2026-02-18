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


async def upload_video(path, metadata):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _upload_sync, path, metadata)


def _upload_sync(path, metadata):
    youtube = get_youtube_service()

    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata["hashtags"],
            "categoryId": "22"
        },
        "status": {"privacyStatus": "public"}
    }

    media = MediaFileUpload(path, resumable=True)

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    response = request.execute()
    return f"https://youtube.com/watch?v={response['id']}"


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

    if not update.message or not update.message.video:
        return

    print("📩 VIDEO RECEIVED")

    video = update.message.video
    caption = update.message.caption or "Viral Short 2026"

    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
        temp_path = tmp.name

    # ✅ FIXED ASYNC BUG HERE
    file = await context.bot.get_file(video.file_id)
    await file.download_to_drive(temp_path)

    metadata = await generate_metadata(caption)

    try:
        url = await upload_video(temp_path, metadata)
        await update.message.reply_text(f"✅ Uploaded\n{url}")
        update_stats(True)
        os.remove(temp_path)

    except Exception as e:
        upload_queue.append({"file": temp_path, "meta": metadata})
        save_json(QUEUE_FILE, upload_queue)
        update_stats(False)

        await update.message.reply_text("⚠️ Added to Auto Retry Queue")


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
