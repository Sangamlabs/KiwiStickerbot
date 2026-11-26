# -*- coding: utf-8 -*-
import os
import sys
import time
import re
import html
import json
import uuid
import asyncio
import aiohttp
import shutil
from io import BytesIO
from PIL import Image, ImageDraw, ImageFont
from pyrogram import Client, filters, idle, StopPropagation
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
try:
    from pyrogram.enums import ParseMode
    HTML_PARSE_MODE = ParseMode.HTML
except ImportError:
    HTML_PARSE_MODE = "html"
from pymongo import MongoClient
from datetime import datetime
from dotenv import load_dotenv

from handlers import plain_handler, vid_handler

# ==============================================
# LOAD ENVIRONMENT VARIABLES
# ==============================================
load_dotenv()

# ==============================================
# BOT CONFIGURATION FROM .env
# ==============================================
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

MONGODB_URI = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "sticker_bot")
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "users")

_owner_raw = (os.getenv("OWNER_ID") or os.getenv("ADMIN_USER_ID") or "").strip()
OWNER_IDS = []
for _piece in _owner_raw.split(","):
    _piece = _piece.strip()
    if _piece.isdigit():
        OWNER_IDS.append(int(_piece))

_admin_raw = (os.getenv("ADMIN_USER_ID") or os.getenv("OWNER_ID") or "").strip()
_BASE_ADMIN_IDS = []
for _piece in _admin_raw.split(","):
    _piece = _piece.strip()
    if _piece.isdigit() and int(_piece) not in _BASE_ADMIN_IDS:
        _BASE_ADMIN_IDS.append(int(_piece))
for _oid in OWNER_IDS:
    if _oid not in _BASE_ADMIN_IDS:
        _BASE_ADMIN_IDS.append(_oid)

ADMIN_IDS = list(_BASE_ADMIN_IDS)
ADMIN_USER_ID = ADMIN_IDS[0] if ADMIN_IDS else None
BOT_START_TIME = time.time()

DEFAULT_EMOJI = os.getenv("DEFAULT_EMOJI", "✨")
TEMP_DIR = os.getenv("TEMP_DIR", "temp_stickers")

# ==============================================
# STARTUP SANITY CHECKS
# ==============================================
missing = [k for k, v in {
    "API_ID": API_ID, "API_HASH": API_HASH, "BOT_TOKEN": BOT_TOKEN,
    "MONGODB_URI": MONGODB_URI,
}.items() if not v]
if missing:
    print(f"❌ Missing required .env values: {', '.join(missing)}")
    print("   Fill these in your .env file before starting the bot.")
    sys.exit(1)

if not ADMIN_IDS:
    print("❌ ADMIN_USER_ID is missing/invalid in your .env file.")
    print("   Add a line like: ADMIN_USER_ID=805508459")
    print("   or for multiple admins: ADMIN_USER_ID=805508459,123456789")
    print("   (numeric Telegram user IDs only, comma-separated, no quotes, no @).")
    sys.exit(1)

API_ID = int(API_ID)

# ==============================================
# CREATE TEMP DIRECTORY
# ==============================================
os.makedirs(TEMP_DIR, exist_ok=True)

# ==============================================
# CONNECT TO MONGODB
# ==============================================
try:
    mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=8000)
    mongo_client.admin.command("ping")
except Exception as e:
    print(f"❌ Could not connect to MongoDB: {e}")
    sys.exit(1)

db = mongo_client[DB_NAME]
users_collection = db[COLLECTION_NAME]

# ==============================================
# MULTI-PACK COLLECTION
# ==============================================
PACKS_COLLECTION_NAME = os.getenv("PACKS_COLLECTION_NAME", "sticker_packs")
packs_collection = db[PACKS_COLLECTION_NAME]
sudo_collection = db["sudo_admins"]

def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS

def is_admin(user_id: int) -> bool:
    return user_id in OWNER_IDS or user_id in ADMIN_IDS

def reload_sudo_admins(fsub_instance=None):
    merged = list(_BASE_ADMIN_IDS)
    try:
        for doc in sudo_collection.find({}, {"user_id": 1}):
            uid = int(doc["user_id"])
            if uid not in merged:
                merged.append(uid)
    except Exception:
        pass
    ADMIN_IDS[:] = merged
    # Sync with __main__ or stick module if double-imported
    for mod_name in ("__main__", "stick"):
        mod = sys.modules.get(mod_name)
        if mod and hasattr(mod, "ADMIN_IDS") and mod.ADMIN_IDS is not ADMIN_IDS:
            mod.ADMIN_IDS[:] = merged
    if fsub_instance is not None:
        fsub_instance.admin_ids[:] = merged
    return ADMIN_IDS

reload_sudo_admins()
admin_filter = filters.create(lambda _, __, m: bool(m.from_user and is_admin(m.from_user.id)))

# ==============================================
# CREATE THE BOT CLIENT
# ==============================================
app = Client(
    "sticker_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

BOT_USERNAME = None

def _resolve_bot_username(client=None):
    global BOT_USERNAME
    if BOT_USERNAME:
        return BOT_USERNAME
    main_mod = sys.modules.get("__main__")
    if main_mod and getattr(main_mod, "BOT_USERNAME", None):
        BOT_USERNAME = main_mod.BOT_USERNAME.lower()
        return BOT_USERNAME
    c = client or app
    if c and getattr(c, "me", None) and getattr(c.me, "username", None):
        BOT_USERNAME = c.me.username.lower()
        return BOT_USERNAME
    return None

# ==============================================
# FFMPEG AUTO-DETECTION
# ==============================================
_BIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin")

def _resolve_binary(name):
    local = os.path.join(_BIN_DIR, name)
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return os.path.normpath(local)
    system = shutil.which(name)
    if system:
        return os.path.normpath(system)
    return None

FFMPEG_PATH = _resolve_binary("ffmpeg")
FFPROBE_PATH = _resolve_binary("ffprobe")

if not FFMPEG_PATH or not FFPROBE_PATH:
    missing = []
    if not FFMPEG_PATH:
        missing.append("ffmpeg")
    if not FFPROBE_PATH:
        missing.append("ffprobe")
    print("=" * 60)
    print("  FFMPEG / FFPROBE NOT FOUND")
    print("=" * 60)
    print(f"  Missing: {', '.join(missing)}")
    print()
    print("  This bot requires FFmpeg to create video stickers.")
    print()
    print("  Install options:")
    print("    • Ubuntu/Debian:  sudo apt install ffmpeg")
    print("    • macOS:          brew install ffmpeg")
    print("    • Windows:        Download from https://ffmpeg.org/download.html")
    print("                        and add to PATH, or place ffmpeg.exe/ffprobe.exe")
    print(f"                        in: {_BIN_DIR}")
    print("    • Docker:         Already included in the Docker image")
    print()
    print("  You can also place static binaries in the 'bin/' directory:")
    print(f"    {_BIN_DIR}/ffmpeg")
    print(f"    {_BIN_DIR}/ffprobe")
    print("=" * 60)
    sys.exit(1)

print("━━━━━━━━━━━━━━━━━━━━━━")
print("FFmpeg Check")
print(f"✓ ffmpeg : {FFMPEG_PATH}")
print(f"✓ ffprobe: {FFPROBE_PATH}")
print("━━━━━━━━━━━━━━━━━━━━━━")

# ==============================================
# FORCE-SUBSCRIBE SYSTEM
# ==============================================
from force_subscribe import ForceSubscribe
fsub = ForceSubscribe(app, db, admin_ids=ADMIN_IDS)

# ==============================================
# HANDLER MODULE IMPORTS
# ==============================================
from handlers import plain as plain_handler
from handlers import vid as vid_handler
from handlers import quote as quote_handler
from handlers import kang as kang_handler
from handlers import admin_sys as admin_sys_handler
from handlers import premium as premium_handler
from handlers import logger as logger_handler
from handlers import grid as grid_handler
from handlers.logger import send_log

# ==============================================
# SMALL CAPS CONVERTER
# ==============================================
def small_caps(text: str) -> str:
    mapping = {
        'a':'ᴀ','b':'ʙ','c':'ᴄ','d':'ᴅ','e':'ᴇ','f':'ꜰ','g':'ɢ','h':'ʜ','i':'ɪ','j':'ᴊ','k':'ᴋ','l':'ʟ','m':'ᴍ','n':'ɴ','o':'ᴏ','p':'ᴘ','q':'ǫ','r':'ʀ','s':'s','t':'ᴛ','u':'ᴜ','v':'ᴠ','w':'ᴡ','x':'x','y':'ʏ','z':'ᴢ',
        'A':'ᴀ','B':'ʙ','C':'ᴄ','D':'ᴅ','E':'ᴇ','F':'ꜰ','G':'ɢ','H':'ʜ','I':'ɪ','J':'ᴊ','K':'ᴋ','L':'ʟ','M':'ᴍ','N':'ɴ','O':'ᴏ','P':'ᴘ','Q':'ǫ','R':'ʀ','S':'s','T':'ᴛ','U':'ᴜ','V':'ᴠ','W':'ᴡ','X':'x','Y':'ʏ','Z':'ᴢ',
        '0':'𝟶','1':'𝟷','2':'𝟸','3':'𝟹','4':'𝟺','5':'𝟻','6':'𝟼','7':'𝟽','8':'𝟾','9':'𝟿'
    }
    return ''.join(mapping.get(ch, ch) for ch in text)

# ==============================================
# MONGODB STORAGE HELPERS
# ==============================================
def get_user_data(user_id):
    user_data = users_collection.find_one({"user_id": str(user_id)})
    if not user_data:
        user_data = {
            "user_id": str(user_id),
            "pack_name": None,
            "emoji": DEFAULT_EMOJI,
            "total_stickers": 0,
            "created_at": datetime.now(),
            "last_active": datetime.now()
        }
        users_collection.insert_one(user_data)
    return user_data

def update_user_data(user_id, update_data):
    users_collection.update_one(
        {"user_id": str(user_id)},
        {"$set": {**update_data, "last_active": datetime.now()}}
    )

def get_user_pack(user_id):
    user_data = get_user_data(user_id)
    return {
        "name": user_data.get("pack_name"),
        "emoji": user_data.get("emoji", DEFAULT_EMOJI)
    }

def update_user_pack(user_id, pack_name, emoji=None):
    update_data = {"pack_name": pack_name}
    if emoji:
        update_data["emoji"] = emoji
    update_user_data(user_id, update_data)

def increment_sticker_count(user_id):
    users_collection.update_one(
        {"user_id": str(user_id)},
        {"$inc": {"total_stickers": 1}, "$set": {"last_active": datetime.now()}}
    )

def get_all_users():
    return list(users_collection.find())

def get_stats():
    total_users = users_collection.count_documents({})
    total_stickers = users_collection.aggregate([
        {"$group": {"_id": None, "total": {"$sum": "$total_stickers"}}}
    ])
    total_stickers = list(total_stickers)
    return {
        "total_users": total_users,
        "total_stickers": total_stickers[0]["total"] if total_stickers else 0
    }

def has_started_bot(user_id):
    user_data = get_user_data(user_id)
    return bool(user_data.get("started_bot", False))

def get_user_packs(user_id):
    return list(packs_collection.find({"user_id": str(user_id)}).sort("pack_index", 1))

def get_next_pack_index(user_id):
    last = packs_collection.find_one(
        {"user_id": str(user_id)},
        sort=[("pack_index", -1)]
    )
    return (last["pack_index"] + 1) if last else 1

def get_pack_by_index(user_id, pack_index):
    return packs_collection.find_one({"user_id": str(user_id), "pack_index": pack_index})

def create_pack_record(user_id, pack_index, display_title, telegram_pack_name, emoji, pack_type="static"):
    packs_collection.insert_one({
        "user_id": str(user_id),
        "pack_index": pack_index,
        "display_title": display_title,
        "telegram_pack_name": telegram_pack_name,
        "emoji": emoji,
        "pack_type": pack_type,
        "created_at": datetime.now(),
        "total_stickers": 1
    })
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(send_log(
            app,
            db,
            f"📦 <b>New Sticker Pack Created</b>\n"
            f"├ 👤 <b>User ID:</b> <code>{user_id}</code>\n"
            f"├ 📝 <b>Title:</b> {html.escape(str(display_title))}\n"
            f"├ 🎨 <b>Type:</b> <code>{html.escape(str(pack_type))}</code>\n"
            f"└ 🔗 <b>Link:</b> https://t.me/addstickers/{telegram_pack_name}",
        ))
    except Exception:
        pass

def increment_pack_sticker_count(user_id, pack_index):
    packs_collection.update_one(
        {"user_id": str(user_id), "pack_index": pack_index},
        {"$inc": {"total_stickers": 1}}
    )

def generate_telegram_pack_name(display_title, bot_username=None):
    if not bot_username:
        bot_username = _resolve_bot_username()
    if not bot_username:
        raise RuntimeError("Bot username is not set yet.")
    base = display_title.strip().lower()
    base = re.sub(r'\s+', '_', base)
    base = re.sub(r'[^a-z0-9_]', '', base)
    base = re.sub(r'_+', '_', base)
    base = base.strip('_')
    if not base:
        base = "pack"
    if base[0].isdigit():
        base = f"p_{base}"
    bot_username = bot_username.lower()
    suffix = f"_by_{bot_username}"
    max_base_len = 64 - len(suffix)
    if max_base_len < 1:
        max_base_len = 20
    base = base[:max_base_len]
    return f"{base}{suffix}"

def validate_telegram_pack_name(name):
    if not name or len(name) > 64:
        return False
    if not re.match(r'^[a-z0-9_]+$', name):
        return False
    return True

def delete_pack_record(user_id, pack_index):
    packs_collection.delete_one({"user_id": str(user_id), "pack_index": pack_index})

def update_pack_display_title(user_id, pack_index, new_title):
    packs_collection.update_one(
        {"user_id": str(user_id), "pack_index": pack_index},
        {"$set": {"display_title": new_title}}
    )

# ==============================================
# IMAGE PROCESSING WITH PILLOW
# ==============================================
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/Arial.ttf",
    os.getenv("CUSTOM_FONT_PATH", "")
]

