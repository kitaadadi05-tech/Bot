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
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/yt-analytics.readonly"
]

TOKEN_FILE = "token.json"
QUEUE_FILE = "queue.json"
STATS_FILE = "stats.json"
ANALYTICS_FILE = "analytics.json"
PERFORMANCE_FILE = "performance.json"

logging.basicConfig(level=logging.INFO)

upload_queue = []
BOT_APP = None
MAIN_LOOP = None


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
            json=payload
        )

    content = r.json()["choices"][0]["message"]["content"]
    content = re.sub(r"```json|```", "", content)

    try:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        data = json.loads(match.group()) if match else {}

        best_title = max(
            data.get("titles", ["Amazing Viral Short 2026"]),
            key=trend_score
        )

        description = data.get("description", "#shorts Viral Content 2026")
        hashtags = data.get("hashtags", ["shorts", "viral", "trend"])

    except Exception:
        best_title = "Amazing Viral Short 2026"
        description = "#shorts Amazing Viral Content"
        hashtags = ["shorts", "viral", "trend"]

    return {
        "title": best_title[:90],
        "description": description,
        "hashtags": hashtags
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


def get_analytics_service():
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    return build("youtubeAnalytics", "v2", credentials=creds)

def fetch_channel_analytics():

    analytics = get_analytics_service()

    end_date = time.strftime("%Y-%m-%d")
    start_date = time.strftime("%Y-%m-%d", time.localtime(time.time()-7*86400))

    response = analytics.reports().query(
        ids="channel==MINE",
        startDate=start_date,
        endDate=end_date,
        metrics="views,estimatedMinutesWatched,averageViewDuration,impressions,impressionCtr",
        dimensions="day"
    ).execute()

    rows = response.get("rows", [])

    save_json(ANALYTICS_FILE, rows)
    return rows

def title_performance_score(title):

    score = 0

    if len(title) < 60:
        score += 10

    if any(x in title.lower() for x in ["how","secret","ai","new"]):
        score += 15

    score += trend_score(title)

    ctr_est = predict_ctr(title)
    score += ctr_est

    return round(score, 2)

def get_best_hour_from_analytics():

    analytics = get_analytics_service()

    end_date = time.strftime("%Y-%m-%d")
    start_date = time.strftime("%Y-%m-%d", time.localtime(time.time()-14*86400))

    response = analytics.reports().query(
        ids="channel==MINE",
        startDate=start_date,
        endDate=end_date,
        metrics="impressionCtr",
        dimensions="hour"
    ).execute()

    rows = response.get("rows", [])

    if not rows:
        return random.choice([11,13,16,19,21])

    best = max(rows, key=lambda x: x[1])

    return int(best[0])
def smart_best_hour():

    weekday = time.localtime().tm_wday  # 0=Mon

    if weekday >= 5:
        base_hours = [10,12,15,18,20]
    else:
        base_hours = [11,13,16,19,21]

    try:
        analytics_hour = get_best_hour_from_analytics()
        return analytics_hour
    except:
        return random.choice(base_hours)

def detect_shadowban():

    analytics = load_json(ANALYTICS_FILE, [])

    if not analytics:
        return False

    last_day = analytics[-1]

    views = last_day[0]
    impressions = last_day[3]

    if impressions > 0 and views < impressions * 0.005:
        return True

    return False

def predict_ctr(title):

    base_score = trend_score(title)

    analytics = load_json(ANALYTICS_FILE, [])
    avg_ctr = 5

    if analytics:
        ctr_values = [row[4] for row in analytics if len(row) > 4]
        if ctr_values:
            avg_ctr = sum(ctr_values) / len(ctr_values)

    prediction = avg_ctr + (base_score * 0.3)

    return round(min(prediction, 25), 2)

async def upload_video(path, metadata, progress_message):

    loop = asyncio.get_running_loop()

    def progress_callback(percent, eta):
        if MAIN_LOOP:
            asyncio.run_coroutine_threadsafe(
                progress_message.edit_text(
                    f"🚀 Uploading to YouTube...\n\n"
                    f"Progress: {percent}%\n"
                    f"ETA: {eta}s"
                ),
                MAIN_LOOP
            )

    return await loop.run_in_executor(
        None,
        _upload_sync,
        path,
        metadata,
        progress_callback
    )


def _upload_sync(path, metadata, progress_callback=None):

    youtube = get_youtube_service()

    body = {
        "snippet": {
          "title": metadata.get("title", "Viral Short 2026"),
            "description": metadata.get("description", "#shorts Viral Content"),
            "tags": metadata.get("hashtags", ["shorts","viral"]),
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
    total = os.path.getsize(path)

    while response is None:
        status, response = request.next_chunk()

        if status and progress_callback:
            uploaded = status.resumable_progress
            percent = int(uploaded / total * 100)

            speed = uploaded / (time.time() - start_time + 0.1)
            eta = (total - uploaded) / (speed + 1)

            progress_callback(percent, round(eta, 1))

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

        if random.randint(1,10) == 5:
            fetch_channel_analytics()

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

    video = update.message.video
    caption = update.message.caption or "Viral Short 2026"

    start_time = time.time()
    progress_msg = await update.message.reply_text("🚀 Processing your Short...\n")

    try:
        # STEP 1 — DOWNLOAD
        await progress_msg.edit_text("📥 Step 1/4\nDownloading video...")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
            temp_path = tmp.name

        file = await context.bot.get_file(video.file_id)
        await file.download_to_drive(temp_path)

        # STEP 2 — METADATA
        await progress_msg.edit_text("🧠 Step 2/4\nGenerating AI Metadata...")

        metadata = await generate_metadata(caption)
        metadata["category"] = detect_category(caption)
        ctr_prediction = predict_ctr(metadata["title"])
        performance_score = title_performance_score(metadata["title"])
        shadow_flag = detect_shadowban()
        trend_value = trend_score(metadata["title"])
        risk_score = monetization_risk_score(metadata["title"])

        # STEP 3 — READY TO UPLOAD
       await progress_msg.edit_text(
            f"🏷 Title: {metadata['title'][:60]}...\n"
            f"📈 Trend Score: {trend_value}\n"
            f"🎯 Predicted CTR: {ctr_prediction}%\n"
            f"🏆 Title Score: {performance_score}\n"
            f"💰 Monetization Risk: {risk_score}%\n"
            f"🚨 Shadow Risk: {'YES' if shadow_flag else 'NO'}\n\n"
            "🚀 Uploading..."
        )


        # STEP 4 — UPLOAD WITH LIVE PROGRESS
        url = await upload_video(temp_path, metadata, progress_msg)

        total_time = round(time.time() - start_time, 2)

        await progress_msg.edit_text(
            "🎉 UPLOAD SUCCESS 🎉\n\n"
            f"🔗 {url}\n\n"
            f"⏱ Time: {total_time}s\n"
            f"🔥 Trend Score: {trend_value}\n"
            f"💰 Risk: {risk_score}%"
        )

        update_stats(True)
        os.remove(temp_path)

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
            "Added to Retry Queue.\n\n"
            f"Error: {str(e)[:100]}"
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
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_event_loop()

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TELEGRAM_TOKEN,
        webhook_url=f"{BASE_URL}/{TELEGRAM_TOKEN}",
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()







