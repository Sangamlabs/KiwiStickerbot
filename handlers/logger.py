# -*- coding: utf-8 -*-
"""
handlers/logger.py
==================
Logger Group System:
- /setlog [chat_id | off] (Admin/Owner only): Set or view the Logger Group ID.
- Automatically logs:
  1. Bot start / restart
  2. Users starting the bot (/start)
  3. New sticker / emoji packs created
  4. Bot added to or removed from groups/channels (with who added/removed it)
"""
import html
import os
from datetime import datetime

from pyrogram import Client, filters
from pyrogram.enums import ChatMemberStatus, ParseMode
from pyrogram.types import ChatMemberUpdated, Message

_LOGGER_COL_NAME = "bot_settings"
_LOGGER_DOC_ID = "logger_config"


def get_logger_chat_id(db) -> int | None:
    """Return configured logger chat ID from MongoDB or .env (LOGGER_GROUP_ID / LOG_GROUP_ID)."""
    try:
        doc = db[_LOGGER_COL_NAME].find_one({"_id": _LOGGER_DOC_ID})
        if doc and doc.get("chat_id"):
            return int(doc["chat_id"])
    except Exception:
        pass

    env_val = (os.getenv("LOGGER_GROUP_ID") or os.getenv("LOG_GROUP_ID") or "").strip()
    if env_val and env_val.lstrip("-").isdigit():
        return int(env_val)
    return None


async def send_log(client: Client, db, text: str):
    """Safely send an HTML log message to the configured Logger Group."""
    chat_id = get_logger_chat_id(db)
    if not chat_id:
        return
    try:
        await client.send_message(
            chat_id,
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        print(f"[logger.py] Failed to send log to {chat_id}: {e}")


def _status_name(status) -> str:
    if hasattr(status, "value"):
        return str(status.value).lower()
    return str(status).split(".")[-1].lower()


def register(app, fsub):
    from stick import (
        db,
        admin_filter,
        register_admin_command,
    )

    @app.on_message(filters.command(["setlog", "setlogger"]) & admin_filter)
    async def setlog_cmd(client: Client, message: Message):
        args = message.text.split(maxsplit=1)
        if len(args) > 1:
            val = args[1].strip()
            if val.lower() in ("off", "none", "disable", "0"):
                db[_LOGGER_COL_NAME].delete_one({"_id": _LOGGER_DOC_ID})
                await message.reply_text("🔕 Logger Group disabled.")
                return
            if not val.lstrip("-").isdigit():
                await message.reply_text(
                    "❌ **Usage:**\n"
                    "• Logger group ke andar `/setlog` bhejein\n"
                    "• Ya `/setlog -1001234567890`\n"
                    "• Disable karne ke liye: `/setlog off`"
                )
                return
            target_id = int(val)
        else:
            if message.chat.type == "private":
                cur = get_logger_chat_id(db)
                await message.reply_text(
                    f"📢 **Current Logger Group:** `{cur or 'Not Set'}`\n\n"
                    "Set karne ke liye:\n"
                    "• Apne Logger Group me `/setlog` bhejein\n"
                    "• Ya `/setlog -1001234567890`\n"
                    "• Disable: `/setlog off`"
                )
                return
            target_id = message.chat.id

        try:
            chat = await client.get_chat(target_id)
            title = chat.title or str(target_id)
        except Exception as e:
            await message.reply_text(f"❌ Group access nahi ho paaya (`{target_id}`): {e}")
            return

        db[_LOGGER_COL_NAME].update_one(
            {"_id": _LOGGER_DOC_ID},
            {"$set": {"chat_id": target_id, "title": title, "updated_at": datetime.utcnow()}},
            upsert=True,
        )
        await message.reply_text(f"✅ **Logger Group Set!**\n📢 Group: **{title}** (`{target_id}`)")
        await send_log(
            client,
            db,
            f"✅ <b>Logger Group Connected!</b>\n"
            f"📢 <b>Chat:</b> {html.escape(title)} (<code>{target_id}</code>)\n"
            f"👤 <b>Set By:</b> {html.escape(message.from_user.first_name or 'Admin')} (<code>{message.from_user.id}</code>)",
        )

    @app.on_chat_member_updated(group=1)
    async def log_bot_chat_membership(client: Client, update: ChatMemberUpdated):
        try:
            me_id = client.me.id if getattr(client, "me", None) else (await client.get_me()).id
        except Exception:
            return

        new = update.new_chat_member
        old = update.old_chat_member
        target_user = (new.user if new else None) or (old.user if old else None)
        if not target_user or target_user.id != me_id:
            return

        new_st = _status_name(new.status) if new else "left"
        old_st = _status_name(old.status) if old else "left"

        if new_st == old_st:
            return

        chat = update.chat
        chat_title = html.escape(chat.title or chat.first_name or str(chat.id))
        actor = update.from_user
        actor_str = (
            f"{html.escape(actor.first_name or 'User')} (<code>{actor.id}</code>)"
            if actor
            else "Unknown"
        )

        inactive = {"left", "kicked", "banned"}
        if old_st in inactive and new_st not in inactive:
            await send_log(
                client,
                db,
                f"➕ <b>Bot Added to Chat</b>\n"
                f"├ 📢 <b>Chat:</b> {chat_title}\n"
                f"├ 🆔 <b>Chat ID:</b> <code>{chat.id}</code>\n"
                f"├ 🛡 <b>Role:</b> <code>{html.escape(new_st)}</code>\n"
                f"└ 👤 <b>Added By:</b> {actor_str}",
            )
        elif old_st not in inactive and new_st in inactive:
            await send_log(
                client,
                db,
                f"➖ <b>Bot Removed from Chat</b>\n"
                f"├ 📢 <b>Chat:</b> {chat_title}\n"
                f"├ 🆔 <b>Chat ID:</b> <code>{chat.id}</code>\n"
                f"├ 🚫 <b>Status:</b> <code>{html.escape(new_st)}</code>\n"
                f"└ 👤 <b>Removed By:</b> {actor_str}",
            )
        elif new_st in ("administrator", "creator") and old_st not in ("administrator", "creator"):
            await send_log(
                client,
                db,
                f"👑 <b>Bot Promoted to Admin</b>\n"
                f"├ 📢 <b>Chat:</b> {chat_title}\n"
                f"├ 🆔 <b>Chat ID:</b> <code>{chat.id}</code>\n"
                f"└ 👤 <b>By:</b> {actor_str}",
            )

    register_admin_command("setlog", "Set or disable the Logger Group for bot event logs.")