def get_font(size):
    for path in FONT_PATHS:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()

def draw_text_with_outline(draw, xy, text, font):
    x, y = xy
    offsets = [(-2,-2),(-2,0),(-2,2),(0,-2),(0,2),(2,-2),(2,0),(2,2)]
    for dx, dy in offsets:
        draw.text((x+dx, y+dy), text, font=font, fill="black")
    draw.text((x, y), text, font=font, fill="white")

def wrap_text(text, max_width, font, draw):
    words = text.split()
    lines = []
    cur = []
    for w in words:
        test = ' '.join(cur + [w])
        bbox = draw.textbbox((0,0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            cur.append(w)
        else:
            if cur:
                lines.append(' '.join(cur))
            cur = [w]
    if cur:
        lines.append(' '.join(cur))
    return '\n'.join(lines)

def create_sticker(image_bytes, text, position, size_multiplier=1.0):
    img = Image.open(BytesIO(image_bytes)).convert("RGBA")
    img.thumbnail((512, 512), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(img)

    base_font_size = max(20, img.width // 12)
    font_size = int(base_font_size * size_multiplier)
    font = get_font(font_size)

    max_w = int(img.width * 0.8)
    wrapped = wrap_text(text, max_w, font, draw)

    bbox = draw.multiline_textbbox((0,0), wrapped, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]

    pad = 20
    if "left" in position:
        x = pad
    elif "right" in position:
        x = img.width - tw - pad
    else:
        x = (img.width - tw) // 2

    if "top" in position:
        y = pad
    elif "bot" in position:
        y = img.height - th - pad
    else:
        y = (img.height - th) // 2

    lines = wrapped.split('\n')
    line_height = font_size + 5
    cy = y
    for line in lines:
        draw_text_with_outline(draw, (x, cy), line, font)
        cy += line_height

    out = BytesIO()
    img.save(out, format="PNG")
    out.seek(0)
    return out.getvalue()

def convert_webp_to_png(image_bytes):
    img = Image.open(BytesIO(image_bytes)).convert("RGBA")
    out = BytesIO()
    img.save(out, format="PNG")
    out.seek(0)
    return out.getvalue()

# ==============================================
# PYROGRAM STICKER SET HELPERS
# ==============================================
TELEGRAM_BOT_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

def normalize_sticker_png(image_bytes):
    img = Image.open(BytesIO(image_bytes))
    img = img.convert("RGBA")

    w, h = img.size
    if w >= h:
        new_w = 512
        new_h = max(1, round(h * (512 / w)))
    else:
        new_h = 512
        new_w = max(1, round(w * (512 / h)))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    offset_x = (512 - new_w) // 2
    offset_y = (512 - new_h) // 2
    canvas.paste(resized, (offset_x, offset_y), resized)

    out = BytesIO()
    canvas.save(out, format="PNG")
    out.seek(0)
    return out.getvalue()

async def _bot_api_upload(method, form_fields, file_field_name, file_bytes, file_name, content_type="image/png"):
    url = f"{TELEGRAM_BOT_API_BASE}/{method}"
    form = aiohttp.FormData()
    for key, value in form_fields.items():
        form.add_field(key, str(value))
    form.add_field(file_field_name, file_bytes, filename=file_name, content_type=content_type)
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=form) as resp:
            result = await resp.json()
    print(f"[TELEGRAM API] Method: {method} | OK: {result.get('ok')} | Description: {result.get('description', 'N/A')}")
    if not result.get("ok"):
        raise Exception(result.get("description", "Unknown Telegram Bot API error"))
    return result.get("result")

async def _bot_api_upload_multi(method, form_fields, files_dict, session=None):
    """Upload multiple files (field -> (bytes, filename, content_type)) in a single Bot API call."""
    url = f"{TELEGRAM_BOT_API_BASE}/{method}"
    form = aiohttp.FormData()
    for key, value in form_fields.items():
        form.add_field(key, str(value))
    for field_name, (fbytes, fname, ctype) in files_dict.items():
        form.add_field(field_name, fbytes, filename=fname, content_type=ctype)
    if session is not None:
        async with session.post(url, data=form) as resp:
            result = await resp.json()
    else:
        async with aiohttp.ClientSession() as temp_session:
            async with temp_session.post(url, data=form) as resp:
                result = await resp.json()
    print(f"[TELEGRAM API] Method: {method} | OK: {result.get('ok')} | Description: {result.get('description', 'N/A')}")
    if not result.get("ok"):
        raise Exception(result.get("description", "Unknown Telegram Bot API error"))
    return result.get("result")

async def create_sticker_pack(client, user_id, pack_name, title, sticker_bytes, emoji):
    try:
        normalized_bytes = normalize_sticker_png(sticker_bytes)

        bot_username = _resolve_bot_username(client)
        username_suffix = f" • @{bot_username}"
        max_title_len = 64 - len(username_suffix)
        if max_title_len < 1:
            max_title_len = 30
        visible_title = f"{title[:max_title_len]}{username_suffix}"

        print(f"[CREATE PACK] Original Title: {title}")
        print(f"[CREATE PACK] Bot Username: {bot_username}")
        print(f"[CREATE PACK] Final Sticker Set Name: {pack_name}")
        print(f"[CREATE PACK] Length: {len(pack_name)}")
        print(f"[CREATE PACK] Regex Valid: {bool(re.match(r'^[a-z0-9_]+$', pack_name))}")

        stickers_payload = json.dumps([{
            "sticker": "attach://sticker_file",
            "format": "static",
            "emoji_list": [emoji]
        }])
        await _bot_api_upload(
            "createNewStickerSet",
            {
                "user_id": user_id,
                "name": pack_name,
                "title": visible_title,
                "stickers": stickers_payload
            },
            "sticker_file",
            normalized_bytes,
            "sticker.png"
        )
        return True, f"https://t.me/addstickers/{pack_name}"
    except Exception as e:
        return False, str(e)

async def add_sticker_to_pack(client, user_id, pack_name, sticker_bytes, emoji):
    try:
        normalized_bytes = normalize_sticker_png(sticker_bytes)
        sticker_payload = json.dumps({
            "sticker": "attach://sticker_file",
            "format": "static",
            "emoji_list": [emoji]
        })
        await _bot_api_upload(
            "addStickerToSet",
            {
                "user_id": user_id,
                "name": pack_name,
                "sticker": sticker_payload
            },
            "sticker_file",
            normalized_bytes,
            "sticker.png"
        )
        return True, None
    except Exception as e:
        return False, str(e)

async def create_video_sticker_pack(client, user_id, pack_name, title, sticker_bytes, emoji):
    try:
        bot_username = _resolve_bot_username(client)
        username_suffix = f" • @{bot_username}"
        max_title_len = 64 - len(username_suffix)
        if max_title_len < 1:
            max_title_len = 30
        visible_title = f"{title[:max_title_len]}{username_suffix}"

        print(f"[CREATE VIDEO PACK] Original Title: {title}")
        print(f"[CREATE VIDEO PACK] Bot Username: {bot_username}")
        print(f"[CREATE VIDEO PACK] Final Sticker Set Name: {pack_name}")
        print(f"[CREATE VIDEO PACK] Length: {len(pack_name)}")
        print(f"[CREATE VIDEO PACK] Regex Valid: {bool(re.match(r'^[a-z0-9_]+$', pack_name))}")

        stickers_payload = json.dumps([{
            "sticker": "attach://sticker_file",
            "format": "video",
            "emoji_list": [emoji]
        }])
        await _bot_api_upload(
            "createNewStickerSet",
            {
                "user_id": user_id,
                "name": pack_name,
                "title": visible_title,
                "stickers": stickers_payload
            },
            "sticker_file",
            sticker_bytes,
            "sticker.webm",
            content_type="video/webm"
        )
        return True, f"https://t.me/addstickers/{pack_name}"
    except Exception as e:
        return False, str(e)

async def add_video_sticker_to_pack(client, user_id, pack_name, sticker_bytes, emoji):
    try:
        sticker_payload = json.dumps({
            "sticker": "attach://sticker_file",
            "format": "video",
            "emoji_list": [emoji]
        })
        await _bot_api_upload(
            "addStickerToSet",
            {
                "user_id": user_id,
                "name": pack_name,
                "sticker": sticker_payload
            },
            "sticker_file",
            sticker_bytes,
            "sticker.webm",
            content_type="video/webm"
        )
        return True, None
    except Exception as e:
        return False, str(e)

MAX_PACK_NAME_RETRIES = 25

def _pack_name_taken(error_text):
    if not error_text:
        return False
    low = error_text.lower()
    return "occupied" in low or ("already" in low and "name" in low)

def _suffixed_pack_name(base_name, n, bot_username):
    suffix = f"_by_{bot_username}"
    core = base_name[:-len(suffix)] if base_name.endswith(suffix) else base_name
    tail = f"_{n}{suffix}"
    max_core_len = 64 - len(tail)
    if max_core_len < 1:
        max_core_len = 10
    core = core[:max_core_len]
    return f"{core}{tail}"

async def create_unique_sticker_pack(client, user_id, display_title, sticker_bytes, emoji):
    bot_username = _resolve_bot_username(client)
    base_name = generate_telegram_pack_name(display_title, bot_username)
    candidate = base_name
    attempt = 0
    last_error = None
    while attempt <= MAX_PACK_NAME_RETRIES:
        success, result = await create_sticker_pack(client, user_id, candidate, display_title, sticker_bytes, emoji)
        if success:
            return True, result, candidate
        last_error = result
        if _pack_name_taken(result):
            attempt += 1
            candidate = _suffixed_pack_name(base_name, attempt, bot_username)
            continue
        return False, result, candidate
    return False, last_error, candidate

async def create_unique_video_sticker_pack(client, user_id, display_title, sticker_bytes, emoji):
    bot_username = _resolve_bot_username(client)
    base_name = generate_telegram_pack_name(display_title, bot_username)
    candidate = base_name
    attempt = 0
    last_error = None
    while attempt <= MAX_PACK_NAME_RETRIES:
        success, result = await create_video_sticker_pack(client, user_id, candidate, display_title, sticker_bytes, emoji)
        if success:
            return True, result, candidate
        last_error = result
        if _pack_name_taken(result):
            attempt += 1
            candidate = _suffixed_pack_name(base_name, attempt, bot_username)
            continue
        return False, result, candidate
    return False, last_error, candidate

async def set_sticker_set_title(pack_name, title):
    url = f"{TELEGRAM_BOT_API_BASE}/setStickerSetTitle"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data={"name": pack_name, "title": title}) as resp:
            result = await resp.json()
    if not result.get("ok"):
        raise Exception(result.get("description", "Unknown Telegram Bot API error"))
    return True

async def _bot_api_json(method, payload, session=None, _retries=0):
    """Send a Bot API POST request, optionally reusing a keep-alive ClientSession."""
    url = f"{TELEGRAM_BOT_API_BASE}/{method}"
    req_timeout = aiohttp.ClientTimeout(total=25)
    if session is not None:
        async with session.post(url, json=payload, timeout=req_timeout) as resp:
            result = await resp.json()
    else:
        async with aiohttp.ClientSession(timeout=req_timeout) as temp_session:
            async with temp_session.post(url, json=payload) as resp:
                result = await resp.json()
    if not result.get("ok"):
        desc = result.get("description", "Unknown Telegram Bot API error")
        retry_after = result.get("parameters", {}).get("retry_after")
        if retry_after and _retries < 2 and float(retry_after) <= 15:
            await asyncio.sleep(float(retry_after) + 0.5)
            return await _bot_api_json(method, payload, session=session, _retries=_retries + 1)
        raise Exception(desc)
    return result.get("result")

async def create_unique_sticker_pack_from_input(user_id, display_title, stickers_list, session=None, sticker_type=None):
    """Create a new sticker pack using InputSticker dicts (e.g. file_id), up to 50 stickers at once."""
    bot_username = _resolve_bot_username()
    base_name = generate_telegram_pack_name(display_title, bot_username)
    username_suffix = f" • @{bot_username}"
    max_title_len = 64 - len(username_suffix)
    if max_title_len < 1:
        max_title_len = 30
    visible_title = f"{display_title[:max_title_len]}{username_suffix}"

    candidate = base_name
    attempt = 0
    last_error = None
    while attempt <= MAX_PACK_NAME_RETRIES:
        try:
            payload = {
                "user_id": user_id,
                "name": candidate,
                "title": visible_title,
                "stickers": stickers_list,
            }
            if sticker_type:
                payload["sticker_type"] = sticker_type
            await _bot_api_json("createNewStickerSet", payload, session=session)
            return True, f"https://t.me/addstickers/{candidate}", candidate
        except Exception as e:
            err = str(e)
            last_error = err
            if _pack_name_taken(err):
                attempt += 1
                candidate = _suffixed_pack_name(base_name, attempt, bot_username)
                continue
            return False, err, candidate
    return False, last_error, candidate

async def add_sticker_input_to_pack(user_id, pack_name, sticker_dict, session=None):
    """Add a single InputSticker dict (e.g. file_id) to an existing sticker pack."""
    try:
        await _bot_api_json(
            "addStickerToSet",
            {
                "user_id": user_id,
                "name": pack_name,
                "sticker": sticker_dict,
            },
            session=session,
        )
        return True, None
    except Exception as e:
        return False, str(e)

# ==============================================
# /clonepack HELPERS
# ==============================================
async def get_sticker_set(short_name):
    url = f"{TELEGRAM_BOT_API_BASE}/getStickerSet"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params={"name": short_name}) as resp:
            result = await resp.json()
    if not result.get("ok"):
        raise Exception(result.get("description", "Unknown Telegram Bot API error"))
    return result.get("result")

def extract_pack_short_name(raw):
    raw = raw.strip()
    m = re.match(r'^(?:https?://)?t\.me/addstickers/([A-Za-z0-9_]+)/?$', raw, re.IGNORECASE)
    if m:
        return m.group(1)
    if re.match(r'^[A-Za-z0-9_]+$', raw):
        return raw
    return None

# ==============================================
# VIDEO STICKER & CATBOX HELPERS
# ==============================================
CATBOX_UPLOAD_URL = "https://catbox.moe/user/api.php"
YUKI_UPLOAD_URL = "https://yukiapi.site/upload"
MAX_PREVIEW_SECONDS = 3.0
DURATION_TOLERANCE = 0.05

def cleanup_temp_files(*paths):
    for path in paths:
        if not path:
            continue
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

async def probe_video_duration(path):
    if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    cmd = [FFPROBE_PATH, "-v", "error", "-show_entries", "format=duration", "-of", "json", path]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        data = json.loads(stdout.decode(errors="ignore"))
        dur_str = data.get("format", {}).get("duration")
        return float(dur_str) if dur_str else None
    except Exception:
        return None

def _escape_ffmpeg_text(text):
    return (
        text.replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", "\\'")
            .replace("%", "\\%")
    )

async def normalize_video_sticker(input_path, output_path):
    vf = "scale=512:512:force_original_aspect_ratio=decrease,pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black"
    cmd = [
        FFMPEG_PATH, "-y",
        "-i", input_path,
        "-t", str(MAX_PREVIEW_SECONDS),
        "-vf", vf,
        "-c:v", "libvpx",
        "-b:v", "200k",
        "-maxrate", "250k",
        "-bufsize", "500k",
        "-an",
        "-deadline", "good",
        "-cpu-used", "4",
        "-auto-alt-ref", "0",
        output_path
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(output_path):
            err = stderr.decode(errors="ignore")[-500:] if stderr else "Unknown FFmpeg error"
            return False, err
        file_size = os.path.getsize(output_path)
        if file_size > 256 * 1024:
            cmd_retry = [
                FFMPEG_PATH, "-y",
                "-i", input_path,
                "-t", str(MAX_PREVIEW_SECONDS),
                "-vf", vf,
                "-c:v", "libvpx",
                "-b:v", "120k",
                "-maxrate", "150k",
                "-bufsize", "300k",
                "-an",
                "-deadline", "good",
                "-cpu-used", "4",
                "-auto-alt-ref", "0",
                output_path
            ]
            proc2 = await asyncio.create_subprocess_exec(
                *cmd_retry,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc2.communicate()
            if proc2.returncode != 0 or not os.path.exists(output_path):
                return False, "Failed to compress video to under 256KB."
        return True, None
    except FileNotFoundError:
        return False, "FFmpeg is not installed on the server."
    except Exception as e:
        return False, str(e)

async def generate_vid_preview(input_path, output_path, text=None):
    vf_parts = [
        "scale=512:512:force_original_aspect_ratio=decrease",
        "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black"
    ]
    if text:
        safe_text = _escape_ffmpeg_text(text)
        vf_parts.append(
            f"drawtext=text='{safe_text}':fontcolor=white:fontsize=48:"
            f"borderw=3:bordercolor=black:x=(w-text_w)/2:y=h-th-30"
        )
    vf = ",".join(vf_parts)

    cmd = [
        FFMPEG_PATH, "-y",
        "-i", input_path,
        "-t", str(MAX_PREVIEW_SECONDS),
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "28",
        "-pix_fmt", "yuv420p",
        "-an",
        "-movflags", "+faststart",
        output_path
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(output_path):
            err = stderr.decode(errors="ignore")[-500:] if stderr else "Unknown FFmpeg error"
            return False, err
        return True, None
    except FileNotFoundError:
        return False, "FFmpeg is not installed on the server."
    except Exception as e:
        return False, str(e)

async def upload_to_yuki(file_path):
    for attempt in range(1, 4):
        try:
            with open(file_path, "rb") as f:
                file_bytes = f.read()
            form = aiohttp.FormData()
            form.add_field("file", file_bytes, filename=os.path.basename(file_path))
            async with aiohttp.ClientSession() as session:
                async with session.post(YUKI_UPLOAD_URL, data=form, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status >= 500:
                        if attempt < 3:
                            await asyncio.sleep(attempt)
                        continue
                    result_text = (await resp.text()).strip()
            if result_text and result_text.startswith("http"):
                return result_text
        except (asyncio.TimeoutError, aiohttp.ClientError, OSError):
            if attempt < 3:
                await asyncio.sleep(attempt)
            continue
    return None

async def upload_to_catbox(file_path):
    yuki_url = await upload_to_yuki(file_path)
    if yuki_url:
        print("[YUKI] Upload to Yuki successful")
        return yuki_url

    print("[YUKI] Yuki upload failed -> falling back to Catbox")

    with open(file_path, "rb") as f:
        file_bytes = f.read()

    form = aiohttp.FormData()
    form.add_field("reqtype", "fileupload")
    form.add_field("fileToUpload", file_bytes, filename=os.path.basename(file_path), content_type="video/mp4")

    async with aiohttp.ClientSession() as session:
        async with session.post(CATBOX_UPLOAD_URL, data=form) as resp:
            result_text = (await resp.text()).strip()

    if not result_text.startswith("http"):
        print("[CATBOX] Catbox upload failed")
        return None
    print("[CATBOX] Catbox upload successful")
    return result_text

def prepare_plain_image_sticker(image_bytes):
    return normalize_sticker_png(image_bytes)

# ==============================================
# ISOLATED SESSION STORAGE
# ==============================================
user_sessions = {}
create_sessions = {}
generated_commands = {}
vid_sessions = {}     # Dedicated session store for /vid
plain_sessions = {}   # Dedicated session store for /plain
kang_sessions = {}    # Dedicated session store for /kang

# ==============================================
# REUSABLE CENTRALIZED SESSION CLEANUP HELPER
# ==============================================
async def cancel_user_session(client: Client, user_id: int, chat_id: int = None, callback: CallbackQuery = None, trigger_message: Message = None):
    """
    Centralized, error-safe session cancellation and resource cleanup.
    Works for all active sessions (user_sessions, plain_sessions, vid_sessions, create_sessions).
    """
    uid = user_id
    cancelled_any = False

    if uid in plain_sessions:
        sess = plain_sessions.pop(uid, None)
        if sess:
            cancelled_any = True
            cleanup_temp_files(sess.get("media_path"))
            prev_msg = sess.get("preview_message")
            if prev_msg:
                try:
                    await prev_msg.delete()
                except Exception:
                    pass

    if uid in vid_sessions:
        sess = vid_sessions.pop(uid, None)
        if sess:
            cancelled_any = True
            cleanup_temp_files(sess.get("video_path"), sess.get("webm_path"), sess.get("preview_path"))
            prev_msg = sess.get("preview_message")
            if prev_msg:
                try:
                    await prev_msg.delete()
                except Exception:
                    pass

    if uid in user_sessions:
        sess = user_sessions.pop(uid, None)
        if sess:
            cancelled_any = True

    if uid in create_sessions:
        sess = create_sessions.pop(uid, None)
        if sess:
            cancelled_any = True

    if uid in kang_sessions:
        sess = kang_sessions.pop(uid, None)
        if sess:
            cancelled_any = True

    try:
        prem_res = db["premium_sessions"].delete_one({"user_id": str(uid)})
        if prem_res.deleted_count > 0:
            cancelled_any = True
    except Exception:
        pass

    try:
        grid_res = db["grid_sessions"].delete_one({"user_id": str(uid)})
        if grid_res.deleted_count > 0:
            cancelled_any = True
    except Exception:
        pass

    cancellation_text = small_caps(
        "❌ **Process Cancelled**\n\n"
        "Your current operation has been cancelled.\n"
        "You can start again anytime."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Main Menu", callback_data="global_main_menu"),
         InlineKeyboardButton("🆕 Create Sticker", callback_data="global_start_sticker")]
    ])

    if callback:
        try:
            await callback.answer()
        except Exception:
            pass
        if cancelled_any:
            try:
                await callback.message.edit_text(cancellation_text, reply_markup=kb)
            except Exception:
                try:
                    await callback.message.reply_text(cancellation_text, reply_markup=kb)
                except Exception:
                    pass
        else:
            try:
                await callback.message.reply_text(small_caps("There is no active process to cancel."))
            except Exception:
                pass
        return

    if trigger_message:
        if cancelled_any:
            await reply_or_dm(client, trigger_message, cancellation_text, reply_markup=kb)
        else:
            await reply_or_dm(client, trigger_message, small_caps("There is no active process to cancel."))
        return

# ==============================================
# COMMAND REGISTRY FOR /help AND /adminhelp
# ==============================================
from collections import OrderedDict

CATEGORY_STICKER = "📦 Sticker Commands"
CATEGORY_GENERAL = "ℹ️ General Commands"

USER_COMMAND_CATEGORIES = OrderedDict()
ADMIN_COMMANDS = []

def register_user_command(command, description, category=CATEGORY_GENERAL):
    target = USER_COMMAND_CATEGORIES
    main_mod = sys.modules.get("__main__")
    if main_mod and hasattr(main_mod, "USER_COMMAND_CATEGORIES"):
        target = main_mod.USER_COMMAND_CATEGORIES
    entries = target.setdefault(category, [])
    if not any(cmd == command for cmd, _ in entries):
        entries.append((command, description))

def register_admin_command(command, description):
    target = ADMIN_COMMANDS
    main_mod = sys.modules.get("__main__")
    if main_mod and hasattr(main_mod, "ADMIN_COMMANDS"):
        target = main_mod.ADMIN_COMMANDS
    if not any(cmd == command for cmd, _ in target):
        target.append((command, description))

# ==============================================
# HELPER: Reply to user in DM or Group
# ==============================================
async def reply_or_dm(client, message, text, reply_markup=None, parse_mode=None):
    if message.chat.type == "private":
        return await message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        mention = f"[{message.from_user.first_name}](tg://user?id={message.from_user.id})"
        return await message.reply_text(f"{mention}\n\n{text}", reply_markup=reply_markup, parse_mode=parse_mode)

# ==============================================
# GROUP FIRST-TIME START GATE
# ==============================================
_THIS_BOT_COMMANDS = {
    "start", "sticker", "plain", "vid", "pr", "premium", "done",
    "grid", "split", "toemoji", "textemoji",
    "q", "quote", "r", "kang",
    "clonepack", "renamepack", "delpack", "mypacks", "setemoji", "reset",
    "help", "cancel",
    "admin", "adminhelp", "stats", "btstats", "users", "ping", "restart",
    "bc", "fbc", "addsudo", "rmsudo", "delsudo", "removesudo", "sudolist", "admins",
    "setlog", "setlogger", "create",
    "addchannel", "addch", "removechannel", "delchannel", "rmchannel", "remchannel", "remove",
    "channels", "listch", "listchannel", "listchannels", "mychats", "getid", "fsubdebug",
}

def _extract_bot_cmd(client, text: str):
    """Return lowercase command name if text is a command for THIS bot, else None."""
    if not text or not text.startswith("/"):
        return None
    first_token = text.split()[0].lstrip("/")
    if "@" in first_token:
        cmd_name, target_bot = first_token.split("@", 1)
        my_uname = (_resolve_bot_username(client) or "").lower()
        if my_uname and target_bot.lower() != my_uname:
            return None
    else:
        cmd_name = first_token
    cmd_low = cmd_name.lower()
    return cmd_low if cmd_low in _THIS_BOT_COMMANDS else None

@app.on_message(filters.group & filters.text & filters.regex(r'^/\w'), group=-3)
async def group_start_gate(client: Client, message: Message):
    # Track group in bot_known_chats for /bc & /fbc group broadcasts
    try:
        db["bot_known_chats"].update_one(
            {"chat_id": message.chat.id},
            {"$set": {
                "chat_id": message.chat.id,
                "title": message.chat.title or str(message.chat.id),
                "username": message.chat.username,
                "type": str(message.chat.type),
            }},
            upsert=True,
        )
    except Exception:
        pass

    if not _extract_bot_cmd(client, message.text):
        return

    if not message.from_user:
        return
    uid = message.from_user.id
    if has_started_bot(uid):
        return

    bot_uname = _resolve_bot_username(client) or "KiwiStickerRobot"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Start in DM", url=f"https://t.me/{bot_uname}?start=start")]
    ])
    await message.reply_text(
        small_caps("👋 You need to start me in private first.\nPress the button below."),
        reply_markup=kb
    )
    raise StopPropagation

# ==============================================
# GLOBAL 20s COMMAND RATE LIMIT (FLOOD WAIT)
# ==============================================
COMMAND_COOLDOWN_SECONDS = int(os.getenv("COMMAND_COOLDOWN_SECONDS", "20"))
_cmd_cooldowns = {}
_cooldown_warned = {}
_RATE_LIMIT_EXEMPT_CMDS = {"cancel", "done"}

@app.on_message((filters.private | filters.group) & filters.text & filters.regex(r'^/\w'), group=-2)
async def cmd_rate_limit_gate(client: Client, message: Message):
    if not message.from_user:
        return
    cmd_low = _extract_bot_cmd(client, message.text)
    if not cmd_low or cmd_low in _RATE_LIMIT_EXEMPT_CMDS:
        return

    uid = message.from_user.id
    if is_admin(uid):
        return

    now = time.time()
    last = _cmd_cooldowns.get(uid, 0.0)
    elapsed = now - last
    if elapsed < COMMAND_COOLDOWN_SECONDS:
        # Warn once per cooldown window so spamming commands doesn't spam the GC
        if _cooldown_warned.get(uid, 0.0) != last:
            _cooldown_warned[uid] = last
            wait_sec = max(1, int(COMMAND_COOLDOWN_SECONDS - elapsed) + 1)
            await reply_or_dm(
                client,
                message,
                small_caps(f"⏳ Flood Wait: Please wait {wait_sec}s before using another command."),
            )
        raise StopPropagation

    _cmd_cooldowns[uid] = now
    _cooldown_warned.pop(uid, None)

# ==============================================
# GLOBAL /cancel COMMAND
# ==============================================
@app.on_message(filters.command("cancel") & (filters.private | filters.group))
async def global_cancel_cmd(client: Client, message: Message):
    uid = message.from_user.id
    await cancel_user_session(client, user_id=uid, trigger_message=message)
register_user_command("cancel", "Cancel any active process and clear session.", category=CATEGORY_GENERAL)

# ==============================================
# /start
# ==============================================
def _build_start_caption():
    title_block = small_caps("🌟 Kiwi Sticker Maker Bot 🌟\nFast Sticker & Custom Emoji Maker Bot for Telegram.")
    cmds_block = "\n".join([
        f"🖼 <code>/sticker</code> — {small_caps('Text-overlaid sticker')}",
        f"🖼 <code>/plain</code> — {small_caps('Plain sticker (no text)')}",
        f"🎥 <code>/vid</code> — {small_caps('Video sticker (MP4 / WEBM / GIF)')}",
        f"✨ <code>/pr</code> — {small_caps('Premium Custom Emoji Pack')}",
        f"🧩 <code>/grid</code> — {small_caps('Photo Grid, Text & Pack Converter')}",
        f"💬 <code>/q</code> / <code>/r</code> — {small_caps('Quote-card sticker')}",
        f"🧲 <code>/kang</code> — {small_caps('Save sticker to new/existing pack')}",
        f"⚡ <code>/clonepack</code> — {small_caps('Fast clone sticker packs')}",
        f"📦 <code>/mypacks</code> — {small_caps('View & manage your packs')}",
    ])
    footer = small_caps("🚀 Tap Commands below or send /help to begin!")
    return (
        f"<blockquote><b>{title_block}</b></blockquote>\n\n"
        f"<blockquote>{cmds_block}</blockquote>\n\n"
        f"{footer}"
    )

def _build_start_keyboard(client=None):
    bot_uname = _resolve_bot_username(client) or "KiwiStickerRobot"
    owner_uname = os.getenv("OWNER_USERNAME", "").strip().lstrip("@")
    if owner_uname:
        owner_url = f"https://t.me/{owner_uname}"
    elif OWNER_IDS:
        owner_url = f"tg://user?id={OWNER_IDS[0]}"
    else:
        owner_url = f"https://t.me/{bot_uname}"

    support_url = os.getenv("SUPPORT_URL", "").strip() or "https://t.me/KIWI_DPX"
    updates_url = os.getenv("UPDATES_URL", "").strip() or "https://t.me/KIWI_DPX"

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Me to Group", url=f"https://t.me/{bot_uname}?startgroup=true")],
        [
            InlineKeyboardButton("📋 Commands", callback_data="start_help_menu"),
            InlineKeyboardButton("👑 Owner", url=owner_url),
        ],
        [
            InlineKeyboardButton("💬 Support", url=support_url),
            InlineKeyboardButton("📢 Updates", url=updates_url),
        ],
    ])

