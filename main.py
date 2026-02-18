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
from PIL import Image, ImageDraw, ImageFont

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError


# ==========================================================
# ENV CONFIG
# ==========================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_FILE = "token.json"

QUEUE_FILE = "queue.json"
LIMIT_FILE = "limit.json"
STATS_FILE = "stats.json"

logging.basicConfig(level=logging.INFO)

upload_queue = []
upload_limit_reached = False
BOT_APP = None


# ==========================================================
# SAFE JSON UTIL
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
# SMART RESET ESTIMATION
# ==========================================================
def save_limit_hit():
    data = {
        "last_hit": time.time(),
        "estimated_reset": time.time() + 86400
    }
    save_json(LIMIT_FILE, data)


def get_reset_remaining():
    data = load_json(LIMIT_FILE, None)
    if not data:
        return 0
    return int(data["estimated_reset"] - time.time())


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


# ==========================================================
# ML METADATA OPTIMIZER
# ==========================================================
def score_title(title):
    score = 0
    power_words = ["INSANE", "SHOCKING", "SECRET", "UNBELIEVABLE", "BEST"]

    for w in power_words:
        if w.lower() in title.lower():
            score += 5

    length = len(title)
    if 45 <= length <= 70:
        score += 10

    score += random.randint(0, 3)
    return score


async def generate_metadata(keyword):

    prompt = f"""
Generate:
- 5 viral titles
- 1 description with #shorts
- 12 hashtags

Keyword: {keyword}

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
    data = json.loads(re.search(r"\{.*\}", content, re.DOTALL).group())

    best_title = max(data["titles"], key=score_title)

    return {
        "title": best_title[:90],
        "description": data["description"],
        "hashtags": data["hashtags"]
    }


# ==========================================================
# ML THUMBNAIL SELECTOR
# ==========================================================
def generate_thumbnail(video_path, text):

    cap = cv2.VideoCapture(video_path)
    frames = []

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        frames.append(frame)

    cap.release()

    if not frames:
        return None

    best_frame = max(frames, key=lambda f: np.mean(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)))

    img = Image.fromarray(cv2.cvtColor(best_frame, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img)

    draw.text((50, 50), text[:20], fill="yellow")

    thumb_path = video_path.replace(".mp4", "_thumb.jpg")
    img.save(thumb_path)

    return thumb_path


# ==========================================================
# UPLOAD
# ==========================================================
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
# AUTO SCALING ENGINE
# ==========================================================
async def auto_retry_engine():

    global upload_limit_reached

    while True:
        await asyncio.sleep(900)

        if not upload_queue:
            continue

        if upload_limit_reached:
            if get_reset_remaining() > 0:
                continue
            upload_limit_reached = False

        item = upload_queue.pop(0)

        try:
            url = await upload_video(item["file"], item["meta"])
            update_stats(True)

            if ADMIN_CHAT_ID:
                await BOT_APP.bot.send_message(
                    ADMIN_CHAT_ID,
                    f"✅ Auto Retry Success\n{url}"
                )

            os.remove(item["file"])

        except Exception:
            upload_queue.append(item)
            update_stats(False)

        save_json(QUEUE_FILE, upload_queue)


# ==========================================================
# VIDEO HANDLER
# ==========================================================
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):

    global upload_limit_reached

    video = update.message.video
    caption = update.message.caption or "Amazing Short"

    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
        temp_path = tmp.name
        await context.bot.get_file(video.file_id).download_to_drive(temp_path)

    metadata = await generate_metadata(caption)

    try:
        url = await upload_video(temp_path, metadata)
        await update.message.reply_text(f"✅ Uploaded\n{url}")
        update_stats(True)
        os.remove(temp_path)

    except HttpError as e:
        if "uploadLimitExceeded" in str(e):
            upload_limit_reached = True
            save_limit_hit()
            upload_queue.append({"file": temp_path, "meta": metadata})
            save_json(QUEUE_FILE, upload_queue)

            await update.message.reply_text(
                f"⚠️ Limit reached.\nReset in {get_reset_remaining()//3600} hours"
            )
        else:
            update_stats(False)
            await update.message.reply_text("❌ Upload failed")


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
# MAIN
# ==========================================================
def main():

    global BOT_APP, upload_queue

    upload_queue = load_json(QUEUE_FILE, [])

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    BOT_APP = app

    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(CommandHandler("stats", stats_cmd))

    app.create_task(auto_retry_engine())

    print("🚀 AUTO SCALING SHORTS MACHINE RUNNING")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
