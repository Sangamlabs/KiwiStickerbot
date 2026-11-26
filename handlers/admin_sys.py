# -*- coding: utf-8 -*-
"""
handlers/admin_sys.py
=====================
Owner & Sudo Admin System + System Commands:
- /addsudo, /rmsudo (/delsudo, /removesudo), /sudolist (/admins)
- /bc (copy broadcast), /fbc (forward broadcast)
- /ping, /btstats, /restart
"""
import asyncio
import os
import platform
import subprocess
import sys
import time
from datetime import datetime

import pyrogram
from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import Message


def _format_uptime(seconds: float) -> str:
    sec = int(max(0, seconds))
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


async def _resolve_target_user(client: Client, message: Message):
    """Resolve target user from reply, numeric user_id, or @username."""
    if message.reply_to_message and message.reply_to_message.from_user:
        u = message.reply_to_message.from_user
        return u.id, (u.first_name or "User"), u.username

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return None, None, None

    target = parts[1].strip().split()[0]
    if target.lstrip("-").isdigit():
        uid = int(target)
        try:
            u = await client.get_users(uid)
            return u.id, (u.first_name or str(uid)), u.username
        except Exception:
            return uid, str(uid), None

    try:
        u = await client.get_users(target)
        return u.id, (u.first_name or target), u.username
    except Exception:
        return None, None, None