@app.on_message(filters.command("start") & filters.private)
async def start_cmd(client: Client, message: Message):
    if not await fsub.check(client, message):
        return
    uid = message.from_user.id
    already_started = has_started_bot(uid)
    get_user_data(uid)
    update_user_data(uid, {"started_bot": True})
    u = message.from_user
    uname = f"@{u.username}" if u.username else "No username"
    badge = "🆕 New User" if not already_started else "👋 Returning User"
    asyncio.create_task(send_log(
        client,
        db,
        f"🚀 <b>Bot Started ({badge})</b>\n"
        f"├ 👤 <b>Name:</b> {html.escape(u.first_name or 'User')}\n"
        f"├ 📌 <b>Username:</b> {html.escape(uname)}\n"
        f"└ 🆔 <b>User ID:</b> <code>{uid}</code>",
    ))

    caption = _build_start_caption()
    kb = _build_start_keyboard(client)
    start_photo = os.getenv("START_PHOTO", "").strip()

    if start_photo:
        try:
            await message.reply_photo(
                photo=start_photo,
                caption=caption,
                reply_markup=kb,
                has_spoiler=True,
                parse_mode=HTML_PARSE_MODE,
            )
            return
        except Exception as e:
            print(f"[START] Failed to send START_PHOTO ({start_photo}): {e}")

    await message.reply_text(caption, reply_markup=kb, parse_mode=HTML_PARSE_MODE)

