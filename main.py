import os
import json
import asyncio
import logging
import tempfile
import httpx
import time
import re
from datetime import datetime, timedelta
import pytz
import random
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters, CallbackQueryHandler, CommandHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
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
PUBLISH_FILE = "publish_list.json"
PRIME_STATS_FILE = "prime_stats.json"

SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/yt-analytics.readonly"
]

TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credential10.json"

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


#============================================================
def load_publish_list():
    if os.path.exists(PUBLISH_FILE):
        with open(PUBLISH_FILE, "r") as f:
            return json.load(f)
    return []


def save_publish_list(data):
    with open(PUBLISH_FILE, "w") as f:
        json.dump(data, f, indent=2)


#============================================================
# PRIME TIME CHECKER
def get_next_prime_time():
    tz = pytz.timezone("Asia/Jakarta")
    now = datetime.now(tz)

    prime_hours = get_best_prime_hour()
    max_per_hour = 3
    max_per_day = 9

    publish_list = load_publish_list()

    # cek 7 hari ke depan
    for day_offset in range(7):
        check_day = now + timedelta(days=day_offset)
        date_only = check_day.date()

        # ambil semua publish di hari tersebut
        day_videos = []
        for item in publish_list:
            publish_time = datetime.fromisoformat(item["publishAt"]).astimezone(tz)
            if publish_time.date() == date_only:
                day_videos.append(publish_time)

        # kalau sudah 9 → skip ke hari berikutnya
        if len(day_videos) >= max_per_day:
            continue

        # hitung slot per jam
        hour_count = {h: 0 for h in prime_hours}
        for publish_time in day_videos:
            if publish_time.hour in hour_count:
                hour_count[publish_time.hour] += 1

        # cari jam yang belum penuh
        for hour in prime_hours:
            if hour_count[hour] < max_per_hour:

                # random minute + second biar natural
                random_minute = random.randint(5, 55)
                random_second = random.randint(0, 59)

                candidate = check_day.replace(
                    hour=hour,
                    minute=random_minute,
                    second=random_second,
                    microsecond=0
                )

                # jangan ambil waktu yang sudah lewat
                if candidate <= now:
                    continue

                return candidate.astimezone(pytz.utc).isoformat()

    # fallback (harusnya jarang kejadian)
    fallback = now + timedelta(days=1)
    return fallback.astimezone(pytz.utc).isoformat()
#============================================================
# Start UI
#============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = get_dashboard_stats()

    next_text = "Tidak ada"
    if stats["next_publish"]:
        next_text = stats["next_publish"].strftime("%d %b %Y - %H:%M WIB")

    text = ("🚀 *YouTube Shorts Automation PRO*\n\n"
            f"📊 Scheduled: {stats['total']}\n"
            f"🕒 Next Publish: {next_text}\n"
            f"🔥 Slot Today: {stats['today_slots']}/3\n\n"
            "Kelola video di bawah 👇")

    keyboard = [[
        InlineKeyboardButton("📅 Manage Videos", callback_data="list")
    ], [InlineKeyboardButton("📤 Upload Guide", callback_data="help")],
                [InlineKeyboardButton("🔄 Refresh", callback_data="dashboard")]]

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard))