def register(app, fsub):
    from stick import (
        BOT_START_TIME,
        OWNER_IDS,
        ADMIN_IDS,
        db,
        mongo_client,
        users_collection,
        packs_collection,
        sudo_collection,
        is_owner,
        is_admin,
        reload_sudo_admins,
        register_admin_command,
    )

    owner_filter = filters.create(lambda _, __, m: bool(m.from_user and is_owner(m.from_user.id)))
    admin_filter = filters.create(lambda _, __, m: bool(m.from_user and is_admin(m.from_user.id)))

    # ==============================================
    # /addsudo (Owner Only)
    # ==============================================
    @app.on_message(filters.command("addsudo") & (filters.private | filters.group))
    async def addsudo_cmd(client: Client, message: Message):
        if not message.from_user or not is_owner(message.from_user.id):
            await message.reply_text("🚫 Only the Bot Owner can add sudo admins.")
            return

        uid, name, username = await _resolve_target_user(client, message)
        if not uid:
            await message.reply_text(
                "❌ **Usage:**\n"
                "• Reply to a user's message with `/addsudo`\n"
                "• `/addsudo 123456789`\n"
                "• `/addsudo @username`"
            )
            return

        if is_owner(uid):
            await message.reply_text("ℹ️ That user is already the Bot Owner.")
            return

        existing = sudo_collection.find_one({"user_id": uid})
        if existing:
            await message.reply_text(f"ℹ️ **{name}** (`{uid}`) is already a Sudo Admin.")
            return

        sudo_collection.update_one(
            {"user_id": uid},
            {"$set": {
                "user_id": uid,
                "name": name,
                "username": username,
                "added_by": message.from_user.id,
                "added_at": datetime.utcnow(),
            }},
            upsert=True,
        )
        reload_sudo_admins(fsub)

        uname_str = f"@{username}" if username else "No username"
        await message.reply_text(
            f"✅ **Sudo Admin Added!**\n\n"
            f"👤 **Name:** {name}\n"
            f"📌 **Username:** {uname_str}\n"
            f"🆔 **User ID:** `{uid}`"
        )

    # ==============================================
    # /rmsudo /delsudo /removesudo (Owner Only)
    # ==============================================
    @app.on_message(filters.command(["rmsudo", "delsudo", "removesudo"]) & (filters.private | filters.group))
    async def rmsudo_cmd(client: Client, message: Message):
        if not message.from_user or not is_owner(message.from_user.id):
            await message.reply_text("🚫 Only the Bot Owner can remove sudo admins.")
            return

        uid, name, _ = await _resolve_target_user(client, message)
        if not uid:
            await message.reply_text(
                "❌ **Usage:**\n"
                "• Reply to a user's message with `/rmsudo`\n"
                "• `/rmsudo 123456789`\n"
                "• `/rmsudo @username`"
            )
            return

        if is_owner(uid):
            await message.reply_text("❌ You cannot remove the Bot Owner.")
            return

        res = sudo_collection.delete_one({"user_id": uid})
        if res.deleted_count == 0:
            await message.reply_text(f"ℹ️ User `{uid}` is not in the Sudo Admins list.")
            return

        reload_sudo_admins(fsub)
        await message.reply_text(
            f"🗑 **Sudo Admin Removed!**\n\n"
            f"👤 **User:** {name} (`{uid}`)"
        )

    # ==============================================
    # /sudolist /admins (Admin Only)
    # ==============================================
    @app.on_message(filters.command(["sudolist", "admins"]) & admin_filter)
    async def sudolist_cmd(client: Client, message: Message):
        lines = ["👑 **Bot Owner(s)**"]
        for oid in OWNER_IDS:
            try:
                u = await client.get_users(oid)
                uname = f"@{u.username}" if u.username else "No username"
                lines.append(f"• **{u.first_name or 'Owner'}** ({uname}) — `{oid}`")
            except Exception:
                lines.append(f"• `Owner` — `{oid}`")

        sudos = list(sudo_collection.find())
        lines.append("\n🛡 **Sudo Admins**")
        if not sudos:
            lines.append("• No sudo admins added yet.\n\n💡 Owner can add via `/addsudo <id/@user>`")
        else:
            for i, s in enumerate(sudos, 1):
                uname = f"@{s['username']}" if s.get("username") else "No username"
                lines.append(f"{i}. **{s.get('name', 'Admin')}** ({uname}) — `{s['user_id']}`")

        await message.reply_text("\n".join(lines))

    # ==============================================
    # /ping (Admin Only)
    # ==============================================
    @app.on_message(filters.command("ping") & admin_filter)
    async def ping_cmd(client: Client, message: Message):
        t0 = time.perf_counter()
        sent = await message.reply_text("🏓 Pinging...")
        bot_ms = (time.perf_counter() - t0) * 1000

        db_t0 = time.perf_counter()
        try:
            mongo_client.admin.command("ping")
            db_ms = f"{(time.perf_counter() - db_t0) * 1000:.1f} ms"
        except Exception:
            db_ms = "Error"

        uptime = _format_uptime(time.time() - BOT_START_TIME)
        await sent.edit_text(
            "🏓 **Pong!**\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"├ ⚡ **Bot Latency:** `{bot_ms:.1f} ms`\n"
            f"├ 🗄 **MongoDB Ping:** `{db_ms}`\n"
            f"└ ⏱ **Uptime:** `{uptime}`"
        )

    # ==============================================
    # /btstats (Bot System Stats — Admin Only)
    # ==============================================
    @app.on_message(filters.command("btstats") & admin_filter)
    async def btstats_cmd(client: Client, message: Message):
        uptime = _format_uptime(time.time() - BOT_START_TIME)

        db_t0 = time.perf_counter()
        try:
            mongo_client.admin.command("ping")
            db_ms = f"{(time.perf_counter() - db_t0) * 1000:.1f} ms"
        except Exception:
            db_ms = "Offline"

        total_users = users_collection.count_documents({})
        total_packs = packs_collection.count_documents({})
        total_fsub = len(fsub._channels())
        total_sudos = sudo_collection.count_documents({})

        # System resource stats (optional psutil)
        cpu_line = "N/A"
        ram_line = "N/A"
        try:
            import psutil
            proc = psutil.Process(os.getpid())
            mem_mb = proc.memory_info().rss / (1024 * 1024)
            sys_mem = psutil.virtual_memory()
            cpu_pct = psutil.cpu_percent(interval=0.2)
            cpu_line = f"{cpu_pct:.1f}%"
            ram_line = f"{mem_mb:.1f} MB (System: {sys_mem.percent}%)"
        except ImportError:
            pass

        text = (
            "🤖 **BOT SYSTEM STATS**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "⚙️ **Environment**\n"
            f"├ 🖥 OS: `{platform.system()} {platform.release()}`\n"
            f"├ 🐍 Python: `{platform.python_version()}`\n"
            f"├ 🔥 Pyrogram: `{pyrogram.__version__}`\n"
            f"├ 🧠 CPU: `{cpu_line}`\n"
            f"├ 💾 RAM: `{ram_line}`\n"
            f"└ ⏱ Uptime: `{uptime}`\n\n"
            "📊 **Database & Counts**\n"
            f"├ 🗄 MongoDB Ping: `{db_ms}`\n"
            f"├ 👥 Total Users: `{total_users}`\n"
            f"├ 📦 Total Packs: `{total_packs}`\n"
            f"├ 📢 Force-Sub Channels: `{total_fsub}`\n"
            f"└ 🛡 Sudo Admins: `{total_sudos}`"
        )
        await message.reply_text(text)

    # ==============================================
    # /bc (Copy Broadcast) & /fbc (Forward Broadcast)
    # ==============================================
    async def _run_broadcast(client: Client, message: Message, forward: bool):
        r = message.reply_to_message
        parts = message.text.split(maxsplit=1)
        text_arg = parts[1].strip() if len(parts) > 1 else ""

        if forward and not r:
            await message.reply_text("❌ Reply to any message with `/fbc` to forward-broadcast it to all users.")
            return
        if not forward and not r and not text_arg:
            await message.reply_text(
                "❌ **Usage:**\n"
                "• Reply to any message with `/bc` (copies without forward tag)\n"
                "• Or `/bc Your announcement text`"
            )
            return

        user_docs = list(users_collection.find({}, {"user_id": 1}))
        group_docs = list(db["bot_known_chats"].find({}, {"chat_id": 1, "type": 1, "status": 1}))

        targets = []
        seen_ids = set()
        user_count = 0
        group_count = 0

        for doc in user_docs:
            try:
                uid = int(doc["user_id"])
                if uid not in seen_ids:
                    seen_ids.add(uid)
                    targets.append((uid, False))
                    user_count += 1
            except Exception:
                continue

        for gdoc in group_docs:
            try:
                gid = int(gdoc["chat_id"])
                ctype = str(gdoc.get("type", "")).lower()
                cstatus = str(gdoc.get("status", "")).lower()
                # Only include groups/supergroups where bot wasn't kicked/left
                if "channel" in ctype or "kicked" in cstatus or "left" in cstatus or "banned" in cstatus:
                    continue
                if gid < 0 and gid not in seen_ids:
                    seen_ids.add(gid)
                    targets.append((gid, True))
                    group_count += 1
            except Exception:
                continue

        total = len(targets)
        if total == 0:
            await message.reply_text("❌ No users or groups found in database.")
            return

        mode_label = "Forward Broadcast (/fbc)" if forward else "Copy Broadcast (/bc)"
        status_msg = await message.reply_text(
            f"📢 **Starting {mode_label}...**\n\n"
            f"👥 Users: `{user_count}` | 👥 Groups: `{group_count}`\n"
            f"⏳ Progress: `0 / {total}`"
        )

        sent = 0
        failed = 0
        pinned = 0
        t0 = time.time()

        for idx, (cid, is_group) in enumerate(targets, 1):
            sent_msg = None
            for _attempt in range(2):
                try:
                    if forward:
                        sent_msg = await r.forward(cid)
                    elif r:
                        sent_msg = await r.copy(cid)
                    else:
                        sent_msg = await client.send_message(cid, text_arg)
                    break
                except FloodWait as fw:
                    await asyncio.sleep(float(fw.value) + 0.5)
                except Exception:
                    break

            if sent_msg is not None:
                sent += 1
                if is_group:
                    try:
                        await sent_msg.pin(disable_notification=True)
                        pinned += 1
                    except Exception:
                        pass  # Bot doesn't have pin permission in this group
            else:
                failed += 1

            if idx % 25 == 0 or idx == total:
                try:
                    await status_msg.edit_text(
                        f"📢 **{mode_label} in Progress...**\n\n"
                        f"⏳ Progress: `{idx} / {total}`\n"
                        f"✅ Sent: `{sent}` | 📌 Pinned: `{pinned}` | ❌ Failed: `{failed}`"
                    )
                except Exception:
                    pass

            await asyncio.sleep(0.05)

        elapsed = _format_uptime(time.time() - t0)
        await status_msg.edit_text(
            f"✅ **{mode_label} Completed!**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"├ 👥 Target Users: `{user_count}` | Groups: `{group_count}`\n"
            f"├ ✅ Delivered: `{sent}`\n"
            f"├ 📌 Auto-Pinned (Groups): `{pinned}`\n"
            f"├ ❌ Failed/Blocked: `{failed}`\n"
            f"└ ⏱ Time Taken: `{elapsed}`"
        )

    @app.on_message(filters.command("bc") & admin_filter)
    async def copy_broadcast_cmd(client: Client, message: Message):
        await _run_broadcast(client, message, forward=False)

    @app.on_message(filters.command("fbc") & admin_filter)
    async def forward_broadcast_cmd(client: Client, message: Message):
        await _run_broadcast(client, message, forward=True)

    # ==============================================
    # /restart (Admin Only)
    # ==============================================
    @app.on_message(filters.command("restart") & admin_filter)
    async def restart_cmd(client: Client, message: Message):
        await message.reply_text("🔄 **Restarting bot...**\nPlease wait a few seconds.")
        await asyncio.sleep(0.8)
        if os.name == "nt":
            # Wait 1.5s in helper so current process releases sticker_bot.session SQLite lock first
            relaunch_cmd = f"import time, subprocess; time.sleep(1.5); subprocess.Popen({([sys.executable] + sys.argv)!r})"
            subprocess.Popen([sys.executable, "-c", relaunch_cmd])
            os._exit(0)
        else:
            os.execv(sys.executable, [sys.executable] + sys.argv)

    # Register in /adminhelp
    register_admin_command("addsudo", "Add a sudo admin by reply, ID, or @username (Owner only).")
    register_admin_command("rmsudo", "Remove a sudo admin (Owner only; aliases: /delsudo, /removesudo).")
    register_admin_command("sudolist", "List bot owner and all sudo admins (alias: /admins).")
    register_admin_command("bc", "Copy-broadcast a message or replied media to all users.")
    register_admin_command("fbc", "Forward-broadcast a replied message to all users.")
    register_admin_command("ping", "Check bot and MongoDB latency + uptime.")
    register_admin_command("btstats", "View bot system stats (CPU, RAM, OS, DB, counts).")
    register_admin_command("restart", "Restart the bot process.")