@app.on_callback_query(filters.regex(r"^start_help_menu$"))
async def start_help_menu_cb(client: Client, callback: CallbackQuery):
    lines = ["📋 <b>Help Menu</b>", ""]
    for category, commands in USER_COMMAND_CATEGORIES.items():
        lines.append(f"<b>{html.escape(category)}</b>")
        for command, description in commands:
            lines.append(f"• <code>/{html.escape(command)}</code> — {html.escape(description)}")
        lines.append("")
    text = "\n".join(lines).strip()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="start_back_home")]])
    await callback.answer()
    try:
        if callback.message.photo:
            await callback.message.edit_caption(text, reply_markup=kb, parse_mode=HTML_PARSE_MODE)
        else:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode=HTML_PARSE_MODE)
    except Exception:
        await callback.message.reply_text(text, reply_markup=kb, parse_mode=HTML_PARSE_MODE)

@app.on_callback_query(filters.regex(r"^start_back_home$"))
async def start_back_home_cb(client: Client, callback: CallbackQuery):
    caption = _build_start_caption()
    kb = _build_start_keyboard(client)
    await callback.answer()
    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption, reply_markup=kb, parse_mode=HTML_PARSE_MODE)
        else:
            await callback.message.edit_text(caption, reply_markup=kb, parse_mode=HTML_PARSE_MODE)
    except Exception:
        pass
