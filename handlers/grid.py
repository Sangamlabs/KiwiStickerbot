# -*- coding: utf-8 -*-
"""
handlers/grid.py
================
/grid (and shortcuts /split, /toemoji, /textemoji):
1. Photo Splitter (Big Emoji Grid): 3x3, 4x4, or 5x5 custom emoji pack + ready-to-copy grid preview.
2. Sticker Pack -> Custom Emoji Pack Converter: converts static, video, and animated sticker packs.
3. Text -> Custom Emoji Pack: turns any word/alphabet into a styled custom emoji pack.

Uses isolated `grid_sessions` in MongoDB and group=3 handlers with StopPropagation
so existing /pr, /sticker, /plain, /vid, /kang workflows are never affected.
"""
import asyncio
import html
import json
import os
import random
import string
import time
import uuid
from datetime import datetime
from io import BytesIO

import aiohttp
from PIL import Image, ImageDraw, ImageFont
from pyrogram import Client, filters, StopPropagation
from pyrogram.enums import MessageEntityType
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    MessageEntity,
)
from handlers.logger import send_log
from handlers.premium import _normalize_emoji_png, _normalize_emoji_webm, MAX_TGS_BYTES

GRID_SESSION_TIMEOUT = 1800
MAX_INPUT_BYTES = 20 * 1024 * 1024

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]


def _get_bold_font(size: int = 66):
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _rand4() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=4))


def _build_grid_pack_name(user_id: int, bot_username: str) -> str:
    uname = bot_username.lower().lstrip("@")
    suffix = f"_by_{uname}"
    core = f"g{user_id}_{_rand4()}"
    max_core = 64 - len(suffix)
    if len(core) > max_core:
        core = core[:max_core]
    return f"{core}{suffix}".lower()


def _split_image_grid(image_bytes: bytes, n: int) -> list[bytes]:
    """Center-crop image to square, resize to (n*100, n*100), and slice into n*n 100x100 PNGs."""
    img = Image.open(BytesIO(image_bytes)).convert("RGBA")
    side = min(img.width, img.height)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    cropped = img.crop((left, top, left + side, top + side))
    full_size = n * 100
    resized = cropped.resize((full_size, full_size), Image.Resampling.LANCZOS)

    tiles = []
    for row in range(n):
        for col in range(n):
            tile = resized.crop((col * 100, row * 100, (col + 1) * 100, (row * 100) + 100))
            buf = BytesIO()
            tile.save(buf, format="PNG")
            tiles.append(buf.getvalue())
    return tiles


_TEXT_STYLES = {
    "neon": {"bg": (18, 20, 36, 255), "fg": (0, 245, 212, 255), "stroke": (131, 56, 236, 255), "rounded": True},
    "gold": {"bg": (22, 22, 26, 255), "fg": (255, 215, 0, 255), "stroke": (0, 0, 0, 255), "rounded": True},
    "white": {"bg": (0, 0, 0, 0), "fg": (255, 255, 255, 255), "stroke": (0, 0, 0, 255), "rounded": False},
    "pink": {"bg": (255, 182, 217, 255), "fg": (255, 255, 255, 255), "stroke": (155, 45, 115, 255), "rounded": True},
}