#==========================================================
# Callback Handler
#==========================================================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "list":
        await show_list(query)

    elif data.startswith("publish_"):
        index = int(data.split("_")[1])
        await publish_from_button(query, index)

    elif data.startswith("delete_"):
        index = int(data.split("_")[1])
        await delete_from_button(query, index)

    elif data == "help":
        await query.edit_message_text(
            "📌 Cara Pakai:\n\n"
            "1️⃣ Kirim video Shorts\n"
            "2️⃣ Bot auto schedule\n"
            "3️⃣ Kelola via tombol\n\n"
            "Jam publish: 12, 17, 20 WIB"
        )

    elif data == "dashboard":

        await query.answer("🔄 Refreshing dashboard...")

        try:
            stats = get_dashboard_stats()

            next_text = "Tidak ada"
            if stats["next_publish"]:
                next_text = stats["next_publish"].strftime("%d %b %Y - %H:%M WIB")

            text = (
                "🚀 *YouTube Shorts Automation PRO*\n\n"
                f"📊 Scheduled: {stats['total']}\n"
                f"🕒 Next Publish: {next_text}\n"
                f"🔥 Slot Today: {stats['today_slots']}/3\n\n"
                "Kelola video di bawah 👇"
            )

            keyboard = [
                [InlineKeyboardButton("📅 Manage Videos", callback_data="list")],
                [InlineKeyboardButton("📤 Upload Guide", callback_data="help")],
                [InlineKeyboardButton("🔄 Refresh", callback_data="dashboard")]
            ]

            try:
                await query.message.delete()
            except:
                pass

            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        except Exception:
            import traceback
            tb = traceback.format_exc()

            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="⚠️ Dashboard Error:\n\n" + tb[:1000]
            )
#==========================================================
#Modern Sheduled list
#==========================================================
async def show_list(query):
    cleanup_published()
    data = load_publish_list()

    if not data:
        await query.edit_message_text("📭 Tidak ada video terjadwal.")
        return

    text = "📅 *Scheduled Videos:*\n\n"
    keyboard = []

    for i, item in enumerate(data):
        title = item["titles"][item.get("current_title_index", 0)]
        publish_time = datetime.fromisoformat(item["publishAt"])
        jakarta_time = publish_time.astimezone(pytz.timezone("Asia/Jakarta"))

        text += f"{i+1}. {title}\n"
        text += f"🕒 {jakarta_time.strftime('%d %b %Y - %H:%M WIB')}\n\n"

        keyboard.append([
            InlineKeyboardButton(f"🚀 Publish {i+1}",
                                 callback_data=f"publish_{i}"),
            InlineKeyboardButton(f"🗑 Delete {i+1}",
                                 callback_data=f"delete_{i}")
        ])

    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="list")])

    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        if "Message is not modified" in str(e):
            pass  # Abaikan
        else:
            raise e


async def analytics_report_worker(context: ContextTypes.DEFAULT_TYPE):

    tz = pytz.timezone("Asia/Jakarta")
    youtube = get_youtube_service()
    creds = youtube._http.credentials

    youtube_analytics = build(
        "youtubeAnalytics",
        "v2",
        credentials=creds
    )
    views = row[1]
    avg_percent = float(row[2])
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    report = youtube_analytics.reports().query(
        ids="channel==MINE",
        startDate=yesterday,
        endDate=yesterday,
        metrics="views,averageViewPercentage",
        dimensions="video"
    ).execute()

    publish_list = load_publish_list()

    for row in report.get("rows", []):
        video_id = row[0]
        avg_percent = float(row[2])

        for item in publish_list:
            if item["video_id"] == video_id and item.get("ab_test"):

                item.setdefault("metrics_history", []).append({
                    "views": views,
                    "retention": avg_percent
                })

                publish_time = datetime.fromisoformat(item["publishAt"])
                update_prime_stats(video_id, avg_percent, publish_time)

    save_publish_list(publish_list)

    if ADMIN_CHAT_ID and BOT_APP:
        await BOT_APP.bot.send_message(ADMIN_CHAT_ID, text)
#==========================================================
# Publish from button
#==========================================================
async def publish_from_button(query, index):
    data = load_publish_list()

    if index >= len(data):
        await query.answer("Video tidak ditemukan", show_alert=True)
        return

    video_id = data[index]["video_id"]

    await query.edit_message_text("🚀 Publishing now...")

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, publish_now, video_id)

        # Hapus dari schedule list
        data.pop(index)
        save_publish_list(data)

        await query.edit_message_text(
            "✅ Video berhasil dipublish sekarang!"
        )

    except Exception as e:
        await query.edit_message_text(
            f"❌ Publish gagal:\n{str(e)}"
        )