register_user_command("start", "Start the bot and see the welcome message.", category=CATEGORY_GENERAL)

# ==============================================
# COMMAND: /sticker
# ==============================================
@app.on_message(filters.command("sticker") & (filters.private | filters.group))
async def sticker_start(client: Client, message: Message):
    if not await fsub.check(client, message):
        return
    uid = message.from_user.id
    user_sessions[uid] = {"step": "waiting_image"}
    await reply_or_dm(client, message, small_caps("📸 **Step 1/4:** Send me an image (photo or document)."))
register_user_command("sticker", "Create a sticker from any image.", category=CATEGORY_STICKER)

# /plain and /vid handlers are registered in handlers/plain.py and handlers/vid.py

# ==============================================
# COMMAND: /clonepack
# ==============================================
@app.on_message(filters.command("clonepack") & (filters.private | filters.group))
async def clonepack_cmd(client: Client, message: Message):
    if not await fsub.check(client, message):
        return
    uid = message.from_user.id
    user_sessions[uid] = {"step": "clonepack_waiting_link"}
    await reply_or_dm(
        client, message,
        small_caps("📦 Send the Telegram Sticker Pack link you want to clone.\n\nExample:\nhttps://t.me/addstickers/NarutoPack")
    )
register_user_command("clonepack", "Clone an existing Telegram sticker pack.", category=CATEGORY_STICKER)

# ==============================================
# COMMAND: /renamepack
# ==============================================
@app.on_message(filters.command("renamepack") & (filters.private | filters.group))
async def renamepack_cmd(client: Client, message: Message):
    if not await fsub.check(client, message):
        return
    uid = message.from_user.id
    user_sessions[uid] = {"step": "renamepack_waiting_index"}
    await reply_or_dm(client, message, small_caps("📦 Send the Pack Index.\n\nExample:\n1"))
register_user_command("renamepack", "Rename one of your sticker packs.", category=CATEGORY_STICKER)

# ==============================================
# COMMAND: /delpack
# ==============================================
@app.on_message(filters.command("delpack") & (filters.private | filters.group))
async def delpack_cmd(client: Client, message: Message):
    if not await fsub.check(client, message):
        return
    uid = message.from_user.id
    user_sessions[uid] = {"step": "delpack_waiting_index"}
    await reply_or_dm(client, message, small_caps("📦 Send the Pack Index you want to delete.\n\nExample:\n2"))
register_user_command("delpack", "Delete one of your sticker packs from the database.", category=CATEGORY_STICKER)

# ==============================================
# HANDLE IMAGE (/sticker Flow)
# ==============================================
@app.on_message((filters.photo | filters.document | filters.sticker) & (filters.private | filters.group), group=5)
async def handle_image(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    if uid not in user_sessions or user_sessions[uid].get("step") != "waiting_image":
        return

    is_sticker_input = False
    file_id = None
    if message.sticker:
        if message.sticker.is_animated or message.sticker.is_video:
            await reply_or_dm(client, message, small_caps("❌ Animated and Video stickers are currently not supported."))
            return
        file_id = message.sticker.file_id
        is_sticker_input = True
    elif message.photo:
        file_id = message.photo.file_id
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
        file_id = message.document.file_id
    else:
        await reply_or_dm(client, message, small_caps("❌ Please send a valid image file."))
        return

    proc = await reply_or_dm(client, message, small_caps("⚙️ Downloading image..."))
    try:
        file = await client.download_media(file_id, in_memory=True)
        img_bytes = file.getvalue()
        if is_sticker_input:
            img_bytes = convert_webp_to_png(img_bytes)
        user_sessions[uid]["image_bytes"] = img_bytes
        user_sessions[uid]["step"] = "waiting_text"
        await proc.edit_text(small_caps("✏️ **Step 2/4:** Send me the text you want on the sticker."))
    except Exception as e:
        await proc.edit_text(small_caps(f"❌ Error: {str(e)}"))

# /plain and /vid media handlers are registered in handlers/plain.py and handlers/vid.py

# ==============================================
# STEP HANDLERS FOR /sticker FLOW
# ==============================================
async def handle_new_pack_name(client: Client, message: Message, uid):
    display_title = message.text.strip()
    if not display_title:
        await reply_or_dm(client, message, small_caps("❌ Please send a valid pack name."))
        return

    sticker_bytes = user_sessions.get(uid, {}).get("sticker_bytes")
    if not sticker_bytes:
        await reply_or_dm(client, message, small_caps("❌ Session expired. Start over with /sticker."))
        user_sessions.pop(uid, None)
        return

    emoji = get_user_pack(uid)["emoji"]
    pack_index = get_next_pack_index(uid)

    proc = await reply_or_dm(client, message, small_caps("⚙️ Creating your new pack..."))

    success, result, telegram_pack_name = await create_unique_sticker_pack(client, uid, display_title, sticker_bytes, emoji)
    if success:
        create_pack_record(uid, pack_index, display_title, telegram_pack_name, emoji, pack_type="static")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌟 Open Sticker Pack", url=f"https://t.me/addstickers/{telegram_pack_name}")]
        ])
        await proc.edit_text(
            small_caps(f"✅ Pack Created Successfully!\n📦 Pack Index: {pack_index}\n📝 Name: {display_title}"),
            reply_markup=kb
        )
    else:
        await proc.edit_text(small_caps(f"❌ Failed to create pack: {result}"))

    user_sessions.pop(uid, None)

async def handle_pack_index(client: Client, message: Message, uid):
    raw = message.text.strip()
    if not raw.isdigit():
        await reply_or_dm(client, message, small_caps("❌ Invalid Pack Index.\nUse /mypacks to see your available pack indexes."))
        return

    pack_index = int(raw)
    pack = get_pack_by_index(uid, pack_index)
    if not pack:
        await reply_or_dm(client, message, small_caps("❌ Invalid Pack Index.\nUse /mypacks to see your available pack indexes."))
        return

    sticker_bytes = user_sessions.get(uid, {}).get("sticker_bytes")
    if not sticker_bytes:
        await reply_or_dm(client, message, small_caps("❌ Session expired. Start over with /sticker."))
        user_sessions.pop(uid, None)
        return

    proc = await reply_or_dm(client, message, small_caps("⚙️ Adding sticker to pack..."))

    success, err = await add_sticker_to_pack(client, uid, pack["telegram_pack_name"], sticker_bytes, pack["emoji"])
    if success:
        increment_pack_sticker_count(uid, pack_index)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🌟 Open Sticker Pack", url=f"https://t.me/addstickers/{pack['telegram_pack_name']}")]
        ])
        await proc.edit_text(
            small_caps(f"✅ Sticker Added Successfully!\n📦 Pack Index: {pack_index}\n📝 Pack Name: {pack['display_title']}"),
            reply_markup=kb
        )
    else:
        await proc.edit_text(small_caps(f"❌ Failed to add sticker: {err}"))

    user_sessions.pop(uid, None)

# ==============================================
# STEP HANDLERS FOR /clonepack
# ==============================================
async def handle_clonepack_link(client: Client, message: Message, uid):
    raw = message.text.strip()
    short_name = extract_pack_short_name(raw)
    if not short_name:
        await reply_or_dm(client, message, small_caps("❌ Invalid sticker pack link."))
        return

    try:
        sticker_set = await get_sticker_set(short_name)
    except Exception:
        await reply_or_dm(client, message, small_caps("❌ Unable to access this sticker pack."))
        return

    if not sticker_set.get("stickers"):
        await reply_or_dm(client, message, small_caps("❌ Unable to access this sticker pack."))
        return

    user_sessions[uid]["source_pack"] = sticker_set
    user_sessions[uid]["step"] = "clonepack_waiting_name"
    await reply_or_dm(
        client, message,
        small_caps("✏️ Enter a name for your cloned sticker pack.\n\nExample:\nNaruto Collection")
    )

async def handle_clonepack_name(client: Client, message: Message, uid):
    display_title = message.text.strip()
    if not display_title:
        await reply_or_dm(client, message, small_caps("❌ Please send a valid pack name."))
        return

    source_pack = user_sessions.get(uid, {}).get("source_pack")
    if not source_pack:
        await reply_or_dm(client, message, small_caps("❌ Session expired. Start over with /clonepack."))
        user_sessions.pop(uid, None)
        return

    # Clear session immediately so messages sent while cloning do not re-trigger another clone
    user_sessions.pop(uid, None)

    stickers = source_pack.get("stickers", [])
    if not stickers:
        await reply_or_dm(client, message, small_caps("❌ Source sticker pack is empty."))
        user_sessions.pop(uid, None)
        return

    default_emoji = get_user_pack(uid)["emoji"]
    pack_index = get_next_pack_index(uid)
    total = len(stickers)

    progress = await reply_or_dm(
        client, message,
        small_caps(f"⚡ Fast-Cloning Sticker Pack...\n\n0 / {total} copied...")
    )

    def _st_format(st):
        if st.get("is_video"):
            return "video"
        if st.get("is_animated"):
            return "animated"
        return "static"

    video_count = sum(1 for st in stickers if st.get("is_video"))
    pack_type = "video" if video_count > 0 and video_count >= total / 2 else "static"
    src_sticker_type = source_pack.get("sticker_type")
    if src_sticker_type not in ("regular", "mask", "custom_emoji"):
        src_sticker_type = None

    input_stickers = [
        {
            "sticker": st["file_id"],
            "format": _st_format(st),
            "emoji_list": [st.get("emoji") or default_emoji],
        }
        for st in stickers
    ]
    first_emoji = input_stickers[0]["emoji_list"][0]

    copied = 0
    telegram_pack_name = None

    job = {
        "display_title": display_title,
        "input_stickers": input_stickers,
        "pack_index": pack_index,
        "pack_type": pack_type,
        "src_sticker_type": src_sticker_type,
        "first_emoji": first_emoji,
        "telegram_pack_name": None,
        "copied": 0,
        "total": total,
        "failed_items": [],
    }
    await _run_clone_job(uid, progress, job)

clone_retry_cache = {}