def _render_char_tile(ch: str, style_key: str) -> bytes:
    """Render a single character onto a 100x100 transparent/rounded PNG tile."""
    st = _TEXT_STYLES.get(style_key, _TEXT_STYLES["neon"])
    canvas = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    d = ImageDraw.Draw(canvas)

    if st["rounded"] and st["bg"][3] > 0:
        d.rounded_rectangle((4, 4, 95, 95), radius=22, fill=st["bg"])

    label = ch if not ch.isspace() else " "
    font = _get_bold_font(64)
    if label.strip():
        bb = d.textbbox((0, 0), label, font=font, stroke_width=4)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        x = (100 - tw) // 2 - bb[0]
        y = (100 - th) // 2 - bb[1]
        d.text(
            (x, y),
            label,
            font=font,
            fill=st["fg"],
            stroke_width=4,
            stroke_fill=st["stroke"],
        )

    buf = BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def _build_custom_emoji_message(emoji_ids: list[str], cols: int = 0) -> tuple[str, list[MessageEntity]]:
    """Build text + MessageEntity list for custom_emoji preview (BMP '⭐' = 1 UTF-16 unit)."""
    entities = []
    chars = []
    offset = 0
    for idx, eid in enumerate(emoji_ids):
        chars.append("⭐")
        entities.append(
            MessageEntity(
                type=MessageEntityType.CUSTOM_EMOJI,
                offset=offset,
                length=1,
                custom_emoji_id=int(eid),
            )
        )
        offset += 1
        if cols > 0 and (idx + 1) % cols == 0 and idx + 1 < len(emoji_ids):
            chars.append("\n")
            offset += 1
    return "".join(chars), entities