def publish_now(video_id):
    youtube = get_youtube_service()

    response = youtube.videos().update(
        part="status",
        body={
            "id": video_id,
            "status": {
                "privacyStatus": "public",
                "selfDeclaredMadeForKids": False
            }
        }
    ).execute()

    print("Publish response:", response)
#==========================================================
# Delete from button
#==========================================================
async def delete_from_button(query, index):
    data = load_publish_list()

    if index >= len(data):
        await query.answer("Index tidak valid")
        return

    video_id = data[index]["video_id"]

    await query.edit_message_text("🗑 Deleting from YouTube...")

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, delete_video_youtube, video_id)

        data.pop(index)
        save_publish_list(data)

        await query.edit_message_text("🗑 Video berhasil dihapus!")

    except Exception as e:
        await query.edit_message_text(
            f"❌ Gagal hapus:\n{str(e)}"
        )

def delete_video_youtube(video_id):
    youtube = get_youtube_service()
    response = youtube.videos().delete(id=video_id).execute()
    print("Delete response:", response)
# ==========================================================
# CLEANUP PUBLISHED VIDEOS
# ==========================================================
def cleanup_published():
    data = load_publish_list()
    now_utc = datetime.now(pytz.utc)

    new_data = []
    for item in data:
        publish_time = datetime.fromisoformat(item["publishAt"])
        if publish_time > now_utc:
            new_data.append(item)

    save_publish_list(new_data)

# ==========================================================
# PRIME STATS SYSTEM (AI LEARNING PRIME HOUR)
# ==========================================================
def load_prime_stats():
    if os.path.exists(PRIME_STATS_FILE):
        with open(PRIME_STATS_FILE, "r") as f:
            return json.load(f)
    return {"12": [], "17": [], "20": []}