async def _run_clone_job(uid: int, progress: Message, job: dict):
    display_title = job["display_title"]
    input_stickers = job["input_stickers"]
    pack_index = job["pack_index"]
    pack_type = job["pack_type"]
    src_sticker_type = job["src_sticker_type"]
    first_emoji = job["first_emoji"]
    telegram_pack_name = job.get("telegram_pack_name")
    copied = job.get("copied", 0)
    total = job["total"]
    failed_items = []

    try:
        async with aiohttp.ClientSession() as http_session:
            if not telegram_pack_name:
                # Fresh pack creation
                batch_size = 10 if pack_type == "video" else 50
                first_batch = input_stickers[:batch_size]
                success, result, telegram_pack_name = await create_unique_sticker_pack_from_input(
                    uid, display_title, first_batch, session=http_session, sticker_type=src_sticker_type
                )

                if success:
                    copied = len(first_batch)
                    items_to_add = input_stickers[len(first_batch):]
                else:
                    print(f"[CLONEPACK] Batch-{batch_size} failed ({result}), trying single sticker...")
                    try:
                        await progress.edit_text(small_caps(f"⚙️ Batch skipped, creating pack 1-by-1...\n\n0 / {total} copied..."))
                    except Exception:
                        pass
                    created_single = False
                    last_err = result or "Timeout"
                    first_ok_idx = 0
                    for probe_idx, probe_item in enumerate(input_stickers[:3]):
                        ok_s, res_s, cand_name = await create_unique_sticker_pack_from_input(
                            uid, display_title, [probe_item], session=http_session, sticker_type=src_sticker_type
                        )
                        if ok_s:
                            created_single = True
                            telegram_pack_name = cand_name
                            first_ok_idx = probe_idx
                            copied = 1
                            failed_items.extend(input_stickers[:probe_idx])
                            break
                        last_err = res_s or "Timeout"

                    if not created_single:
                        job["telegram_pack_name"] = None
                        clone_retry_cache[uid] = job
                        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Retry Cloning", callback_data="clone_retry")]])
                        await progress.edit_text(
                            small_caps(f"❌ Unable to create the cloned sticker pack:\n{last_err}\n\nTap Retry below after a few seconds."),
                            reply_markup=kb,
                        )
                        return
                    items_to_add = input_stickers[first_ok_idx + 1:]

                job["telegram_pack_name"] = telegram_pack_name
                job["copied"] = copied
                create_pack_record(uid, pack_index, display_title, telegram_pack_name, first_emoji, pack_type=pack_type)
            else:
                # Resuming failed items into existing pack
                items_to_add = job.get("failed_items", [])

            if items_to_add:
                try:
                    await progress.edit_text(small_caps(f"⚡ Fast-Cloning Sticker Pack...\n\n{copied} / {total} copied..."))
                except Exception:
                    pass

                sem = asyncio.Semaphore(3)

                async def _add_one(item):
                    async with sem:
                        return await add_sticker_input_to_pack(uid, telegram_pack_name, item, session=http_session)

                for chunk_start in range(0, len(items_to_add), 15):
                    chunk = items_to_add[chunk_start:chunk_start + 15]
                    results = await asyncio.gather(*[_add_one(it) for it in chunk], return_exceptions=True)
                    for item, res in zip(chunk, results):
                        if isinstance(res, tuple) and res[0]:
                            copied += 1
                        else:
                            failed_items.append(item)
                    job["copied"] = copied
                    try:
                        await progress.edit_text(small_caps(f"⚡ Fast-Cloning Sticker Pack...\n\n{copied} / {total} copied..."))
                    except Exception:
                        pass
    except Exception as e:
        print(f"[CLONEPACK] Unexpected error: {type(e).__name__}: {e}")
        job["copied"] = copied
        job["failed_items"] = failed_items
        clone_retry_cache[uid] = job
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Retry Cloning", callback_data="clone_retry")]])
        try:
            await progress.edit_text(
                small_caps(f"❌ Cloning error: {str(e) or type(e).__name__}\n\nTap Retry below."),
                reply_markup=kb,
            )
        except Exception:
            pass
        return

    packs_collection.update_one(
        {"user_id": str(uid), "pack_index": pack_index},
        {"$set": {"total_stickers": copied}}
    )

    buttons = [[InlineKeyboardButton("🌟 Open Sticker Pack", url=f"https://t.me/addstickers/{telegram_pack_name}")]]
    if failed_items:
        job["failed_items"] = failed_items
        clone_retry_cache[uid] = job
        buttons.append([InlineKeyboardButton(f"🔄 Retry {len(failed_items)} Failed", callback_data="clone_retry")])
    else:
        clone_retry_cache.pop(uid, None)

    await progress.edit_text(
        small_caps(
            f"✅ Sticker Pack Cloned Successfully!\n"
            f"📦 Pack Index: {pack_index}\n"
            f"📝 Name: {display_title}\n"
            f"📊 Stickers Copied: {copied} / {total}\n"
            f"❌ Failed: {len(failed_items)}"
        ),
        reply_markup=InlineKeyboardMarkup(buttons),
    )

@app.on_callback_query(filters.regex(r"^clone_retry$"))
async def clone_retry_cb(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    job = clone_retry_cache.get(uid)
    if not job:
        await callback.answer("Retry session expired. Start again with /clonepack.", show_alert=True)
        return
    await callback.answer("Retrying...")
    await callback.message.edit_text(small_caps(f"🔄 Retrying Clone...\n\n{job.get('copied', 0)} / {job['total']} copied..."))
    await _run_clone_job(uid, callback.message, job)

# ==============================================
# STEP HANDLERS FOR /renamepack
# ==============================================
async def handle_renamepack_index(client: Client, message: Message, uid):
    raw = message.text.strip()
    if not raw.isdigit():
        await reply_or_dm(client, message, small_caps("❌ Invalid Pack Index.\nUse /mypacks to see your available pack indexes."))
        return

    pack_index = int(raw)
    pack = get_pack_by_index(uid, pack_index)
    if not pack:
        await reply_or_dm(client, message, small_caps("❌ Invalid Pack Index.\nUse /mypacks to see your available pack indexes."))
        return

    user_sessions[uid]["rename_pack_index"] = pack_index
    user_sessions[uid]["step"] = "renamepack_waiting_name"
    await reply_or_dm(client, message, small_caps("✏️ Enter the new display name.\n\nExample:\nAnime Collection"))

async def handle_renamepack_name(client: Client, message: Message, uid):
    new_title = message.text.strip()
    if not new_title:
        await reply_or_dm(client, message, small_caps("❌ Please send a valid name."))
        return

    pack_index = user_sessions.get(uid, {}).get("rename_pack_index")
    pack = get_pack_by_index(uid, pack_index) if pack_index is not None else None
    if not pack:
        await reply_or_dm(client, message, small_caps("❌ Session expired. Start over with /renamepack."))
        user_sessions.pop(uid, None)
        return

    try:
        await set_sticker_set_title(pack["telegram_pack_name"], new_title)
        update_pack_display_title(uid, pack_index, new_title)
        await reply_or_dm(
            client, message,
            small_caps(f"✅ Pack renamed!\n📦 Pack Index: {pack_index}\n📝 New Name: {new_title}")
        )
    except Exception:
        update_pack_display_title(uid, pack_index, new_title)
        await reply_or_dm(
            client, message,
            small_caps(
                f"✅ Display name updated!\n📦 Pack Index: {pack_index}\n📝 New Name: {new_title}\n\n"
                f"ℹ️ Telegram does not allow renaming a sticker pack's link, so only the name shown in /mypacks was updated."
            )
        )

    user_sessions.pop(uid, None)

# ==============================================
# STEP HANDLER FOR /delpack
# ==============================================
async def handle_delpack_index(client: Client, message: Message, uid):
    raw = message.text.strip()
    if not raw.isdigit():
        await reply_or_dm(client, message, small_caps("❌ Invalid Pack Index.\nUse /mypacks to see your available pack indexes."))
        return

    pack_index = int(raw)
    pack = get_pack_by_index(uid, pack_index)
    if not pack:
        await reply_or_dm(client, message, small_caps("❌ Invalid Pack Index.\nUse /mypacks to see your available pack indexes."))
        return

    user_sessions[uid]["delete_pack_index"] = pack_index
    user_sessions[uid]["step"] = "delpack_confirming"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Delete", callback_data="delpack_confirm"),
         InlineKeyboardButton("❌ Cancel", callback_data="delpack_cancel")]
    ])
    await reply_or_dm(
        client, message,
        small_caps(f"⚠️ Delete Pack Index {pack_index} ({pack['display_title']}) from the database?"),
        reply_markup=kb
    )

# ==============================================
# HANDLE TEXT (Routing for /sticker, /plain, /vid, and utilities)
# ==============================================
@app.on_message(
    filters.text & (filters.private | filters.group) &
    ~filters.command(["sticker", "setemoji", "mypacks", "stats", "users", "reset", "start", "addchannel", "addch", "removechannel", "delchannel", "rmchannel", "remchannel", "remove", "channels", "listch", "listchannel", "listchannels", "mychats", "getid", "fsubdebug", "clonepack", "renamepack", "delpack", "q", "quote", "r", "kang", "pr", "premium", "done", "grid", "split", "toemoji", "textemoji", "setlog", "setlogger", "addsudo", "rmsudo", "delsudo", "removesudo", "sudolist", "admins", "fbc", "bc", "ping", "btstats", "restart", "help", "admin", "adminhelp", "create", "cancel"]),
    group=6
)
async def handle_text(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id

    if uid not in user_sessions:
        return

    step = user_sessions[uid].get("step")

    if step == "waiting_text":
        text = message.text.strip()
        if not text:
            await reply_or_dm(client, message, small_caps("❌ Please send some text."))
            return

        user_sessions[uid]["text"] = text
        user_sessions[uid]["step"] = "waiting_size"

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔹 Sᴍᴀʟʟ", callback_data="size_small"),
             InlineKeyboardButton("🔸 Mᴇᴅɪᴜᴍ", callback_data="size_medium")],
            [InlineKeyboardButton("🔹 Lᴀʀɢᴇ", callback_data="size_large"),
             InlineKeyboardButton("🔸 Exᴛʀᴀ Lᴀʀɢᴇ", callback_data="size_xlarge")]
        ])
        await reply_or_dm(client, message, small_caps("🔧 **Step 3/4:** Choose text size:"), reply_markup=kb)
        return

    if step == "waiting_new_pack_name":
        await handle_new_pack_name(client, message, uid)
        return

    if step == "waiting_pack_index":
        await handle_pack_index(client, message, uid)
        return

    if step == "clonepack_waiting_link":
        await handle_clonepack_link(client, message, uid)
        return

    if step == "clonepack_waiting_name":
        await handle_clonepack_name(client, message, uid)
        return

    if step == "renamepack_waiting_index":
        await handle_renamepack_index(client, message, uid)
        return

    if step == "renamepack_waiting_name":
        await handle_renamepack_name(client, message, uid)
        return

    if step == "delpack_waiting_index":
        await handle_delpack_index(client, message, uid)
        return

# /plain and /vid step handlers are defined in handlers/plain.py and handlers/vid.py

# ==============================================
# GLOBAL CANCEL CALLBACK HANDLER
# ==============================================
@app.on_callback_query(filters.regex(r"^global_"))
async def global_menu_callbacks(client: Client, callback: CallbackQuery):
    action = callback.data.replace("global_", "")
    await callback.answer()
    if action == "main_menu":
        await callback.message.edit_text(
            small_caps("👋 Welcome to the Main Menu!\nUse /help to see all available commands.")
        )
    elif action == "start_sticker":
        uid = callback.from_user.id
        user_sessions[uid] = {"step": "waiting_image"}
        await callback.message.reply_text(small_caps("📸 **Step 1/4:** Send me an image (photo or document)."))