def register(app, fsub):
    from stick import (
        db,
        BOT_TOKEN,
        TELEGRAM_BOT_API_BASE,
        TEMP_DIR,
        FFMPEG_PATH,
        reply_or_dm,
        _resolve_bot_username,
        _bot_api_upload,
        _bot_api_upload_multi,
        get_sticker_set,
        extract_pack_short_name,
        cleanup_temp_files,
        register_user_command,
        CATEGORY_STICKER,
    )

    async def _fast_download_bytes(file_id: str, http_session: aiohttp.ClientSession) -> bytes:
        """Download Telegram file directly via Bot API HTTPS (much faster than MTProto)."""
        async with http_session.get(f"{TELEGRAM_BOT_API_BASE}/getFile", params={"file_id": file_id}) as resp:
            meta = await resp.json()
        if not meta.get("ok"):
            raise Exception(meta.get("description", "getFile failed"))
        fpath = meta["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fpath}"
        async with http_session.get(file_url) as fresp:
            if fresp.status != 200:
                raise Exception(f"HTTP {fresp.status}")
            return await fresp.read()

    grid_sessions = db["grid_sessions"]
    premium_packs = db["premium_packs"]

    try:
        grid_sessions.create_index("user_id", unique=True)
    except Exception:
        pass

    def _get_session(uid: int):
        sess = grid_sessions.find_one({"user_id": str(uid)})
        if not sess:
            return None
        if (time.time() - sess.get("started_at", 0)) > GRID_SESSION_TIMEOUT:
            grid_sessions.delete_one({"user_id": str(uid)})
            return None
        return sess

    async def _create_pack_batch(client: Client, uid: int, title: str, items: list[tuple[bytes, str, str, str]], pack_type: str):
        """
        Upload up to 50 custom_emoji items in ONE createNewStickerSet call.
        items: list of (file_bytes, format, filename, content_type)
        """
        bot_username = _resolve_bot_username(client)
        if not bot_username:
            return False, "❌ Bot username load nahi ho paaya. Thodi der baad try karein.", None

        stickers_meta = []
        files_dict = {}
        for idx, (_b, fmt, fname, ctype) in enumerate(items[:50]):
            field = f"f_{idx}"
            stickers_meta.append({
                "sticker": f"attach://{field}",
                "format": fmt,
                "emoji_list": ["⭐"],
            })
            files_dict[field] = (_b, fname, ctype)

        stickers_json = json.dumps(stickers_meta)
        for _ in range(10):
            pack_name = _build_grid_pack_name(uid, bot_username)
            try:
                await _bot_api_upload_multi(
                    "createNewStickerSet",
                    {
                        "user_id": uid,
                        "name": pack_name,
                        "title": title[:64],
                        "sticker_type": "custom_emoji",
                        "stickers": stickers_json,
                    },
                    files_dict,
                )
                premium_packs.update_one(
                    {"user_id": str(uid), "pack_name": pack_name},
                    {"$set": {
                        "user_id": str(uid),
                        "pack_name": pack_name,
                        "title": title[:64],
                        "pack_type": pack_type,
                        "count": len(items[:50]),
                        "created_at": datetime.utcnow(),
                    }},
                    upsert=True,
                )
                asyncio.create_task(send_log(
                    client,
                    db,
                    f"🧩 <b>New Grid/Tool Emoji Pack Created</b>\n"
                    f"├ 👤 <b>User ID:</b> <code>{uid}</code>\n"
                    f"├ 📝 <b>Title:</b> {html.escape(title[:64])}\n"
                    f"├ 🎨 <b>Type:</b> <code>{pack_type}</code> ({len(items[:50])} emojis)\n"
                    f"└ 🔗 <b>Link:</b> https://t.me/addemoji/{pack_name}",
                ))
                return True, None, pack_name
            except Exception as e:
                err = str(e).lower()
                if "occupied" in err or "stickerset_invalid" in err or "already" in err:
                    continue
                if "peer_id_invalid" in err:
                    return False, "❌ Pehle bot ko DM me /start karein, phir try karein.", None
                print(f"[grid.py] createNewStickerSet error: {e}")
                return False, f"❌ Pack create karne me error aaya: {e}", None

        return False, "❌ Unique pack name generate nahi ho paaya. Dobara try karein.", None

    # ==============================================
    # COMMANDS: /grid, /split, /toemoji, /textemoji
    # ==============================================
    @app.on_message(filters.command("grid") & (filters.private | filters.group))
    async def grid_menu_cmd(client: Client, message: Message):
        if not await fsub.check(client, message):
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🧩 Photo Grid (3x3 / 4x4 / 5x5)", callback_data="grid_photo")],
            [InlineKeyboardButton("🔄 Sticker Pack → Emoji Pack", callback_data="grid_convert")],
            [InlineKeyboardButton("🔤 Text → Emoji Pack", callback_data="grid_text")],
            [InlineKeyboardButton("❌ Cancel", callback_data="grid_cancel")],
        ])
        await reply_or_dm(
            client,
            message,
            "🧩 **Grid & Smart Emoji Tools**\n\n"
            "• **Photo Grid:** Ek photo ko `3x3`, `4x4`, ya `5x5` Big Emoji pack me badlein\n"
            "• **Pack Converter:** Kisi bhi Sticker Pack link ko Custom Emoji Pack banayein\n"
            "• **Text → Emoji:** Apne naam/word ke stylish letter emojis banayein\n\n"
            "👇 Ek option chunein:",
            reply_markup=kb,
        )

    @app.on_message(filters.command("split") & (filters.private | filters.group))
    async def split_shortcut_cmd(client: Client, message: Message):
        if not await fsub.check(client, message):
            return
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("3x3 (9)", callback_data="grid_size|3"),
                InlineKeyboardButton("4x4 (16)", callback_data="grid_size|4"),
                InlineKeyboardButton("5x5 (25)", callback_data="grid_size|5"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="grid_cancel")],
        ])
        await reply_or_dm(client, message, "🧩 **Photo Grid Size Chunein:**", reply_markup=kb)

    @app.on_message(filters.command("toemoji") & (filters.private | filters.group))
    async def toemoji_shortcut_cmd(client: Client, message: Message):
        if not await fsub.check(client, message):
            return
        uid = message.from_user.id
        grid_sessions.update_one(
            {"user_id": str(uid)},
            {"$set": {"user_id": str(uid), "step": "waiting_pack_link", "started_at": time.time()}},
            upsert=True,
        )
        await reply_or_dm(
            client,
            message,
            "🔄 Jis **Sticker Pack** ko Custom Emoji Pack me convert karna hai, uska link bhejein:\n\n"
            "Example: `https://t.me/addstickers/Animals`",
        )

    @app.on_message(filters.command("textemoji") & (filters.private | filters.group))
    async def textemoji_shortcut_cmd(client: Client, message: Message):
        if not await fsub.check(client, message):
            return
        uid = message.from_user.id
        grid_sessions.update_one(
            {"user_id": str(uid)},
            {"$set": {"user_id": str(uid), "step": "waiting_text_word", "started_at": time.time()}},
            upsert=True,
        )
        await reply_or_dm(
            client,
            message,
            "🔤 Apna **naam, word, ya alphabets** bhejein (max 50 characters):\n\n"
            "Example: `KIWI` ya `ABCDEFGHIJKLMNOPQRSTUVWXYZ`",
        )

    # ==============================================
    # CALLBACKS
    # ==============================================
    @app.on_callback_query(filters.regex(r"^grid_cancel$"))
    async def grid_cancel_cb(client: Client, callback: CallbackQuery):
        grid_sessions.delete_one({"user_id": str(callback.from_user.id)})
        await callback.answer("Cancelled")
        await callback.message.edit_text("❌ Grid tool cancelled.")

    @app.on_callback_query(filters.regex(r"^grid_photo$"))
    async def grid_photo_cb(client: Client, callback: CallbackQuery):
        await callback.answer()
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("3x3 (9)", callback_data="grid_size|3"),
                InlineKeyboardButton("4x4 (16)", callback_data="grid_size|4"),
                InlineKeyboardButton("5x5 (25)", callback_data="grid_size|5"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="grid_cancel")],
        ])
        await callback.message.edit_text("🧩 **Photo Grid Size Chunein:**", reply_markup=kb)

    @app.on_callback_query(filters.regex(r"^grid_size\|([345])$"))
    async def grid_size_cb(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        n = int(callback.matches[0].group(1))
        grid_sessions.update_one(
            {"user_id": str(uid)},
            {"$set": {"user_id": str(uid), "step": "waiting_grid_photo", "grid_n": n, "started_at": time.time()}},
            upsert=True,
        )
        await callback.answer()
        await callback.message.edit_text(
            f"📸 **{n}x{n} ({n*n} Emojis)** selected!\n\n"
            f"Ab apni **Photo / Image / Static Sticker** bhejein. Bot usse {n}x{n} Big Emoji Pack me badal dega!"
        )

    @app.on_callback_query(filters.regex(r"^grid_convert$"))
    async def grid_convert_cb(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        grid_sessions.update_one(
            {"user_id": str(uid)},
            {"$set": {"user_id": str(uid), "step": "waiting_pack_link", "started_at": time.time()}},
            upsert=True,
        )
        await callback.answer()
        await callback.message.edit_text(
            "🔄 Jis **Sticker Pack** ko Custom Emoji Pack me convert karna hai, uska link bhejein:\n\n"
            "Example: `https://t.me/addstickers/Animals`"
        )

    @app.on_callback_query(filters.regex(r"^grid_text$"))
    async def grid_text_cb(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        grid_sessions.update_one(
            {"user_id": str(uid)},
            {"$set": {"user_id": str(uid), "step": "waiting_text_word", "started_at": time.time()}},
            upsert=True,
        )
        await callback.answer()
        await callback.message.edit_text(
            "🔤 Apna **naam, word, ya alphabets** bhejein (max 50 characters):\n\n"
            "Example: `KIWI` ya `ABCDEFGHIJKLMNOPQRSTUVWXYZ`"
        )

    @app.on_callback_query(filters.regex(r"^grid_tstyle\|(neon|gold|white|pink)$"))
    async def grid_text_style_cb(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        style_key = callback.matches[0].group(1)
        sess = _get_session(uid)
        if not sess or sess.get("step") != "waiting_text_style" or not sess.get("word"):
            await callback.answer("Session expire ho gaya hai.", show_alert=True)
            return

        word = sess["word"]
        grid_sessions.delete_one({"user_id": str(uid)})
        await callback.answer("Creating pack...")
        await callback.message.edit_text(f"⚙️ **{len(word)}** letter emojis render aur upload ho rahe hain...")

        tiles = await asyncio.to_thread(lambda: [_render_char_tile(ch, style_key) for ch in word])
        items = [(t, "static", f"ch_{i}.png", "image/png") for i, t in enumerate(tiles)]
        title = f"{word[:24]} Text Emojis"

        ok, err, pack_name = await _create_pack_batch(client, uid, title, items, pack_type="text_emoji")
        if not ok:
            await callback.message.edit_text(err)
            return

        link = f"https://t.me/addemoji/{pack_name}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🌟 Emoji Pack Kholen", url=link)]])
        await callback.message.edit_text(
            f"✅ **Text Emoji Pack Taiyaar Hai!**\n\n"
            f"🔤 **Text:** `{word}`\n"
            f"🎨 **Style:** `{style_key}`\n"
            f"🔗 **Link:** {link}",
            reply_markup=kb,
        )

        # Send live custom_emoji preview
        try:
            st_set = await get_sticker_set(pack_name)
            eids = [s["custom_emoji_id"] for s in st_set.get("stickers", []) if s.get("custom_emoji_id")]
            if eids:
                prev_text, prev_entities = _build_custom_emoji_message(eids, cols=0)
                await callback.message.reply_text(prev_text, entities=prev_entities)
        except Exception:
            pass

    # ==============================================
    # GROUP=3 HANDLERS (Only active `grid_sessions`)
    # ==============================================
    @app.on_message(
        filters.text & (filters.private | filters.group) & ~filters.regex(r"^/"),
        group=3,
    )
    async def grid_text_handler(client: Client, message: Message):
        if not message.from_user:
            return
        uid = message.from_user.id
        sess = _get_session(uid)
        if not sess:
            return

        step = sess.get("step")

        if step == "waiting_text_word":
            word = "".join(ch for ch in message.text.strip() if not ch.isspace())
            if not word or len(word) > 50:
                await reply_or_dm(client, message, "❌ Text 1 se 50 characters ke beech hona chahiye (bina spaces ke max 50). Dobara bhejein.")
                raise StopPropagation

            grid_sessions.update_one(
                {"user_id": str(uid)},
                {"$set": {"step": "waiting_text_style", "word": word, "started_at": time.time()}},
            )
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🌈 Neon Dark", callback_data="grid_tstyle|neon"),
                    InlineKeyboardButton("🔥 Gold & Black", callback_data="grid_tstyle|gold"),
                ],
                [
                    InlineKeyboardButton("💎 Clean White", callback_data="grid_tstyle|white"),
                    InlineKeyboardButton("🌸 Pastel Pink", callback_data="grid_tstyle|pink"),
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="grid_cancel")],
            ])
            await reply_or_dm(client, message, f"🎨 **`{word}`** ke liye style chunein:", reply_markup=kb)
            raise StopPropagation

        if step == "waiting_pack_link":
            short_name = extract_pack_short_name(message.text.strip())
            if not short_name:
                await reply_or_dm(client, message, "❌ Invalid sticker pack link. Example: `https://t.me/addstickers/Animals`")
                raise StopPropagation

            proc = await reply_or_dm(client, message, "⚙️ Sticker pack fetch ho raha hai...")
            try:
                src_set = await get_sticker_set(short_name)
            except Exception:
                await proc.edit_text("❌ Ye sticker pack access nahi ho paaya.")
                grid_sessions.delete_one({"user_id": str(uid)})
                raise StopPropagation

            stickers = src_set.get("stickers", [])
            if not stickers:
                await proc.edit_text("❌ Sticker pack khali hai.")
                grid_sessions.delete_one({"user_id": str(uid)})
                raise StopPropagation

            grid_sessions.update_one(
                {"user_id": str(uid)},
                {"$set": {
                    "step": "waiting_convert_name",
                    "short_name": short_name,
                    "src_title": src_set.get("title") or short_name,
                    "started_at": time.time(),
                }},
            )
            await proc.edit_text(
                f"✅ Pack mil gaya: **{src_set.get('title') or short_name}** ({len(stickers)} stickers)\n\n"
                f"✏️ Ab apne naye **Custom Emoji Pack** ka **name/title** bhejein (max 64 characters):\n\n"
                f"Example: `My Cool Emojis`"
            )
            raise StopPropagation

        if step == "waiting_convert_name":
            custom_title = message.text.strip()
            if not custom_title or len(custom_title) > 64:
                await reply_or_dm(client, message, "❌ Title 1 se 64 characters ke beech hona chahiye. Dobara bhejein.")
                raise StopPropagation

            short_name = sess.get("short_name")
            grid_sessions.delete_one({"user_id": str(uid)})

            proc = await reply_or_dm(client, message, "⚙️ Sticker pack fetch ho raha hai...")
            try:
                src_set = await get_sticker_set(short_name)
            except Exception:
                await proc.edit_text("❌ Ye sticker pack access nahi ho paaya.")
                raise StopPropagation

            stickers = src_set.get("stickers", [])
            if not stickers:
                await proc.edit_text("❌ Sticker pack khali hai.")
                raise StopPropagation

            total = len(stickers)
            title = custom_title[:64]
            await proc.edit_text(f"🔄 **{total}** stickers ko Custom Emoji me convert kiya ja raha hai...\n`0 / {total}`")

            bot_username = _resolve_bot_username(client)
            if not bot_username:
                await proc.edit_text("❌ Bot username load nahi ho paaya.")
                raise StopPropagation

            async with aiohttp.ClientSession() as http_session:
                dl_sem = asyncio.Semaphore(10)
                ffmpeg_sem = asyncio.Semaphore(2)  # Prevent CPU thrashing on Railway/VPS

                async def _convert_one(st):
                    async with dl_sem:
                        fid = st["file_id"]
                        em = st.get("emoji") or "⭐"
                        raw_bytes = await _fast_download_bytes(fid, http_session)
                    if st.get("is_animated"):
                        if len(raw_bytes) > MAX_TGS_BYTES:
                            return None
                        return (raw_bytes, "animated", "e.tgs", "application/x-tgsticker", em)
                    elif st.get("is_video"):
                        raw_p = os.path.join(TEMP_DIR, f"grd_r_{uid}_{uuid.uuid4().hex}.webm")
                        out_p = os.path.join(TEMP_DIR, f"grd_o_{uid}_{uuid.uuid4().hex}.webm")
                        try:
                            with open(raw_p, "wb") as f:
                                f.write(raw_bytes)
                            async with ffmpeg_sem:
                                ok, _ = await _normalize_emoji_webm(FFMPEG_PATH, raw_p, out_p, is_webm_input=True)
                                if not ok:
                                    ok, _ = await _normalize_emoji_webm(FFMPEG_PATH, raw_p, out_p, is_webm_input=False)
                            if not ok:
                                return None
                            with open(out_p, "rb") as f:
                                return (f.read(), "video", "e.webm", "video/webm", em)
                        finally:
                            cleanup_temp_files(raw_p, out_p)
                    else:
                        png = await asyncio.to_thread(_normalize_emoji_png, raw_bytes)
                        return (png, "static", "e.png", "image/png", em)

                converted = []
                failed = 0
                for chunk_start in range(0, total, 20):
                    chunk = stickers[chunk_start:chunk_start + 20]
                    res_list = await asyncio.gather(*[_convert_one(s) for s in chunk], return_exceptions=True)
                    for r in res_list:
                        if isinstance(r, tuple):
                            converted.append(r)
                        else:
                            failed += 1
                    try:
                        await proc.edit_text(
                            f"⚡ Converting... `{len(converted) + failed} / {total}`"
                        )
                    except Exception:
                        pass

                if not converted:
                    await proc.edit_text("❌ Koi bhi sticker convert nahi ho paaya.")
                    raise StopPropagation

                # Step 2: Upload up to 50 emojis in ONE single createNewStickerSet call
                pack_name = _build_grid_pack_name(uid, bot_username)
                first_batch = converted[:50]
                batch_meta = []
                batch_files = {}
                for idx, (fbytes, fmt, fname, ctype, em) in enumerate(first_batch):
                    field = f"e_{idx}"
                    batch_meta.append({
                        "sticker": f"attach://{field}",
                        "format": fmt,
                        "emoji_list": [em],
                    })
                    batch_files[field] = (fbytes, fname, ctype)

                created = False
                start_idx = 0
                added = 0
                last_err = ""

                for attempt in range(10):
                    if attempt > 0:
                        pack_name = _build_grid_pack_name(uid, bot_username)
                    try:
                        await _bot_api_upload_multi(
                            "createNewStickerSet",
                            {
                                "user_id": uid,
                                "name": pack_name,
                                "title": title[:64],
                                "sticker_type": "custom_emoji",
                                "stickers": json.dumps(batch_meta),
                            },
                            batch_files,
                            session=http_session,
                        )
                        created = True
                        added = len(first_batch)
                        start_idx = len(first_batch)
                        break
                    except Exception as e:
                        last_err = str(e)
                        if "occupied" in last_err.lower() or "stickerset_invalid" in last_err.lower():
                            continue
                        break

                # Fallback: if 50-batch rejected, create with first sticker and parallel-add rest
                if not created:
                    first = converted[0]
                    for attempt in range(10):
                        if attempt > 0:
                            pack_name = _build_grid_pack_name(uid, bot_username)
                        try:
                            await _bot_api_upload_multi(
                                "createNewStickerSet",
                                {
                                    "user_id": uid,
                                    "name": pack_name,
                                    "title": title[:64],
                                    "sticker_type": "custom_emoji",
                                    "stickers": json.dumps([{
                                        "sticker": "attach://e_0",
                                        "format": first[1],
                                        "emoji_list": [first[4]],
                                    }]),
                                },
                                {"e_0": (first[0], first[2], first[3])},
                                session=http_session,
                            )
                            created = True
                            added = 1
                            start_idx = 1
                            break
                        except Exception as e:
                            last_err = str(e)
                            if "occupied" in last_err.lower() or "stickerset_invalid" in last_err.lower():
                                continue
                            break

                if not created:
                    await proc.edit_text(f"❌ Emoji pack create nahi ho paaya: {last_err}")
                    raise StopPropagation

                # Step 3: Upload remaining emojis concurrently (Semaphore 6 + shared session)
                if start_idx < len(converted):
                    try:
                        await proc.edit_text(f"⚡ Uploading emojis... `{added} / {len(converted)}`")
                    except Exception:
                        pass

                    up_sem = asyncio.Semaphore(6)

                    async def _upload_one(it):
                        async with up_sem:
                            await _bot_api_upload_multi(
                                "addStickerToSet",
                                {
                                    "user_id": uid,
                                    "name": pack_name,
                                    "sticker": json.dumps({
                                        "sticker": "attach://e_0",
                                        "format": it[1],
                                        "emoji_list": [it[4]],
                                    }),
                                },
                                {"e_0": (it[0], it[2], it[3])},
                                session=http_session,
                            )

                    rem = converted[start_idx:]
                    for chunk_start in range(0, len(rem), 15):
                        chunk = rem[chunk_start:chunk_start + 15]
                        up_res = await asyncio.gather(*[_upload_one(it) for it in chunk], return_exceptions=True)
                        for ur in up_res:
                            if isinstance(ur, Exception):
                                failed += 1
                            else:
                                added += 1
                        try:
                            await proc.edit_text(f"⚡ Uploading emojis... `{added} / {len(converted)}`")
                        except Exception:
                            pass

            premium_packs.update_one(
                {"user_id": str(uid), "pack_name": pack_name},
                {"$set": {
                    "user_id": str(uid),
                    "pack_name": pack_name,
                    "title": title[:64],
                    "pack_type": "converted",
                    "count": added,
                    "created_at": datetime.utcnow(),
                }},
                upsert=True,
            )
            asyncio.create_task(send_log(
                client,
                db,
                f"🔄 <b>Sticker Pack → Emoji Pack Converted</b>\n"
                f"├ 👤 <b>User ID:</b> <code>{uid}</code>\n"
                f"├ 📝 <b>Title:</b> {html.escape(title[:64])}\n"
                f"├ 🎨 <b>Converted:</b> {added}/{total}\n"
                f"└ 🔗 <b>Link:</b> https://t.me/addemoji/{pack_name}",
            ))

            link = f"https://t.me/addemoji/{pack_name}"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🌟 Emoji Pack Kholen", url=link)]])
            await proc.edit_text(
                f"✅ **Pack Convert Ho Gaya!**\n\n"
                f"📦 **Title:** {title[:64]}\n"
                f"🎨 **Added:** `{added}` | ❌ **Skipped/Failed:** `{failed}`\n"
                f"🔗 **Link:** {link}",
                reply_markup=kb,
            )
            raise StopPropagation

    @app.on_message(
        (filters.photo | filters.document | filters.sticker)
        & (filters.private | filters.group),
        group=3,
    )
    async def grid_media_handler(client: Client, message: Message):
        if not message.from_user:
            return
        uid = message.from_user.id
        sess = _get_session(uid)
        if not sess or sess.get("step") != "waiting_grid_photo":
            return

        file_id = None
        if message.photo:
            file_id = message.photo.file_id
        elif message.sticker and not (message.sticker.is_video or message.sticker.is_animated):
            file_id = message.sticker.file_id
        elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
            file_id = message.document.file_id
        else:
            await reply_or_dm(client, message, "❌ Please ek static **Photo / Image / Static Sticker** bhejein.")
            raise StopPropagation

        n = int(sess.get("grid_n", 3))
        grid_sessions.delete_one({"user_id": str(uid)})
        proc = await reply_or_dm(client, message, f"🧩 Photo ko **{n}x{n} ({n*n} emojis)** me split aur upload kiya ja raha hai...")

        try:
            bio = await client.download_media(file_id, in_memory=True)
            raw_bytes = bio.getvalue()
            if len(raw_bytes) > MAX_INPUT_BYTES:
                await proc.edit_text("❌ File bahut badi hai (max 20 MB).")
                raise StopPropagation
            tiles = await asyncio.to_thread(_split_image_grid, raw_bytes, n)
        except StopPropagation:
            raise
        except Exception as e:
            print(f"[grid.py] split error: {e}")
            await proc.edit_text("❌ Image process nahi ho paayi. Doosri photo bhejein.")
            raise StopPropagation

        items = [(t, "static", f"tile_{i}.png", "image/png") for i, t in enumerate(tiles)]
        title = f"{n}x{n} Big Emoji Grid"
        ok, err, pack_name = await _create_pack_batch(client, uid, title, items, pack_type=f"grid_{n}x{n}")
        if not ok:
            await proc.edit_text(err)
            raise StopPropagation

        link = f"https://t.me/addemoji/{pack_name}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🌟 Emoji Pack Kholen", url=link)]])
        await proc.edit_text(
            f"✅ **{n}x{n} Big Emoji Grid Taiyaar Hai!**\n\n"
            f"🧩 **Tiles:** {n*n} Custom Emojis\n"
            f"🔗 **Link:** {link}\n\n"
            f"👇 Niche wala grid message forward/copy karke chat me use karein *(Telegram Premium required)*:",
            reply_markup=kb,
        )

        # Send ready-to-copy N x N grid message using custom_emoji entities
        try:
            st_set = await get_sticker_set(pack_name)
            eids = [s["custom_emoji_id"] for s in st_set.get("stickers", []) if s.get("custom_emoji_id")]
            if eids:
                grid_text, grid_entities = _build_custom_emoji_message(eids, cols=n)
                await message.reply_text(grid_text, entities=grid_entities)
        except Exception as e:
            print(f"[grid.py] preview grid error: {e}")

        raise StopPropagation

    register_user_command("grid", "Photo Grid (3x3/4x4/5x5), Sticker->Emoji converter, & Text->Emoji.", category=CATEGORY_STICKER)