def save_prime_stats(data):
    with open(PRIME_STATS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_best_prime_hour():
    stats = load_prime_stats()

    def avg(hour):
        values = stats.get(str(hour), [])
        return sum(values) / len(values) if values else 0

    prime_hours = [12, 17, 20]
    prime_hours.sort(key=lambda h: avg(h), reverse=True)

    return prime_hours

def update_prime_stats(video_id, ctr, publish_time):
    tz = pytz.timezone("Asia/Jakarta")
    hour = publish_time.astimezone(tz).hour

    if hour not in [12, 17, 20]:
        return

    stats = load_prime_stats()
    stats.setdefault(str(hour), []).append(float(ctr))
    save_prime_stats(stats)
# ==========================================================
# YOUTUBE AUTH
# ==========================================================
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
import json, os


def get_youtube_service():
    creds = None

    # ==========================================
    # 1️⃣ LOAD TOKEN SAFE MODE
    # ==========================================
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                data = json.load(f)

            required_fields = [
                "token", "refresh_token", "client_id", "client_secret",
                "token_uri"
            ]

            if all(field in data for field in required_fields):
                creds = Credentials.from_authorized_user_info(data, SCOPES)
            else:
                print("⚠️ token.json format salah. Menghapus file...")
                os.remove(TOKEN_FILE)
                creds = None

        except Exception as e:
            print("⚠️ token.json corrupt:", e)
            os.remove(TOKEN_FILE)
            creds = None

    # ==========================================
    # 2️⃣ REFRESH JIKA EXPIRED
    # ==========================================
    if creds and creds.expired and creds.refresh_token:
        try:
            print("🔄 Refreshing token...")
            creds.refresh(Request())

            # Simpan ulang token yang sudah direfresh
            with open(TOKEN_FILE, "w") as f:
                f.write(creds.to_json())

            print("✅ Token refreshed!")

        except Exception as e:
            print("⚠️ Refresh gagal:", e)
            os.remove(TOKEN_FILE)
            creds = None

    # ==========================================
    # 3️⃣ LOGIN ULANG JIKA TIDAK ADA CREDS
    # ==========================================
    if not creds or not creds.valid:

        print("🔐 Login YouTube diperlukan...")

        flow = InstalledAppFlow.from_client_secrets_file(
            CREDENTIALS_FILE, SCOPES, redirect_uri="http://localhost:8080/")

        auth_url, _ = flow.authorization_url(prompt="consent")

        print("\n=====================================")
        print("Buka URL ini di browser:")
        print(auth_url)
        print("=====================================\n")

        code = input("Paste kode setelah 'code=' di sini: ").strip()

        flow.fetch_token(code=code)
        creds = flow.credentials

        # Simpan token dengan format resmi Google
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

        print("✅ Login berhasil & token disimpan!")

    # ==========================================
    # 4️⃣ BUILD SERVICE
    # ==========================================
    return build("youtube", "v3", credentials=creds)


# ==========================================================
# OPENROUTER METADATA
# ==========================================================
async def generate_viral_metadata(keyword: str):
    prompt = f"""
    You are a YouTube SEO expert for educational 'Fakta Unik Dunia' Shorts.

    Rules:
    - Monetization safe
    - No copyrighted content references
    - No movie names
    - No music titles
    - No brand trademarks
    - Educational factual tone
    - Avoid sensational claims
    - Avoid "shocking", "you won't believe"
    - No reused content claims
    - 8-12 relevant SEO hashtags

    Based on this caption:
    "{keyword}"

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
    "titles": data["titles"][:3],
    "description": data["description"],
    "hashtags": data["hashtags"]
    }

#===================================
async def list_videos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cleanup_published()
    data = load_publish_list()
    if not data:
        await update.message.reply_text("Tidak ada video scheduled.")
        return

    tz_jakarta = pytz.timezone("Asia/Jakarta")

    text = "📅 Scheduled Videos:\n\n"

    for i, item in enumerate(data, 1):
        # convert UTC → WIB
        utc_time = datetime.fromisoformat(item["publishAt"])
        jakarta_time = utc_time.astimezone(tz_jakarta)

        formatted_time = jakarta_time.strftime("%d %b %Y - %H:%M WIB")

        text += f"{i}. {item['title']}\nPublish: {formatted_time}\n\n"

    await update.message.reply_text(text)


# ==========================================================
# UPLOAD
# ==========================================================
async def upload_to_youtube(path, metadata):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _upload_sync, path, metadata)


def _upload_sync(path, metadata):

    tz = pytz.timezone("Asia/Jakarta")
    publish_time_utc = get_next_prime_time()

    youtube = get_youtube_service()
    publish_list = load_publish_list()

    today = datetime.now(tz).date()

    # Hitung slot hari ini
    today_count = 0
    for item in publish_list:
        item_time = datetime.fromisoformat(item["publishAt"]).astimezone(tz)
        if item_time.date() == today:
            today_count += 1

    if today_count >= 3:
        raise Exception("Daily prime slot penuh (3/hari)")

    # ===== VALIDASI METADATA =====
    if not metadata.get("titles"):
        raise Exception("AI tidak menghasilkan title")

    if not metadata.get("description"):
        raise Exception("AI tidak menghasilkan description")

    title = metadata.get("titles", ["Untitled Video"])[0]
    description = metadata.get("description", "")
    hashtags = metadata.get("hashtags", [])

    # ===== BODY YOUTUBE =====
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": hashtags,
            "categoryId": "22"
        },
        "status": {
            "privacyStatus": "private",
            "publishAt": publish_time_utc,
            "selfDeclaredMadeForKids": False
        }
    }

    media = MediaFileUpload(path, resumable=True)

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    response = request.execute()
    video_id = response["id"]

    publish_list.append({
        "video_id": video_id,
        "titles": metadata["titles"],
        "current_title_index": 0,
        "publishAt": publish_time_utc,
        "ab_test": True,
        "ctr_history": []
    })

    save_publish_list(publish_list)

    return f"https://youtube.com/watch?v={video_id}"

async def ab_test_worker(context: ContextTypes.DEFAULT_TYPE):

    youtube = get_youtube_service()
    data = load_publish_list()

    for item in data:
        if not item.get("ab_test"):
            continue

        titles = item.get("titles", [])
        metrics = item.get("metrics_history", [])
        index = item.get("current_title_index", 0)

        # =========================
        # DAY 2 & 3 → ROTATE TITLE
        # =========================
        if index < len(titles) - 1:

            new_index = index + 1
            new_title = titles[new_index]

            youtube.videos().update(
                part="snippet",
                body={
                    "id": item["video_id"],
                    "snippet": {
                        "title": new_title,
                        "categoryId": "22"
                    }
                }
            ).execute()

            item["current_title_index"] = new_index

        # =========================
        # DAY 4 → PICK WINNER (Weighted Score)
        # =========================
        else:

            if metrics and len(metrics) == len(titles):

                # Cari max views untuk normalisasi
                max_views = max(m["views"] for m in metrics) or 1

                scores = []

                for m in metrics:
                    views_score = m["views"] / max_views
                    retention_score = m["retention"] / 100

                    final_score = (retention_score * 0.6) + (views_score * 0.4)
                    scores.append(final_score)

                best_index = scores.index(max(scores))
                best_title = titles[best_index]

                youtube.videos().update(
                    part="snippet",
                    body={
                        "id": item["video_id"],
                        "snippet": {
                            "title": best_title,
                            "categoryId": "22"
                        }
                    }
                ).execute()

                item["current_title_index"] = best_index

            # Stop A/B test setelah winner dipilih
            item["ab_test"] = False

    save_publish_list(data)
#==========================================================
# PUBLISH VIDEO
async def publish_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Gunakan: /publish 1")
        return

    index = int(context.args[0]) - 1
    data = load_publish_list()

    if index >= len(data):
        await update.message.reply_text("Index tidak valid.")
        return

    youtube = get_youtube_service()
    video_id = data[index]["video_id"]

    youtube.videos().update(part="status",
                            body={
                                "id": video_id,
                                "status": {
                                    "privacyStatus": "public"
                                }
                            }).execute()

    data.pop(index)
    save_publish_list(data)

    await update.message.reply_text("✅ Video dipublish sekarang!")


#==========================================================
async def delete_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Gunakan: /delete 1")
        return

    index = int(context.args[0]) - 1
    data = load_publish_list()

    if index >= len(data):
        await update.message.reply_text("Index tidak valid.")
        return

    youtube = get_youtube_service()
    video_id = data[index]["video_id"]

    youtube.videos().delete(id=video_id).execute()

    data.pop(index)
    save_publish_list(data)

    await update.message.reply_text("🗑 Video berhasil dihapus.")


# ==========================================================
# RETRY WORKER
# ==========================================================
async def retry_worker():
    global upload_limit_reached
    while True:
        await asyncio.sleep(1800)
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

    status_msg = await message.reply_text("⬇️ Downloading...")

    file = await context.bot.get_file(video.file_id)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
        temp_path = tmp.name
        await file.download_to_drive(temp_path)

    await status_msg.edit_text("🤖 Generating metadata...")
    metadata = await generate_viral_metadata(caption)

    await status_msg.edit_text("🚀 Uploading to YouTube...")

    try:
        url = await upload_to_youtube(temp_path, metadata)

        await status_msg.edit_text(
            f"✅ Uploaded & Scheduled!\n{url}"
        )

        os.remove(temp_path)

    except HttpError as e:
        if "uploadLimitExceeded" in str(e):
            upload_limit_reached = True
            save_limit_time()

            upload_queue.append({
                "file_path": temp_path,
                "metadata": metadata
            })
            save_queue(upload_queue)

            await status_msg.edit_text(
                "⚠️ YouTube daily upload limit.\n"
                "Masuk queue auto retry 24 jam."
            )
        else:
            await status_msg.edit_text(f"❌ YouTube Error:\n{e}")
            os.remove(temp_path)

    except Exception as e:
        upload_queue.append({
            "file_path": temp_path,
            "metadata": metadata
        })
        save_queue(upload_queue)

        await status_msg.edit_text(
            f"⚠️ Slot hari ini penuh / sistem delay.\n"
            f"Video masuk queue.\n\nReason: {str(e)}"
        )
# ==========================================================
# STARTUP
# ==========================================================
async def on_startup(app):
    global upload_queue, BOT_APP
    BOT_APP = app
    cleanup_published()
    upload_queue = load_queue()
    await app.bot.delete_webhook(drop_pending_updates=True)
    count = sync_scheduled_from_youtube()
    print(f"Synced {count} scheduled videos from YouTube")
    app.create_task(ab_test_worker())
    app.create_task(analytics_report_worker())
    app.create_task(retry_worker())


# ==========================================================
# CLEANUP PUBLISHED VIDEOS
# ==========================================================
def get_dashboard_stats():
    cleanup_published()
    data = load_publish_list()

    total = len(data)

    tz = pytz.timezone("Asia/Jakarta")
    now = datetime.now(tz)

    today_slots = 0
    next_publish = None

    for item in data:
        publish_time = datetime.fromisoformat(item["publishAt"]).astimezone(tz)

        if publish_time.date() == now.date():
            today_slots += 1

        if not next_publish or publish_time < next_publish:
            next_publish = publish_time

    return {
        "total": total,
        "today_slots": today_slots,
        "next_publish": next_publish
    }


# ==========================================================
# ERROR HANDLER
# ==========================================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    import traceback

    error = context.error

    if error is None:
        print("Unknown error without exception object.")
        return

    tb = "".join(
        traceback.format_exception(
            type(error),
            error,
            error.__traceback__
        )
    )

    print("=== BOT ERROR ===")
    print(tb)

    if ADMIN_CHAT_ID and BOT_APP:
        await BOT_APP.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text="⚠️ BOT ERROR:\n\n" + tb[:3500]
        )

def sync_scheduled_from_youtube():
    youtube = get_youtube_service()
    tz = pytz.timezone("Asia/Jakarta")

    request = youtube.videos().list(
        part="snippet,status",
        mine=True,
        maxResults=50
    )

    response = request.execute()

    new_publish_list = []

    now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)

    for item in response.get("items", []):
        status = item.get("status", {})
        snippet = item.get("snippet", {})

        publish_at = status.get("publishAt")
        privacy = status.get("privacyStatus")

        if publish_at and privacy == "private":
            publish_time = datetime.fromisoformat(
                publish_at.replace("Z", "+00:00")
            )

            if publish_time > now_utc:

                new_publish_list.append({
                    "video_id": item["id"],
                    "titles": [snippet.get("title", "")],
                    "current_title_index": 0,
                    "publishAt": publish_at,
                    "ab_test": False,
                    "metrics_history": []
                })

    save_publish_list(new_publish_list)
    return len(new_publish_list)
# ==========================================================
# MAIN
# ==========================================================
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_videos))
    app.add_handler(CommandHandler("publish", publish_video))
    app.add_handler(CommandHandler("delete", delete_video))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))

    app.add_error_handler(error_handler)

    async def startup(app):
        global upload_queue, BOT_APP
        BOT_APP = app
        cleanup_published()
        upload_queue = load_queue()

        await app.bot.delete_webhook(drop_pending_updates=True)

        app.job_queue.run_daily(
            analytics_report_worker,
            time=datetime.time(hour=23, minute=59)
        )
        
        app.job_queue.run_daily(
            ab_test_worker,
            time=datetime.time(hour=0, minute=5)
        )
        
        app.job_queue.run_repeating(
            retry_worker,
            interval=1800,
            first=30
        )

    app.post_init = startup

    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
        close_loop=False
    )

if __name__ == "__main__":
    main()





















