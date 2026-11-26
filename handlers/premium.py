# -*- coding: utf-8 -*-
"""
handlers/premium.py
===================
/pr (alias: /premium) — Create and manage Telegram Premium Custom Emoji Packs.
Supports:
- Static images/stickers -> 100x100 transparent PNG
- Animated stickers (.tgs) -> Lottie TGS (<= 64 KB)
- Video / GIF / Video stickers -> 100x100 VP9 WEBM, 30fps, max 3s, no audio, <= 256 KB
"""
import asyncio
import base64
import html
import json
import os
import random
import string
import time
import uuid
from datetime import datetime
from io import BytesIO

from PIL import Image
from pyrogram import Client, filters, StopPropagation
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from handlers.logger import send_log

MAX_EMOJI_PER_PACK = 200
MAX_INPUT_BYTES = 20 * 1024 * 1024   # 20 MB input safety cap
MAX_TGS_BYTES = 64 * 1024            # 64 KB Telegram TGS limit
MAX_WEBM_BYTES = 256 * 1024          # 256 KB Telegram WEBM limit
PREMIUM_SESSION_TIMEOUT = 1800       # 30 minutes


def _normalize_emoji_png(image_bytes: bytes) -> bytes:
    """Resize any image to 100x100 transparent PNG, preserving aspect ratio and centering."""
    img = Image.open(BytesIO(image_bytes)).convert("RGBA")
    w, h = img.size
    if w >= h:
        new_w = 100
        new_h = max(1, round(h * (100 / w)))
    else:
        new_h = 100
        new_w = max(1, round(w * (100 / h)))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
    canvas.paste(resized, ((100 - new_w) // 2, (100 - new_h) // 2), resized)

    out = BytesIO()
    canvas.save(out, format="PNG")
    out.seek(0)
    return out.getvalue()


async def _normalize_emoji_webm(ffmpeg_path: str, input_path: str, output_path: str, is_webm_input: bool = False):
    """Convert video/GIF/webm into 100x100 VP9 .webm (30fps, max 3s, no audio, <= 256 KB)."""
    vf = (
        "scale=100:100:force_original_aspect_ratio=decrease,"
        "pad=100:100:(ow-iw)/2:(oh-ih)/2:color=0x00000000,"
        "fps=30"
    )
    input_args = ["-c:v", "libvpx-vp9", "-i", input_path] if is_webm_input else ["-i", input_path]
    for bitrate, maxrate, bufsize in (("125k", "160k", "320k"), ("85k", "110k", "220k")):
        cmd = [
            ffmpeg_path, "-y",
            *input_args,
            "-t", "3",
            "-vf", vf,
            "-c:v", "libvpx-vp9",
            "-pix_fmt", "yuva420p",
            "-b:v", bitrate,
            "-maxrate", maxrate,
            "-bufsize", bufsize,
            "-an",
            "-deadline", "realtime",
            "-cpu-used", "8",
            "-auto-alt-ref", "0",
            output_path,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0 or not os.path.exists(output_path):
                err = stderr.decode(errors="ignore")[-400:] if stderr else "Unknown FFmpeg error"
                return False, err
            if os.path.getsize(output_path) <= MAX_WEBM_BYTES:
                return True, None
        except FileNotFoundError:
            return False, "FFmpeg server par installed nahi hai."
        except Exception as e:
            return False, str(e)

    return False, "Video compress karne ke baad bhi 256 KB se bada hai. Chhota video bhejein."


def _rand4() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=4))


def _build_emoji_pack_name(user_id: int, bot_username: str) -> str:
    uname = bot_username.lower().lstrip("@")
    suffix = f"_by_{uname}"
    core = f"e{user_id}_{_rand4()}"
    max_core = 64 - len(suffix)
    if len(core) > max_core:
        core = core[:max_core]
    return f"{core}{suffix}".lower()


def _extract_first_emoji(text: str, default: str = "😀") -> str:
    text = (text or "").strip()
    if not text or text.lower() in ("skip", "default"):
        return default
    token = text.split()[0]
    return token[:8] if token else default


_FORMAT_META = {
    "static": ("emoji.png", "image/png"),
    "animated": ("emoji.tgs", "application/x-tgsticker"),
    "video": ("emoji.webm", "video/webm"),
}


def register(app, fsub):
    from stick import (
        db,
        TEMP_DIR,
        FFMPEG_PATH,
        reply_or_dm,
        _resolve_bot_username,
        _bot_api_upload,
        _bot_api_json,
        get_sticker_set,
        cleanup_temp_files,
        register_user_command,
        CATEGORY_STICKER,
    )

    premium_sessions = db["premium_sessions"]
    premium_packs = db["premium_packs"]

    try:
        premium_sessions.create_index("user_id", unique=True)
        premium_packs.create_index([("user_id", 1), ("pack_name", 1)], unique=True)
    except Exception:
        pass

    def _get_session(uid: int):
        sess = premium_sessions.find_one({"user_id": str(uid)})
        if not sess:
            return None
        if (time.time() - sess.get("started_at", 0)) > PREMIUM_SESSION_TIMEOUT:
            premium_sessions.delete_one({"user_id": str(uid)})
            return None
        return sess

    async def _upload_custom_emoji(client: Client, uid: int, sess: dict, file_bytes: bytes, fmt: str, emoji: str):
        """Create a new custom_emoji pack or add to existing pack via Bot API."""
        is_new = sess.get("is_new", False)
        pack_name = sess.get("pack_name")
        title = sess.get("title", "Custom Emoji Pack")
        file_name, content_type = _FORMAT_META.get(fmt, ("emoji.png", "image/png"))

        if is_new:
            bot_username = _resolve_bot_username(client)
            if not bot_username:
                return False, "❌ Bot username load nahi ho paaya. Thodi der baad try karein.", pack_name

            for attempt in range(10):
                if attempt > 0:
                    pack_name = _build_emoji_pack_name(uid, bot_username)
                try:
                    stickers_payload = json.dumps([{
                        "sticker": "attach://emoji_file",
                        "format": fmt,
                        "emoji_list": [emoji],
                    }])
                    await _bot_api_upload(
                        "createNewStickerSet",
                        {
                            "user_id": uid,
                            "name": pack_name,
                            "title": title[:64],
                            "sticker_type": "custom_emoji",
                            "stickers": stickers_payload,
                        },
                        "emoji_file",
                        file_bytes,
                        file_name,
                        content_type=content_type,
                    )
                    premium_packs.update_one(
                        {"user_id": str(uid), "pack_name": pack_name},
                        {"$set": {
                            "user_id": str(uid),
                            "pack_name": pack_name,
                            "title": title[:64],
                            "pack_type": fmt,
                            "count": 1,
                            "created_at": datetime.utcnow(),
                        }},
                        upsert=True,
                    )
                    premium_sessions.update_one(
                        {"user_id": str(uid)},
                        {"$set": {
                            "is_new": False,
                            "pack_name": pack_name,
                            "pack_type": fmt,
                            "count": 1,
                            "step": "waiting_media",
                            "pending_b64": None,
                            "pending_fmt": None,
                            "started_at": time.time(),
                        }},
                    )
                    asyncio.create_task(send_log(
                        client,
                        db,
                        f"✨ <b>New Custom Emoji Pack Created</b>\n"
                        f"├ 👤 <b>User ID:</b> <code>{uid}</code>\n"
                        f"├ 📝 <b>Title:</b> {html.escape(title[:64])}\n"
                        f"├ 🎨 <b>Format:</b> <code>{fmt}</code>\n"
                        f"└ 🔗 <b>Link:</b> https://t.me/addemoji/{pack_name}",
                    ))
                    return True, None, pack_name
                except Exception as e:
                    err = str(e)
                    low = err.lower()
                    if "occupied" in low or "stickserset_invalid" in low or "stickerset_invalid" in low or ("already" in low and "name" in low):
                        continue
                    if "peer_id_invalid" in low:
                        return False, "❌ Pehle bot ko DM me /start karein, phir dobara try karein.", pack_name
                    print(f"[premium.py] createNewStickerSet error for {uid} ({fmt}): {err}")
                    return False, "❌ Pack create karne me error aaya. Dobara try karein.", pack_name
            return False, "❌ Unique pack name generate nahi ho paaya. Dobara try karein.", pack_name

        # Add to existing pack
        current_count = sess.get("count", 0)
        if current_count >= MAX_EMOJI_PER_PACK:
            return False, f"⚠️ Is pack me max {MAX_EMOJI_PER_PACK} emojis ki limit poori ho chuki hai! /pr se naya pack banayein.", pack_name

        try:
            sticker_payload = json.dumps({
                "sticker": "attach://emoji_file",
                "format": fmt,
                "emoji_list": [emoji],
            })
            await _bot_api_upload(
                "addStickerToSet",
                {
                    "user_id": uid,
                    "name": pack_name,
                    "sticker": sticker_payload,
                },
                "emoji_file",
                file_bytes,
                file_name,
                content_type=content_type,
            )
            new_count = current_count + 1
            premium_packs.update_one(
                {"user_id": str(uid), "pack_name": pack_name},
                {"$set": {"count": new_count}},
            )
            premium_sessions.update_one(
                {"user_id": str(uid)},
                {"$set": {
                    "count": new_count,
                    "step": "waiting_media",
                    "pending_b64": None,
                    "pending_fmt": None,
                    "started_at": time.time(),
                }},
            )
            return True, None, pack_name
        except Exception as e:
            err = str(e)
            print(f"[premium.py] addStickerToSet error for {uid} ({fmt}): {err}")
            if "stickers_too_much" in err.lower():
                return False, f"⚠️ Pack me {MAX_EMOJI_PER_PACK} emojis ki limit poori ho gayi hai! Naya pack banayein.", pack_name
            if "peer_id_invalid" in err.lower():
                return False, "❌ Pehle bot ko DM me /start karein, phir try karein.", pack_name
            return False, "❌ Emoji add karne me error aaya. Dobara media bhejein.", pack_name

    # ==============================================
    # /pr (alias: /premium) MENU
    # ==============================================
    @app.on_message(filters.command(["pr", "premium"]) & (filters.private | filters.group))
    async def premium_cmd(client: Client, message: Message):
        if not await fsub.check(client, message):
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🆕 Naya Pack Banao", callback_data="prem_new")],
            [InlineKeyboardButton("📦 Mere Packs", callback_data="prem_mypacks")],
            [InlineKeyboardButton("❌ Cancel", callback_data="prem_cancel")],
        ])
        await reply_or_dm(
            client,
            message,
            "✨ **Premium Custom Emoji Pack Maker**\n\n"
            "Yahan aap apna Telegram Custom Emoji Pack bana sakte hain:\n"
            "• 🖼 **Static:** Photo / PNG / JPG / WebP (100x100)\n"
            "• ✨ **Animated:** `.tgs` Animated Sticker\n"
            "• 🎥 **Video:** Video / GIF / `.webm` (100x100 VP9)\n\n"
            "👇 Ek option chunein:",
            reply_markup=kb,
        )

    # ==============================================
    # /done COMMAND
    # ==============================================
    @app.on_message(filters.command("done") & (filters.private | filters.group))
    async def premium_done_cmd(client: Client, message: Message):
        uid = message.from_user.id
        sess = _get_session(uid)
        if not sess:
            await reply_or_dm(client, message, "ℹ️ Abhi koi active `/pr` session nahi hai.")
            return

        pack_name = sess.get("pack_name")
        count = sess.get("count", 0)
        premium_sessions.delete_one({"user_id": str(uid)})

        if not pack_name or count == 0:
            await reply_or_dm(client, message, "❌ Koi emoji add nahi hua. `/pr` se naya pack banayein.")
            return

        link = f"https://t.me/addemoji/{pack_name}"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🌟 Emoji Pack Kholen", url=link)]])
        await reply_or_dm(
            client,
            message,
            f"✅ **Custom Emoji Pack Taiyaar Hai!**\n\n"
            f"📦 **Title:** {sess.get('title', 'Custom Emoji Pack')}\n"
            f"🎨 **Format:** `{sess.get('pack_type', 'static')}`\n"
            f"🔢 **Total Emojis:** {count}\n"
            f"🔗 **Link:** {link}\n\n"
            f"ℹ️ *Note: Ye custom emojis sirf Telegram Premium users hi use kar sakte hain.*",
            reply_markup=kb,
        )

    # ==============================================
    # CALLBACKS
    # ==============================================
    @app.on_callback_query(filters.regex(r"^prem_cancel$"))
    async def prem_cancel_cb(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        premium_sessions.delete_one({"user_id": str(uid)})
        await callback.answer("Cancelled")
        await callback.message.edit_text("❌ Premium Emoji session cancel kar diya gaya.")

    @app.on_callback_query(filters.regex(r"^prem_new$"))
    async def prem_new_cb(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        bot_username = _resolve_bot_username(client)
        if not bot_username:
            await callback.answer("Bot username abhi ready nahi hai.", show_alert=True)
            return

        premium_sessions.update_one(
            {"user_id": str(uid)},
            {"$set": {
                "user_id": str(uid),
                "step": "waiting_title",
                "is_new": True,
                "pack_name": None,
                "pack_type": None,
                "title": None,
                "count": 0,
                "pending_b64": None,
                "pending_fmt": None,
                "started_at": time.time(),
            }},
            upsert=True,
        )
        await callback.answer()
        await callback.message.edit_text(
            "✏️ Apne naye Custom Emoji Pack ka **title** bhejein (max 64 characters).\n\n"
            "Example: `My Cool Emojis`\n\n"
            "Cancel karne ke liye `/cancel` bhejein."
        )

    @app.on_callback_query(filters.regex(r"^prem_defemoji$"))
    async def prem_defemoji_cb(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        sess = _get_session(uid)
        if not sess or sess.get("step") != "waiting_emoji" or not sess.get("pending_b64"):
            await callback.answer("Session expire ho gaya hai.", show_alert=True)
            return

        await callback.answer("Uploading...")
        await callback.message.edit_text("⚙️ Emoji upload ho raha hai...")

        file_bytes = base64.b64decode(sess["pending_b64"])
        fmt = sess.get("pending_fmt") or "static"
        chosen_emoji = sess.get("suggested_emoji") or "😀"
        ok, err, pack_name = await _upload_custom_emoji(client, uid, sess, file_bytes, fmt, chosen_emoji)
        if ok:
            updated = premium_sessions.find_one({"user_id": str(uid)}) or {}
            cnt = updated.get("count", 1)
            await callback.message.edit_text(
                f"✅ Emoji add ho gaya! (`{chosen_emoji}` • `{fmt}`)\n"
                f"📊 **Total:** {cnt} / {MAX_EMOJI_PER_PACK}\n\n"
                f"Agla media bhejein, ya pack finish karne ke liye `/done` bhejein."
            )
        else:
            await callback.message.edit_text(err)

    @app.on_callback_query(filters.regex(r"^prem_mypacks$"))
    async def prem_mypacks_cb(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        packs = list(premium_packs.find({"user_id": str(uid)}))
        if not packs:
            await callback.answer()
            await callback.message.edit_text(
                "📦 Aapke paas abhi koi Custom Emoji Pack nahi hai.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🆕 Naya Pack Banao", callback_data="prem_new")],
                    [InlineKeyboardButton("❌ Close", callback_data="prem_cancel")],
                ]),
            )
            return

        await callback.answer()
        rows = []
        for p in packs:
            pname = p["pack_name"]
            ptype = p.get("pack_type", "static")
            icon = "🎥" if ptype == "video" else ("✨" if ptype == "animated" else "🖼")
            rows.append([
                InlineKeyboardButton(f"{icon} {p.get('title', pname)[:22]} ({p.get('count', 0)})", url=f"https://t.me/addemoji/{pname}"),
            ])
            rows.append([
                InlineKeyboardButton("➕ Emoji Add", callback_data=f"prem_add|{pname}"),
                InlineKeyboardButton("🗑 Del Last", callback_data=f"prem_dellast|{pname}"),
                InlineKeyboardButton("❌ Del Pack", callback_data=f"prem_delpack|{pname}"),
            ])
        rows.append([InlineKeyboardButton("🆕 Naya Pack Banao", callback_data="prem_new")])
        await callback.message.edit_text(
            "📦 **Aapke Custom Emoji Packs:**\n\nKisi bhi pack ko open, edit ya delete karein:",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    @app.on_callback_query(filters.regex(r"^prem_add\|(.+)$"))
    async def prem_add_more_cb(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        pname = callback.matches[0].group(1)
        pack = premium_packs.find_one({"user_id": str(uid), "pack_name": pname})
        if not pack:
            await callback.answer("Pack nahi mila.", show_alert=True)
            return
        if pack.get("count", 0) >= MAX_EMOJI_PER_PACK:
            await callback.answer(f"Max {MAX_EMOJI_PER_PACK} limit full!", show_alert=True)
            return

        ptype = pack.get("pack_type", "static")
        premium_sessions.update_one(
            {"user_id": str(uid)},
            {"$set": {
                "user_id": str(uid),
                "step": "waiting_media",
                "is_new": False,
                "pack_name": pname,
                "pack_type": ptype,
                "title": pack.get("title", pname),
                "count": pack.get("count", 0),
                "pending_b64": None,
                "pending_fmt": None,
                "started_at": time.time(),
            }},
            upsert=True,
        )
        await callback.answer()
        await callback.message.edit_text(
            f"➕ **{pack.get('title', pname)}** (`{ptype}`) me emoji add karne ke liye media bhejein.\n"
            f"Finish karne ke liye `/done` bhejein."
        )

    @app.on_callback_query(filters.regex(r"^prem_dellast\|(.+)$"))
    async def prem_del_last_cb(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        pname = callback.matches[0].group(1)
        pack = premium_packs.find_one({"user_id": str(uid), "pack_name": pname})
        if not pack:
            await callback.answer("Pack nahi mila.", show_alert=True)
            return
        try:
            st_set = await get_sticker_set(pname)
            stickers = st_set.get("stickers", [])
            if not stickers:
                await callback.answer("Pack khali hai.", show_alert=True)
                return
            last_fid = stickers[-1]["file_id"]
            await _bot_api_json("deleteStickerFromSet", {"sticker": last_fid})
            new_cnt = max(0, len(stickers) - 1)
            premium_packs.update_one({"user_id": str(uid), "pack_name": pname}, {"$set": {"count": new_cnt}})
            await callback.answer("✅ Last emoji delete ho gaya!", show_alert=True)
        except Exception as e:
            print(f"[premium.py] deleteStickerFromSet error: {e}")
            await callback.answer("❌ Emoji delete nahi ho paaya.", show_alert=True)

    @app.on_callback_query(filters.regex(r"^prem_delpack\|(.+)$"))
    async def prem_del_pack_cb(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        pname = callback.matches[0].group(1)
        try:
            await _bot_api_json("deleteStickerSet", {"name": pname})
        except Exception as e:
            print(f"[premium.py] deleteStickerSet error: {e}")
        premium_packs.delete_one({"user_id": str(uid), "pack_name": pname})
        await callback.answer("🗑 Pack delete ho gaya!", show_alert=True)
        await callback.message.edit_text(f"🗑 Pack `{pname}` delete kar diya gaya.")

    # ==============================================
    # GROUP=2 HANDLERS (Only active `premium_sessions`)
    # ==============================================
    @app.on_message(
        filters.text & (filters.private | filters.group) & ~filters.regex(r"^/"),
        group=2,
    )
    async def prem_text_handler(client: Client, message: Message):
        if not message.from_user:
            return
        uid = message.from_user.id
        sess = _get_session(uid)
        if not sess:
            return  # Normal bot flow continue hoga

        step = sess.get("step")
        if step == "waiting_title":
            title = message.text.strip()
            if not title or len(title) > 64:
                await reply_or_dm(client, message, "❌ Title 1 se 64 characters ke beech hona chahiye. Dobara bhejein.")
                raise StopPropagation

            bot_username = _resolve_bot_username(client)
            if not bot_username:
                await reply_or_dm(client, message, "❌ Bot username load nahi ho paaya. Thodi der baad try karein.")
                raise StopPropagation

            pack_name = _build_emoji_pack_name(uid, bot_username)
            premium_sessions.update_one(
                {"user_id": str(uid)},
                {"$set": {
                    "title": title,
                    "pack_name": pack_name,
                    "step": "waiting_media",
                    "started_at": time.time(),
                }},
            )
            await reply_or_dm(
                client,
                message,
                f"✅ Title set: **{title}**\n\n"
                f"📸 Ab koi bhi **Photo, Sticker (.webp/.tgs/.webm), GIF, ya Video** bhejein — bot format khud auto-set kar dega!\n"
                f"Finish karne ke liye `/done` bhejein.",
            )
            raise StopPropagation

        if step == "waiting_emoji" and sess.get("pending_b64"):
            emoji = _extract_first_emoji(message.text, default="😀")
            proc = await reply_or_dm(client, message, "⚙️ Emoji upload ho raha hai...")
            file_bytes = base64.b64decode(sess["pending_b64"])
            fmt = sess.get("pending_fmt") or "static"
            ok, err, _ = await _upload_custom_emoji(client, uid, sess, file_bytes, fmt, emoji)
            if ok:
                updated = premium_sessions.find_one({"user_id": str(uid)}) or {}
                cnt = updated.get("count", 1)
                await proc.edit_text(
                    f"✅ Emoji add ho gaya! ({emoji} • `{fmt}`)\n"
                    f"📊 **Total:** {cnt} / {MAX_EMOJI_PER_PACK}\n\n"
                    f"Agla media bhejein, ya finish karne ke liye `/done` bhejein."
                )
            else:
                await proc.edit_text(err)
            raise StopPropagation

    @app.on_message(
        (filters.photo | filters.document | filters.sticker | filters.video | filters.animation)
        & (filters.private | filters.group),
        group=2,
    )
    async def prem_media_handler(client: Client, message: Message):
        if not message.from_user:
            return
        uid = message.from_user.id
        sess = _get_session(uid)
        if not sess or sess.get("step") not in ("waiting_media", "waiting_emoji"):
            return  # Normal bot flow continue hoga

        # Detect incoming format: static | animated | video
        incoming_fmt = None
        file_id = None
        file_size = 0

        if message.sticker:
            file_id = message.sticker.file_id
            file_size = message.sticker.file_size or 0
            if message.sticker.is_video:
                incoming_fmt = "video"
            elif message.sticker.is_animated:
                incoming_fmt = "animated"
            else:
                incoming_fmt = "static"
        elif message.photo:
            file_id = message.photo.file_id
            file_size = message.photo.file_size or 0
            incoming_fmt = "static"
        elif message.video:
            file_id = message.video.file_id
            file_size = message.video.file_size or 0
            incoming_fmt = "video"
        elif message.animation:
            file_id = message.animation.file_id
            file_size = message.animation.file_size or 0
            incoming_fmt = "video"
        elif message.document:
            file_id = message.document.file_id
            file_size = message.document.file_size or 0
            mime = (message.document.mime_type or "").lower()
            fname = (message.document.file_name or "").lower()
            if fname.endswith(".tgs") or "tgsticker" in mime:
                incoming_fmt = "animated"
            elif mime.startswith("video/") or fname.endswith((".webm", ".mp4", ".gif", ".mov")):
                incoming_fmt = "video"
            elif mime.startswith("image/") or fname.endswith((".png", ".jpg", ".jpeg", ".webp")):
                incoming_fmt = "static"

        if not incoming_fmt or not file_id:
            await reply_or_dm(client, message, "❌ Unsupported file! Photo, Sticker (.webp/.tgs/.webm), GIF, ya Video bhejein.")
            raise StopPropagation

        if file_size and file_size > MAX_INPUT_BYTES:
            await reply_or_dm(client, message, "❌ File bahut badi hai (max 20 MB).")
            raise StopPropagation

        proc = await reply_or_dm(client, message, f"⚙️ `{incoming_fmt}` emoji auto-convert ho raha hai...")
        out_bytes = None
        raw_path = None
        webm_path = None

        try:
            if incoming_fmt == "static":
                bio = await client.download_media(file_id, in_memory=True)
                raw_bytes = bio.getvalue()
                if len(raw_bytes) > MAX_INPUT_BYTES:
                    await proc.edit_text("❌ File bahut badi hai (max 20 MB).")
                    raise StopPropagation
                out_bytes = await asyncio.to_thread(_normalize_emoji_png, raw_bytes)

            elif incoming_fmt == "animated":
                bio = await client.download_media(file_id, in_memory=True)
                out_bytes = bio.getvalue()
                if len(out_bytes) > MAX_TGS_BYTES:
                    await proc.edit_text("❌ `.tgs` file 64 KB se badi hai. Chhoti animated file bhejein.")
                    raise StopPropagation

            elif incoming_fmt == "video":
                raw_path = os.path.join(TEMP_DIR, f"prem_raw_{uid}_{uuid.uuid4().hex}")
                webm_path = os.path.join(TEMP_DIR, f"prem_out_{uid}_{uuid.uuid4().hex}.webm")
                await client.download_media(file_id, file_name=raw_path)
                is_webm_src = bool(
                    (message.sticker and message.sticker.is_video)
                    or (message.document and (message.document.file_name or "").lower().endswith(".webm"))
                )
                ok, err = await _normalize_emoji_webm(FFMPEG_PATH, raw_path, webm_path, is_webm_input=is_webm_src)
                if not ok and is_webm_src:
                    # Fallback without forcing vp9 decoder (in case source sticker is vp8)
                    ok, err = await _normalize_emoji_webm(FFMPEG_PATH, raw_path, webm_path, is_webm_input=False)
                if not ok:
                    print(f"[premium.py] FFmpeg error for {uid}: {err}")
                    await proc.edit_text(f"❌ Video convert nahi ho paaya: {err}")
                    raise StopPropagation
                with open(webm_path, "rb") as f:
                    out_bytes = f.read()

        except StopPropagation:
            raise
        except Exception as e:
            print(f"[premium.py] media process error for {uid} ({incoming_fmt}): {e}")
            await proc.edit_text("❌ Corrupt ya unsupported file. Doosra media bhejein.")
            raise StopPropagation
        finally:
            cleanup_temp_files(raw_path, webm_path)

        suggested = (message.sticker.emoji if (message.sticker and message.sticker.emoji) else "😀")
        premium_sessions.update_one(
            {"user_id": str(uid)},
            {"$set": {
                "step": "waiting_emoji",
                "pending_b64": base64.b64encode(out_bytes).decode("ascii"),
                "pending_fmt": incoming_fmt,
                "suggested_emoji": suggested,
                "started_at": time.time(),
            }},
        )

        btn_label = f"{suggested} Sticker Emoji ({suggested}) Use Karein" if (message.sticker and message.sticker.emoji) else "😀 Default (😀) Use Karein"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(btn_label, callback_data="prem_defemoji")],
            [InlineKeyboardButton("❌ Cancel", callback_data="prem_cancel")],
        ])
        await proc.edit_text(
            f"😊 `{incoming_fmt}` emoji ready hai!\n"
            f"Iske liye **koi ek emoji** bhejein (jaise `🔥` ya `❤️`), ya niche button dabayein:",
            reply_markup=kb,
        )
        raise StopPropagation

    register_user_command("pr", "Create & manage Telegram Premium Custom Emoji packs.", category=CATEGORY_STICKER)