# ==============================================
# SIZE CALLBACK
# ==============================================
@app.on_callback_query(filters.regex(r"^size_"))
async def size_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in user_sessions or user_sessions[uid].get("step") != "waiting_size":
        await callback.answer(small_caps("Session expired. Start over with /sticker."), show_alert=True)
        return

    size_map = {
        "size_small": 0.7,
        "size_medium": 1.0,
        "size_large": 1.4,
        "size_xlarge": 1.8
    }
    multiplier = size_map.get(callback.data, 1.0)
    user_sessions[uid]["size"] = multiplier
    user_sessions[uid]["step"] = "waiting_position"

    positions = [
        ("Tᴏᴘ Lᴇꜰᴛ", "top-left"), ("Tᴏᴘ Mɪᴅ", "top-mid"), ("Tᴏᴘ Rɪɢʜᴛ", "top-right"),
        ("Mɪᴅ Lᴇꜰᴛ", "mid-left"), ("Cᴇɴᴛᴇʀ", "center"), ("Mɪᴅ Rɪɢʜᴛ", "mid-right"),
        ("Bᴏᴛ Lᴇꜰᴛ", "bot-left"), ("Bᴏᴛ Mɪᴅ", "bot-mid"), ("Bᴏᴛ Rɪɢʜᴛ", "bot-right")
    ]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"pos_{data}") for label, data in positions[:3]],
        [InlineKeyboardButton(label, callback_data=f"pos_{data}") for label, data in positions[3:6]],
        [InlineKeyboardButton(label, callback_data=f"pos_{data}") for label, data in positions[6:]]
    ])
    await callback.message.edit_text(small_caps("📍 **Step 4/4:** Choose text position:"), reply_markup=kb)
    await callback.answer()

async def send_edit_again_preview(client: Client, chat_id: int, from_user, sticker_bytes, is_group: bool):
    caption = small_caps(
        "📝 Text added successfully.\n"
        "If you want to add more text (for example, text at the top and bottom), press Edit Again.\n"
        "Otherwise press Skip to continue."
    )
    if is_group:
        mention = f"[{from_user.first_name}](tg://user?id={from_user.id})"
        caption = f"{mention}\n\n{caption}"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Edit Again", callback_data="editagain_edit"),
         InlineKeyboardButton("✅ Skip", callback_data="editagain_skip")]
    ])

    photo_file = BytesIO(sticker_bytes)
    photo_file.name = "preview.png"
    await client.send_photo(
        chat_id=chat_id,
        photo=photo_file,
        caption=caption,
        reply_markup=kb
    )

@app.on_callback_query(filters.regex(r"^pos_"))
async def position_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in user_sessions or user_sessions[uid].get("step") != "waiting_position":
        await callback.answer(small_caps("Session expired. Start over with /sticker."), show_alert=True)
        return

    position = callback.data.replace("pos_", "")
    user_sessions[uid]["position"] = position

    await callback.answer(small_caps("⚙️ Rendering sticker..."))

    img_bytes = user_sessions[uid]["image_bytes"]
    text = user_sessions[uid]["text"]
    size_mult = user_sessions[uid]["size"]

    try:
        sticker_bytes = create_sticker(img_bytes, text, position, size_mult)
        user_sessions[uid]["sticker_bytes"] = sticker_bytes
        user_sessions[uid]["step"] = "preview_edit_again"

        try:
            await callback.message.delete()
        except Exception:
            pass

        await send_edit_again_preview(
            client,
            callback.message.chat.id,
            callback.from_user,
            sticker_bytes,
            callback.message.chat.type != "private"
        )
    except Exception as e:
        await callback.message.reply_text(small_caps(f"❌ Error: {str(e)}"))
        user_sessions.pop(uid, None)

@app.on_callback_query(filters.regex(r"^editagain_"))
async def edit_again_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in user_sessions or user_sessions[uid].get("step") != "preview_edit_again":
        await callback.answer(small_caps("Session expired. Start over with /sticker."), show_alert=True)
        return

    choice = callback.data.replace("editagain_", "")
    await callback.answer()

    if choice == "edit":
        user_sessions[uid]["image_bytes"] = user_sessions[uid]["sticker_bytes"]
        user_sessions[uid]["step"] = "waiting_text"
        await callback.message.reply_text(
            small_caps("✏️ Send me the text you want to add on the sticker.")
        )
    else:
        user_sessions[uid]["step"] = "choose_destination"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🆕 Create New Pack", callback_data="dest_new")],
            [InlineKeyboardButton("📂 Existing Pack", callback_data="dest_existing")]
        ])
        await callback.message.reply_text(
            small_caps("📦 Where do you want to save this sticker?"),
            reply_markup=kb
        )

@app.on_callback_query(filters.regex(r"^dest_"))
async def destination_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in user_sessions or user_sessions[uid].get("step") != "choose_destination":
        await callback.answer(small_caps("Session expired. Start over with /sticker."), show_alert=True)
        return

    choice = callback.data.replace("dest_", "")
    await callback.answer()

    if choice == "new":
        user_sessions[uid]["step"] = "waiting_new_pack_name"
        await callback.message.edit_text(
            small_caps("✏️ Enter the display name for your new sticker pack.\n\nExample:\nAnime Pack")
        )
    else:
        user_sessions[uid]["step"] = "waiting_pack_index"
        await callback.message.edit_text(
            small_caps("📂 Send the Pack Index where you want to save this sticker.\n\nExample:\n1")
        )

@app.on_callback_query(filters.regex(r"^delpack_"))
async def delpack_callback(client: Client, callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in user_sessions or user_sessions[uid].get("step") != "delpack_confirming":
        await callback.answer(small_caps("Session expired. Start over with /delpack."), show_alert=True)
        return

    choice = callback.data.replace("delpack_", "")
    pack_index = user_sessions[uid].get("delete_pack_index")
    await callback.answer()

    if choice == "cancel":
        await cancel_user_session(client, user_id=uid, callback=callback)
        return

    delete_pack_record(uid, pack_index)
    await callback.message.edit_text(
        small_caps(
            f"✅ Pack Index {pack_index} deleted from the database.\n\n"
            f"ℹ️ Telegram's Bot API does not support deleting sticker sets, so the pack itself still exists on Telegram — it's just no longer tracked here, and its index will never be reused."
        )
    )
    user_sessions.pop(uid, None)

# ==============================================
# /setemoji
# ==============================================
@app.on_message(filters.command("setemoji") & (filters.private | filters.group))
async def set_emoji_cmd(client: Client, message: Message):
    uid = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await reply_or_dm(client, message, small_caps("❌ Usage: /setemoji 😀"))
        return
    emoji = args[1].strip()
    if not emoji:
        await reply_or_dm(client, message, small_caps("❌ Please send a valid emoji."))
        return

    get_user_data(uid)
    update_user_data(uid, {"emoji": emoji})
    await reply_or_dm(client, message, small_caps(f"✅ Default emoji set to {emoji}"))
register_user_command("setemoji", "Choose the default emoji for new stickers.", category=CATEGORY_STICKER)

# ==============================================
# /mypacks (10 per page in expandable blockquote + Previous / Next / Close)
# ==============================================
MYPACKS_PAGE_SIZE = 10

def _build_mypacks_page(uid: int, packs: list, page: int = 0):
    total = len(packs)
    total_pages = max(1, (total + MYPACKS_PAGE_SIZE - 1) // MYPACKS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    start = page * MYPACKS_PAGE_SIZE
    end = start + MYPACKS_PAGE_SIZE
    chunk = packs[start:end]

    blocks = []
    for pack in chunk:
        idx = pack["pack_index"]
        title = html.escape(small_caps(f"[{idx}] {pack['display_title']}"))
        telegram_name = pack.get("telegram_pack_name")
        if telegram_name:
            link_line = f"🔗 https://t.me/addstickers/{telegram_name}\n"
        else:
            link_line = html.escape(small_caps("🔗 No Telegram Pack Yet")) + "\n"
        if pack.get("preview_video_url"):
            preview_line = html.escape(small_caps("🎬 Preview Available"))
        else:
            preview_line = html.escape(small_caps("❌ No Preview"))
        blocks.append(
            title + "\n"
            + link_line
            + html.escape(small_caps(f"🎨 Stickers: {pack.get('total_stickers', 0)}")) + "\n"
            + preview_line
        )

    header = html.escape(small_caps(f"📦 Your Sticker Packs (Page {page + 1}/{total_pages})"))
    body = "<blockquote expandable>\n" + "\n\n".join(blocks) + "\n</blockquote>"
    text = f"{header}\n\n{body}"

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"mypacks_page|{uid}|{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="mypacks_noop"))
    if page + 1 < total_pages:
        nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"mypacks_page|{uid}|{page + 1}"))

    kb = InlineKeyboardMarkup([
        nav_row,
        [InlineKeyboardButton("❌ Close", callback_data=f"mypacks_close|{uid}")],
    ])
    return text, kb

@app.on_message(filters.command("mypacks") & (filters.private | filters.group))
async def mypacks_cmd(client: Client, message: Message):
    if not await fsub.check(client, message):
        return
    uid = message.from_user.id
    packs = get_user_packs(uid)

    if not packs:
        await reply_or_dm(client, message, small_caps("You have no sticker packs yet. Create one with /sticker or /plain."))
        return

    text, kb = _build_mypacks_page(uid, packs, page=0)
    await message.reply_text(
        text,
        reply_markup=kb,
        parse_mode=HTML_PARSE_MODE,
        disable_web_page_preview=True,
    )

@app.on_callback_query(filters.regex(r"^mypacks_page\|(\d+)\|(\d+)$"))
async def mypacks_page_cb(client: Client, callback: CallbackQuery):
    owner_uid = int(callback.matches[0].group(1))
    page = int(callback.matches[0].group(2))
    if callback.from_user.id != owner_uid:
        await callback.answer("This menu belongs to someone else!", show_alert=True)
        return

    packs = get_user_packs(owner_uid)
    if not packs:
        await callback.answer("No packs found.", show_alert=True)
        return

    text, kb = _build_mypacks_page(owner_uid, packs, page=page)
    await callback.answer()
    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb,
            parse_mode=HTML_PARSE_MODE,
            disable_web_page_preview=True,
        )
    except Exception:
        pass

@app.on_callback_query(filters.regex(r"^mypacks_close\|(\d+)$"))
async def mypacks_close_cb(client: Client, callback: CallbackQuery):
    owner_uid = int(callback.matches[0].group(1))
    if callback.from_user.id != owner_uid:
        await callback.answer("This menu belongs to someone else!", show_alert=True)
        return
    await callback.answer("Closed")
    try:
        await callback.message.delete()
    except Exception:
        try:
            await callback.message.edit_text("❌ Closed.")
        except Exception:
            pass

@app.on_callback_query(filters.regex(r"^mypacks_noop$"))
async def mypacks_noop_cb(client: Client, callback: CallbackQuery):
    await callback.answer()

register_user_command("mypacks", "View and manage your sticker packs.", category=CATEGORY_STICKER)

# ==============================================
# /stats (admin only)
# ==============================================
@app.on_message(filters.command("stats") & admin_filter)
async def stats_cmd(client: Client, message: Message):
    stats = get_stats()

    total_packs = packs_collection.count_documents({})

    top_user = users_collection.find_one(
        sort=[("total_stickers", -1)]
    )

    if top_user:
        try:
            tg = await client.get_users(int(top_user["user_id"]))
            top_name = tg.first_name
            top_username = (
                f"@{tg.username}" if tg.username else "No Username"
            )
        except:
            top_name = "Unknown"
            top_username = "Unknown"

        top_stickers = top_user.get("total_stickers", 0)
    else:
        top_name = "-"
        top_username = "-"
        top_stickers = 0

    text = (
        "📊 **BOT STATISTICS**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👥 **Users**\n"
        f"├ Total Users : **{stats['total_users']}**\n"
        f"└ Active Database : **{stats['total_users']}**\n\n"
        "🎨 **Sticker Data**\n"
        f"├ Total Stickers : **{stats['total_stickers']}**\n"
        f"└ Total Packs : **{total_packs}**\n\n"
        "🏆 **Top Sticker Creator**\n"
        f"├ Name : **{top_name}**\n"
        f"├ Username : {top_username}\n"
        f"└ Stickers : **{top_stickers}**\n\n"
        "🤖 **Bot Status**\n"
        "└ 🟢 Online"
    )

    await message.reply_text(
        text,
        disable_web_page_preview=True
    )
