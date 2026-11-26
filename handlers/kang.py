# -*- coding: utf-8 -*-
"""
handlers/kang.py
================
/kang — Reply to a sticker or image/video and choose to create a new pack or add to an existing pack.
"""
import os
import time
import uuid

from pyrogram import Client, filters
from pyrogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

KANG_SESSION_TIMEOUT = 600


def register(app, fsub):
    from stick import (
        TEMP_DIR,
        kang_sessions,
        small_caps,
        reply_or_dm,
        get_user_pack,
        get_user_packs,
        get_next_pack_index,
        create_pack_record,
        increment_pack_sticker_count,
        create_unique_sticker_pack,
        create_unique_video_sticker_pack,
        create_unique_sticker_pack_from_input,
        add_sticker_to_pack,
        add_video_sticker_to_pack,
        add_sticker_input_to_pack,
        normalize_video_sticker,
        cleanup_temp_files,
        register_user_command,
        CATEGORY_STICKER,
    )

    def _session_expired(session):
        return (time.time() - session.get("started_at", 0)) > KANG_SESSION_TIMEOUT

    async def _kang_to_new_pack(client: Client, uid: int, session: dict, display_title: str):
        emoji = session["emoji"]
        pack_index = get_next_pack_index(uid)

        if session.get("is_sticker"):
            fmt = "video" if session.get("is_video") else ("animated" if session.get("is_animated") else "static")
            pack_type = "video" if fmt == "video" else "static"
            sticker_item = {
                "sticker": session["file_id"],
                "format": fmt,
                "emoji_list": [emoji],
            }
            ok, result, tg_pack_name = await create_unique_sticker_pack_from_input(
                uid, display_title, [sticker_item]
            )
            if ok:
                create_pack_record(uid, pack_index, display_title, tg_pack_name, emoji, pack_type=pack_type)
                return True, result, pack_index
            return False, result, pack_index

        # Non-sticker media (photo or video)
        if session.get("is_video"):
            raw_path = os.path.join(TEMP_DIR, f"kang_raw_{uid}_{uuid.uuid4().hex}.mp4")
            webm_path = os.path.join(TEMP_DIR, f"kang_out_{uid}_{uuid.uuid4().hex}.webm")
            try:
                await client.download_media(session["file_id"], file_name=raw_path)
                norm_ok, norm_err = await normalize_video_sticker(raw_path, webm_path)
                if not norm_ok:
                    return False, norm_err or "Failed to convert video.", pack_index
                with open(webm_path, "rb") as f:
                    webm_bytes = f.read()
                ok, result, tg_pack_name = await create_unique_video_sticker_pack(
                    client, uid, display_title, webm_bytes, emoji
                )
                if ok:
                    create_pack_record(uid, pack_index, display_title, tg_pack_name, emoji, pack_type="video")
                return ok, result, pack_index
            finally:
                cleanup_temp_files(raw_path, webm_path)
        else:
            bio = await client.download_media(session["file_id"], in_memory=True)
            img_bytes = bio.getvalue()
            ok, result, tg_pack_name = await create_unique_sticker_pack(
                client, uid, display_title, img_bytes, emoji
            )
            if ok:
                create_pack_record(uid, pack_index, display_title, tg_pack_name, emoji, pack_type="static")
            return ok, result, pack_index

    async def _kang_to_existing_pack(client: Client, uid: int, session: dict, pack: dict):
        emoji = session["emoji"]
        tg_pack_name = pack["telegram_pack_name"]
        pack_index = pack["pack_index"]

        if session.get("is_sticker"):
            fmt = "video" if session.get("is_video") else ("animated" if session.get("is_animated") else "static")
            sticker_item = {
                "sticker": session["file_id"],
                "format": fmt,
                "emoji_list": [emoji],
            }
            ok, err = await add_sticker_input_to_pack(uid, tg_pack_name, sticker_item)
            if ok:
                increment_pack_sticker_count(uid, pack_index)
                return True, f"https://t.me/addstickers/{tg_pack_name}"
            return False, err

        if session.get("is_video"):
            raw_path = os.path.join(TEMP_DIR, f"kang_raw_{uid}_{uuid.uuid4().hex}.mp4")
            webm_path = os.path.join(TEMP_DIR, f"kang_out_{uid}_{uuid.uuid4().hex}.webm")
            try:
                await client.download_media(session["file_id"], file_name=raw_path)
                norm_ok, norm_err = await normalize_video_sticker(raw_path, webm_path)
                if not norm_ok:
                    return False, norm_err or "Failed to convert video."
                with open(webm_path, "rb") as f:
                    webm_bytes = f.read()
                ok, err = await add_video_sticker_to_pack(client, uid, tg_pack_name, webm_bytes, emoji)
                if ok:
                    increment_pack_sticker_count(uid, pack_index)
                    return True, f"https://t.me/addstickers/{tg_pack_name}"
                return False, err
            finally:
                cleanup_temp_files(raw_path, webm_path)
        else:
            bio = await client.download_media(session["file_id"], in_memory=True)
            img_bytes = bio.getvalue()
            ok, err = await add_sticker_to_pack(client, uid, tg_pack_name, img_bytes, emoji)
            if ok:
                increment_pack_sticker_count(uid, pack_index)
                return True, f"https://t.me/addstickers/{tg_pack_name}"
            return False, err

    # ==============================================
    # COMMAND: /kang
    # ==============================================
    @app.on_message(filters.command("kang") & (filters.private | filters.group))
    async def kang_cmd(client: Client, message: Message):
        if not await fsub.check(client, message):
            return

        uid = message.from_user.id
        r = message.reply_to_message
        if not r:
            await reply_or_dm(
                client,
                message,
                small_caps("❌ Reply to a sticker, photo, or video with /kang to add it to a sticker pack!"),
            )
            return

        args = message.text.split(maxsplit=1)
        custom_emoji = args[1].strip() if len(args) > 1 and args[1].strip() else None
        default_emoji = get_user_pack(uid)["emoji"]

        is_sticker = False
        is_video = False
        is_animated = False
        file_id = None
        emoji = custom_emoji or default_emoji

        if r.sticker:
            is_sticker = True
            is_video = bool(r.sticker.is_video)
            is_animated = bool(r.sticker.is_animated)
            file_id = r.sticker.file_id
            emoji = custom_emoji or r.sticker.emoji or default_emoji
        elif r.photo:
            file_id = r.photo.file_id
        elif r.document and r.document.mime_type and r.document.mime_type.startswith("image/"):
            file_id = r.document.file_id
        elif r.video or r.animation:
            is_video = True
            file_id = (r.video or r.animation).file_id
        elif r.document and r.document.mime_type and r.document.mime_type.startswith("video/"):
            is_video = True
            file_id = r.document.file_id
        else:
            await reply_or_dm(
                client,
                message,
                small_caps("❌ Please reply to a valid sticker, photo, or video."),
            )
            return

        kang_sessions[uid] = {
            "step": "kang_choose_dest",
            "file_id": file_id,
            "is_sticker": is_sticker,
            "is_video": is_video,
            "is_animated": is_animated,
            "emoji": emoji,
            "started_at": time.time(),
        }

        kind_label = "🎥 Video Sticker" if is_video else ("✨ Animated Sticker" if is_animated else "🖼 Static Sticker")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🆕 Create New Pack", callback_data="kangdest_new")],
            [InlineKeyboardButton("📦 Add to Existing Pack", callback_data="kangdest_existing")],
            [InlineKeyboardButton("❌ Cancel", callback_data="kangflow_cancel")],
        ])
        await reply_or_dm(
            client,
            message,
            small_caps(f"🧲 **Kang Sticker** ({kind_label} • {emoji})\n\nWhere do you want to save this sticker?"),
            reply_markup=kb,
        )

    # ==============================================
    # CALLBACKS FOR /kang
    # ==============================================
    @app.on_callback_query(filters.regex(r"^kangflow_cancel$"))
    async def kang_cancel_cb(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        kang_sessions.pop(uid, None)
        await callback.answer("Cancelled")
        await callback.message.edit_text(small_caps("❌ Kang cancelled."))

    @app.on_callback_query(filters.regex(r"^kangdest_new$"))
    async def kang_new_pack_cb(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        sess = kang_sessions.get(uid)
        if not sess or _session_expired(sess):
            kang_sessions.pop(uid, None)
            await callback.answer("Session expired.", show_alert=True)
            await callback.message.edit_text(small_caps("❌ Session expired. Reply to a sticker with /kang again."))
            return

        sess["step"] = "kang_waiting_pack_name"
        sess["started_at"] = time.time()
        await callback.answer()
        await callback.message.edit_text(
            small_caps("✏️ Send a name for your new sticker pack.\n\nExample:\nMy Cool Stickers"),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="kangflow_cancel")]]),
        )

    @app.on_callback_query(filters.regex(r"^kangdest_existing$"))
    async def kang_existing_pack_cb(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        sess = kang_sessions.get(uid)
        if not sess or _session_expired(sess):
            kang_sessions.pop(uid, None)
            await callback.answer("Session expired.", show_alert=True)
            await callback.message.edit_text(small_caps("❌ Session expired. Reply to a sticker with /kang again."))
            return

        packs = get_user_packs(uid)
        if not packs:
            await callback.answer()
            await callback.message.edit_text(
                small_caps("📦 You don't have any sticker packs yet.\nTap below to create a new one!"),
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🆕 Create New Pack", callback_data="kangdest_new")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="kangflow_cancel")],
                ]),
            )
            return

        rows = []
        for p in packs:
            p_icon = "🎥" if p.get("pack_type") == "video" else "🖼"
            label = f"{p_icon} [{p['pack_index']}] {p['display_title']} ({p.get('total_stickers', 0)})"
            rows.append([InlineKeyboardButton(label[:60], callback_data=f"kangpack_{p['pack_index']}")])
        rows.append([
            InlineKeyboardButton("🆕 New Pack", callback_data="kangdest_new"),
            InlineKeyboardButton("❌ Cancel", callback_data="kangflow_cancel"),
        ])

        await callback.answer()
        await callback.message.edit_text(
            small_caps("📦 Select a sticker pack to add this sticker to:"),
            reply_markup=InlineKeyboardMarkup(rows),
        )

    @app.on_callback_query(filters.regex(r"^kangpack_(\d+)$"))
    async def kang_select_pack_cb(client: Client, callback: CallbackQuery):
        uid = callback.from_user.id
        sess = kang_sessions.get(uid)
        if not sess or _session_expired(sess):
            kang_sessions.pop(uid, None)
            await callback.answer("Session expired.", show_alert=True)
            await callback.message.edit_text(small_caps("❌ Session expired. Reply to a sticker with /kang again."))
            return

        pack_index = int(callback.matches[0].group(1))
        packs = get_user_packs(uid)
        target_pack = next((p for p in packs if p["pack_index"] == pack_index), None)
        if not target_pack:
            await callback.answer("Pack not found.", show_alert=True)
            return

        await callback.answer("Adding sticker...")
        await callback.message.edit_text(small_caps("⚙️ Adding sticker to your pack..."))

        ok, res = await _kang_to_existing_pack(client, uid, sess, target_pack)
        kang_sessions.pop(uid, None)

        if ok:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🌟 Open Sticker Pack", url=res)]])
            await callback.message.edit_text(
                small_caps(
                    f"✅ **Sticker Added!**\n\n"
                    f"📦 Pack: {target_pack['display_title']}\n"
                    f"😊 Emoji: {sess['emoji']}"
                ),
                reply_markup=kb,
            )
        else:
            await callback.message.edit_text(small_caps(f"❌ Failed to add sticker:\n{res}"))

    # ==============================================
    # TEXT INPUT FOR NEW PACK NAME (/kang)
    # ==============================================
    @app.on_message(filters.text & (filters.private | filters.group) & ~filters.regex(r"^/"), group=12)
    async def kang_text_input(client: Client, message: Message):
        if not message.from_user:
            return
        uid = message.from_user.id
        sess = kang_sessions.get(uid)
        if not sess or sess.get("step") != "kang_waiting_pack_name":
            return

        if _session_expired(sess):
            kang_sessions.pop(uid, None)
            await reply_or_dm(client, message, small_caps("❌ Session expired. Reply to a sticker with /kang again."))
            return

        display_title = message.text.strip()
        if not display_title:
            await reply_or_dm(client, message, small_caps("❌ Please send a valid pack name."))
            return

        proc = await reply_or_dm(client, message, small_caps("⚙️ Creating new sticker pack..."))
        ok, res, pack_index = await _kang_to_new_pack(client, uid, sess, display_title)
        kang_sessions.pop(uid, None)

        if ok:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🌟 Open Sticker Pack", url=res)]])
            await proc.edit_text(
                small_caps(
                    f"✅ **Sticker Pack Created!**\n\n"
                    f"📦 Pack Index: {pack_index}\n"
                    f"📝 Name: {display_title}\n"
                    f"😊 Emoji: {sess['emoji']}"
                ),
                reply_markup=kb,
            )
        else:
            await proc.edit_text(small_caps(f"❌ Failed to create sticker pack:\n{res}"))

    register_user_command("kang", "Reply to a sticker/image to save it to a new or existing pack.", category=CATEGORY_STICKER)
