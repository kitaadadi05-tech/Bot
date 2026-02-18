import os
import json
import asyncio
import logging
import tempfile
import httpx
import time
import re

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

# ==========================================================
# CONFIG
# ==========================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # isi dengan chat id kamu

QUEUE_FILE = "queue.json"
LIMIT_FILE = "limit.json"

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"

logging.basicConfig(level=logging.INFO)

upload_queue = []
upload_limit_reached = False
BOT_APP = None


# ==========================================================
# QUEUE SYSTEM
# ==========================================================
def load_queue():
    if os.path.exists(QUEUE_FILE):
        with open(QUEUE_FILE, "r") as f:
            return json.load(f)
    return []


def save_queue(queue):
    with open(QUEUE_FILE, "w") as f:
        json.dump(queue, f)


def save_limit_time():
    with open(LIMIT_FILE, "w") as f:
        json.dump({"time": time.time()}, f)


def can_retry():
    if not os.path.exists(LIMIT_FILE):
        return True

    with open(LIMIT_FILE, "r") as f:
        data = json.load(f)

    return time.time() - data["time"] > 86400  # 24 jam


# ==========================================================
# YOUTUBE AUTH
# ==========================================================
def get_youtube_service():
    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    if not creds:
        flow = InstalledAppFlow.from_client_secrets_file(
            CREDENTIALS_FILE, SCOPES)
        auth_url, _ = flow.authorization_url(prompt="consent")
        print("\nOpen this URL:\n", auth_url)
        code = input("Paste code: ")
        flow.fetch_token(code=code)
        creds = flow.credentials

        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return build("youtube", "v3", credentials=creds)


# ==========================================================
# OPENROUTER METADATA
# ==========================================================
async def generate_viral_metadata(keyword: str):

    prompt = f"""
Generate:
- 5 viral titles (max 90 chars)
- 1 description with #shorts and CTA
- exactly 12 hashtags

Keyword: "{keyword}"

Return STRICT JSON:
{{
 "titles": [],
 "description": "",
 "hashtags": []
}}
"""

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "openai/gpt-4o-mini",
        "messages": [{
            "role": "user",
            "content": prompt
        }],
        "temperature": 0.9
    }

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=payload)

    result = response.json()
    content = result["choices"][0]["message"]["content"]

    content = re.sub(r"```json|```", "", content)
    match = re.search(r"\{.*\}", content, re.DOTALL)

    if not match:
        raise Exception("Invalid AI JSON")

    data = json.loads(match.group())
    best_title = max(data["titles"], key=len)

    return {
        "title": best_title[:90],
        "description": data["description"],
        "hashtags": data["hashtags"]
    }


# ==========================================================
# UPLOAD
# ==========================================================
async def upload_to_youtube(path, metadata):
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
        "status": {
            "privacyStatus": "public",
            "madeForKids": False
        }
    }

    media = MediaFileUpload(path, resumable=True)

    request = youtube.videos().insert(part="snippet,status",
                                      body=body,
                                      media_body=media)

    response = request.execute()
    video_id = response["id"]

    return f"https://youtube.com/watch?v={video_id}"


# ==========================================================
# RETRY WORKER
# ==========================================================
async def retry_worker():
    global upload_limit_reached

    while True:
        await asyncio.sleep(1800)  # cek tiap 30 menit

        if upload_limit_reached and can_retry() and upload_queue:

            logging.info("Retrying queue...")

            new_queue = []

            for item in upload_queue:
                try:
                    url = await upload_to_youtube(item["file_path"],
                                                  item["metadata"])

                    if ADMIN_CHAT_ID and BOT_APP:
                        await BOT_APP.bot.send_message(
                            chat_id=ADMIN_CHAT_ID,
                            text=f"✅ Retry sukses!\n{url}")

                    os.remove(item["file_path"])

                except HttpError as e:
                    if "uploadLimitExceeded" in str(e):
                        save_limit_time()
                        new_queue.append(item)
                        break
                    else:
                        new_queue.append(item)

                except Exception:
                    new_queue.append(item)

            upload_queue.clear()
            upload_queue.extend(new_queue)
            save_queue(upload_queue)

            if not upload_queue:
                upload_limit_reached = False


# ==========================================================
# VIDEO HANDLER
# ==========================================================
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):

    global upload_limit_reached

    message = update.message
    video = message.video

    if not video:
        return

    if video.duration > 60:
        await message.reply_text("❌ Max 60 seconds.")
        return

    caption = message.caption or "Amazing Short"

    await message.reply_text("⬇️ Downloading...")
    file = await context.bot.get_file(video.file_id)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
        temp_path = tmp.name
        await file.download_to_drive(temp_path)

    await message.reply_text("🤖 Generating metadata...")
    metadata = await generate_viral_metadata(caption)

    await message.reply_text("🚀 Uploading...")

    try:
        url = await upload_to_youtube(temp_path, metadata)
        await message.reply_text(f"✅ Uploaded!\n{url}")
        os.remove(temp_path)

    except HttpError as e:
        if "uploadLimitExceeded" in str(e):
            upload_limit_reached = True
            save_limit_time()

            upload_queue.append({"file_path": temp_path, "metadata": metadata})

            save_queue(upload_queue)

            await message.reply_text(
                "⚠️ Daily limit reached.\nVideo masuk queue auto retry 24 jam."
            )
        else:
            await message.reply_text(f"❌ YouTube Error: {e}")
            os.remove(temp_path)


# ==========================================================
# STARTUP
# ==========================================================
async def on_startup(app):
    global upload_queue, BOT_APP

    BOT_APP = app
    upload_queue = load_queue()

    await app.bot.delete_webhook(drop_pending_updates=True)

    app.create_task(retry_worker())


# ==========================================================
# MAIN
# ==========================================================
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(MessageHandler(filters.VIDEO, handle_video))

    app.post_init = on_startup

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