register_admin_command("stats", "View bot statistics.")

# ==============================================
# /users (admin only)
# ==============================================
@app.on_message(filters.command("users") & admin_filter)
async def users_cmd(client: Client, message: Message):
    users = sorted(
        get_all_users(),
        key=lambda x: x.get("total_stickers", 0),
        reverse=True
    )

    if not users:
        await message.reply_text("❌ No users found.")
        return

    text = (
        "🏆 **TOP 10 STICKER MAKERS**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 **Total Users:** {len(users)}\n\n"
    )

    medals = ["🥇", "🥈", "🥉"]

    for i, user in enumerate(users[:10], start=1):
        uid = int(user["user_id"])

        try:
            tg = await client.get_users(uid)
            name = tg.first_name or "Unknown"
            username = f"@{tg.username}" if tg.username else "No Username"
        except:
            name = "Unknown User"
            username = "Unavailable"

        stickers = user.get("total_stickers", 0)
        packs = packs_collection.count_documents(
            {"user_id": str(uid)}
        )

        rank = medals[i-1] if i <= 3 else f"#{i}"

        text += (
            f"{rank} **{name}**\n"
            f"├ 👤 {username}\n"
            f"├ 🆔 `{uid}`\n"
            f"├ 📦 Packs : {packs}\n"
            f"└ 🎨 Stickers : **{stickers}**\n\n"
        )

    await message.reply_text(
        text,
        disable_web_page_preview=True
    )
register_admin_command("users", "View top sticker makers and all bot users.")

# ==============================================
# /reset
# ==============================================
@app.on_message(filters.command("reset") & filters.private)
async def reset_cmd(client: Client, message: Message):
    uid = message.from_user.id
    args = message.text.split(maxsplit=1)
    confirmed = len(args) > 1 and args[1].strip().lower() == "confirm"

    if not confirmed:
        await message.reply_text(
            small_caps("⚠️ **WARNING:** This will delete your sticker pack data from the database.\n"
                       "Your Telegram sticker pack will still exist but won't be linked to the bot.\n\n"
                       "To confirm, send: `/reset confirm`")
        )
        return

    users_collection.delete_one({"user_id": str(uid)})
    await message.reply_text(small_caps("✅ Your data has been reset. You can start fresh with /sticker."))
register_user_command("reset", "Reset your sticker pack data.", category=CATEGORY_STICKER)

# ==============================================
# FORCE-SUBSCRIBE ADMIN COMMANDS
# Handlers (/addchannel, /removechannel, /listch, /mychats, /getid, /fsubdebug)
# are managed and registered via ForceSubscribe in force_subscribe.py.
# ==============================================
register_admin_command("addchannel", "Add one or multiple channels to force-subscribe (alias: /addch).")
register_admin_command("removechannel", "Remove one or multiple channels (aliases: /delchannel, /remove).")
register_admin_command("listch", "List all required channels and bot admin status (alias: /channels).")
register_admin_command("mychats", "List all chats where the bot is currently an admin.")
register_admin_command("getid", "Get chat ID and ref of current channel/group.")
register_admin_command("fsubdebug", "Debug force-subscribe status for a user.")

# /plain callback handlers are registered in handlers/plain.py

# /vid callback handlers are registered in handlers/vid.py

# ==============================================
# /create WIZARD (ADMIN ONLY)
# ==============================================
CREATE_SESSION_TIMEOUT = 600

def _create_session_expired(session):
    return (time.time() - session.get("started_at", 0)) > CREATE_SESSION_TIMEOUT

def _build_addchannel_command(data):
    parts = ["/addchannel"]

    chat_id = data.get("chat_id")
    if chat_id:
        parts.append(chat_id)

    username = data.get("username")
    if username:
        parts.append(username)

    if data.get("request"):
        parts.append("--request")

    button_text = data.get("button_text")
    if button_text:
        parts.append(f'--button="{button_text}"')

    title = data.get("title")
    if title:
        parts.append(title)

    return " ".join(parts)

@app.on_message(filters.command("create") & filters.user(ADMIN_IDS) & (filters.private | filters.group))
async def create_wizard_start(client: Client, message: Message):
    uid = message.from_user.id
    create_sessions[uid] = {"step": "waiting_chatid", "started_at": time.time()}
    await reply_or_dm(
        client, message,
        small_caps("🆔 Send the Chat ID of the Channel or Group.\n\nExample:\n-1001234567890")
    )
register_admin_command("create", "Generate a ready-to-copy /addchannel command via a step-by-step wizard.")

@app.on_message(
    filters.text & (filters.private | filters.group) & ~filters.regex(r'^/'),
    group=7
)
async def create_wizard_text(client: Client, message: Message):
    if not message.from_user:
        return
    uid = message.from_user.id
    if uid not in create_sessions:
        return
    if uid not in ADMIN_IDS:
        create_sessions.pop(uid, None)
        return

    session = create_sessions[uid]
    if _create_session_expired(session):
        create_sessions.pop(uid, None)
        await reply_or_dm(client, message, small_caps("⌛ Your /create session timed out. Start again with /create."))
        return

    step = session.get("step")
    text = message.text.strip()

    if step == "waiting_chatid":
        if not text:
            await reply_or_dm(client, message, small_caps("❌ Please send a valid Chat ID."))
            return
        session["chat_id"] = text
        session["step"] = "waiting_username"
        session["started_at"] = time.time()
        await reply_or_dm(
            client, message,
            small_caps(
                "👤 Send the Username or Join Link.\n\n"
                "Examples:\n@MyChannel\nor\nhttps://t.me/MyChannel\n"
                "or\nhttps://t.me/+AbCdEf12345\n\n"
                "Reply with:\nskip\nif you don't want to provide one."
            )
        )
        return

    if step == "waiting_username":
        if text.lower() != "skip":
            session["username"] = text
        session["step"] = "waiting_title"
        session["started_at"] = time.time()
        await reply_or_dm(
            client, message,
            small_caps(
                "📛 Send a Custom Title.\n\nExample:\nAnime Updates\n\n"
                "Reply with:\nskip\nto use Telegram's title."
            )
        )
        return

    if step == "waiting_title":
        if text.lower() != "skip":
            session["title"] = text
        session["step"] = "waiting_request"
        session["started_at"] = time.time()
        await reply_or_dm(
            client, message,
            small_caps("❓ Require Join Request?\n\nReply with:\nyes\nor\nno")
        )
        return

    if step == "waiting_request":
        low = text.lower()
        if low not in ("yes", "no"):
            await reply_or_dm(client, message, small_caps("❌ Please reply with:\nyes\nor\nno"))
            return
        session["request"] = (low == "yes")
        session["step"] = "waiting_button"
        session["started_at"] = time.time()
        await reply_or_dm(
            client, message,
            small_caps(
                "🔘 Custom Join Button Text\n\nExample:\n🎬 Join Movies\n\n"
                "Reply with:\nskip\nto use the default button."
            )
        )
        return

    if step == "waiting_button":
        if text.lower() != "skip":
            session["button_text"] = text

        command = _build_addchannel_command(session)
        create_sessions.pop(uid, None)

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Copy Command", callback_data="createcmd_copy")]
        ])
        sent = await reply_or_dm(
            client, message,
            small_caps("✅ Command Generated Successfully\n\nCopy and run the command below:") + f"\n\n`{command}`",
            reply_markup=kb
        )
        generated_commands[sent.id] = command
        return

@app.on_callback_query(filters.regex(r"^createcmd_copy$"))
async def create_copy_callback(client: Client, callback: CallbackQuery):
    command = generated_commands.get(callback.message.id)
    if not command:
        await callback.answer(small_caps("Command no longer available."), show_alert=True)
        return
    await callback.answer()
    await callback.message.reply_text(f"`{command}`")

# ==============================================
# /help
# ==============================================
@app.on_message(filters.command("help") & (filters.private | filters.group))
async def help_cmd(client: Client, message: Message):
    lines = ["📋 <b>Help Menu</b>", ""]
    for category, commands in USER_COMMAND_CATEGORIES.items():
        lines.append(f"<b>{html.escape(category)}</b>")
        for command, description in commands:
            lines.append(f"• <code>/{html.escape(command)}</code> — {html.escape(description)}")
        lines.append("")

    text = "\n".join(lines).strip()
    await reply_or_dm(client, message, text, parse_mode=HTML_PARSE_MODE)
register_user_command("help", "View all available user commands.", category=CATEGORY_GENERAL)

# ==============================================
# /admin & /adminhelp (OWNER & SUDO ADMINS ONLY)
# ==============================================
@app.on_message(filters.command(["admin", "adminhelp"]) & (filters.private | filters.group))
async def adminhelp_cmd(client: Client, message: Message):
    uid = message.from_user.id
    if not is_admin(uid):
        await reply_or_dm(
            client, message,
            small_caps("🚫 Unauthorized. This command is for the Bot Owner and Sudo Admins only.")
        )
        return

    lines = ["🛠 <b>Admin Commands</b>", ""]
    for command, description in ADMIN_COMMANDS:
        lines.append(f"• <code>/{html.escape(command)}</code> — {html.escape(description)}")

    text = "\n".join(lines).strip()
    await reply_or_dm(client, message, text, parse_mode=HTML_PARSE_MODE)
register_admin_command("admin", "View all available admin commands (alias: /adminhelp).")

# ==============================================
# INITIALIZE AND RUN BOT
# ==============================================
def main():
    global BOT_USERNAME
    print(small_caps("🚀 Starting Sticker Bot..."))

    retry_delay = 5
    while True:
        try:
            with app:
                me = app.get_me()
                if not me or not me.username:
                    print("❌ Bot username is not set. Please set a username for your bot via @BotFather and restart.")
                    sys.exit(1)
                BOT_USERNAME = me.username.lower()
                if "stick" in sys.modules:
                    sys.modules["stick"].BOT_USERNAME = BOT_USERNAME

                print(small_caps(f"📊 Bot Username: @{BOT_USERNAME}"))
                print(small_caps(f"🤖 Bot Name: {me.first_name}"))
                print(small_caps(f"📁 Database: {DB_NAME}"))
                print(small_caps(f"👤 Admin ID(s): {', '.join(str(a) for a in ADMIN_IDS)}"))
                print(small_caps("✅ Bot is ready!"))

                users_collection.create_index("user_id", unique=True)
                users_collection.create_index("last_active")
                packs_collection.create_index([("user_id", 1), ("pack_index", 1)], unique=True)

                # Register /plain, /vid, /q (/r), /kang, /pr, logger, and admin/sudo system handlers
                plain_handler.register(app, fsub)
                vid_handler.register(app, fsub)
                quote_handler.register(app, fsub)
                kang_handler.register(app, fsub)
                admin_sys_handler.register(app, fsub)
                premium_handler.register(app, fsub)
                grid_handler.register(app, fsub)
                logger_handler.register(app, fsub)

                try:
                    app.loop.run_until_complete(send_log(
                        app,
                        db,
                        f"🟢 <b>Bot Started / Restarted!</b>\n"
                        f"├ 🤖 <b>Bot:</b> @{BOT_USERNAME}\n"
                        f"└ ⏰ <b>Time:</b> <code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>",
                    ))
                except Exception:
                    pass

                idle()
            break
        except Exception as e:
            if "RPC_CALL_FAIL" in str(e) or "500" in str(e) or "InternalServerError" in type(e).__name__:
                print(f"⚠️ Telegram server error (RPC_CALL_FAIL). Retrying in {int(retry_delay)}s...")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, 30)
            else:
                raise

if __name__ == "__main__":
    main()
