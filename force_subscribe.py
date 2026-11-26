# -*- coding: utf-8 -*-
"""
force_subscribe.py
===================
Universal "Must Join" / Force-Subscribe module for Pyrogram bots.
Supports single and MULTIPLE required channels/groups.

COMMANDS (Admin only):
- /addchannel (alias: /addch)    : Add one or multiple channels
- /removechannel (aliases: /delchannel, /rmchannel, /remove) : Remove one, multiple, or 'all' channels
- /listch (aliases: /listchannel, /channels) : List all required channels & bot admin status
- /mychats                      : List chats where the bot is currently admin
- /getid                        : Get chat ID and ref of current channel/group
- /fsubdebug                    : Check subscription status for yourself or a user
"""

import os
import re
from datetime import datetime
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ChatMemberUpdated
)
from pyrogram.errors import UserNotParticipant, RPCError

# Recognized Telegram links
_INVITE_LINK_RE = re.compile(r'(?:https?://)?(?:www\.)?t\.me/(?:\+|joinchat/)[A-Za-z0-9_-]+', re.IGNORECASE)
_USERNAME_LINK_RE = re.compile(r'(?:https?://)?(?:www\.)?t\.me/([A-Za-z]\w{3,31})/?$', re.IGNORECASE)
_BARE_USERNAME_RE = re.compile(r'^[A-Za-z]\w{3,31}$')


class ForceSubscribe:
    def __init__(self, app, db, admin_ids, collection_name="force_sub_channels",
                 env_var="FORCE_SUB_CHANNELS"):
        """
        app          : your pyrogram Client instance
        db           : your pymongo database handle
        admin_ids    : list[int] of Telegram user IDs allowed to manage channels
        """
        if not admin_ids:
            raise ValueError("ForceSubscribe requires at least one admin_id.")

        self.app = app
        self.db = db
        self.col = db[collection_name]

        cleaned_ids = []
        for a in admin_ids:
            try:
                cleaned_ids.append(int(a))
            except (TypeError, ValueError):
                continue

        if not cleaned_ids:
            raise ValueError("ForceSubscribe requires at least one valid numeric admin_id.")
        self.admin_ids = cleaned_ids

        # Ensure index on ref
        try:
            self.col.create_index("ref", unique=True)
        except Exception:
            pass

        # Tracks chats where bot is member/admin for /mychats
        self.chats_col = db["bot_known_chats"]
        try:
            self.chats_col.create_index("chat_id", unique=True)
        except Exception:
            pass

        self._seed_from_env(env_var)
        self._register_admin_commands()
        self._register_recheck_callback()
        self._register_chat_tracking()
        self._register_utility_commands()

    # ------------------------------------------------------------------
    # SETUP & CLASSIFICATION HELPERS
    # ------------------------------------------------------------------
    def _seed_from_env(self, env_var):
        """Seed initial channels from environment variable (supports comma or space separated)."""
        raw = os.getenv(env_var, "").strip()
        if not raw:
            return
        entries = [e.strip() for e in re.split(r'[,;\s\n]+', raw) if e.strip()]
        for entry in entries:
            kind, normalized = self._classify_ref(entry)
            ref_to_store = normalized if kind else entry
            try:
                self.col.update_one(
                    {"ref": ref_to_store},
                    {"$setOnInsert": {
                        "ref": ref_to_store,
                        "title": entry,
                        "invite_link": None,
                        "link_type": kind or "unknown",
                        "join_mode": "direct",
                        "button_text": None,
                        "added_by": "env",
                        "created_at": datetime.utcnow()
                    }},
                    upsert=True
                )
            except Exception:
                pass

    @staticmethod
    def _to_client_ref(ref: str):
        """Convert a stored ref string to what Pyrogram expects: int ID or str username."""
        stripped = str(ref).lstrip("-")
        if stripped.isdigit():
            return int(ref)
        return str(ref)

    @staticmethod
    def _classify_ref(raw: str):
        """
        Classifies channel reference.
        Returns (kind, normalized_value):
          "invite_link" -> raw https://t.me/+... link
          "username"    -> "@username"
          "chat_id"     -> "-1001234567890"
        """
        raw = raw.strip()
        if not raw:
            return None, raw

        if _INVITE_LINK_RE.search(raw):
            return "invite_link", raw

        m = _USERNAME_LINK_RE.match(raw)
        if m:
            return "username", f"@{m.group(1)}"

        stripped = raw.lstrip("-")
        if stripped.isdigit():
            return "chat_id", raw

        if raw.startswith("@") and _BARE_USERNAME_RE.match(raw[1:]):
            return "username", raw

        if _BARE_USERNAME_RE.match(raw):
            return "username", f"@{raw}"

        return None, raw

    def _channels(self):
        """Return list of all configured channel documents."""
        return list(self.col.find())

    # ------------------------------------------------------------------
    # PUBLIC API METHODS (backward & programmatic compatibility)
    # ------------------------------------------------------------------
    async def get_channels(self) -> list:
        """Returns list of all channel documents."""
        return self._channels()

    async def add_channel(self, ref: str | int, title: str = None, join_mode: str = "direct", button_text: str = None) -> bool:
        """Programmatically add a channel."""
        try:
            res = await self._add_single_channel_internal(
                self.app, str(ref), title_override=title, join_mode=join_mode,
                button_text=button_text, added_by="code"
            )
            return res.get("status") in ("added", "already_exists")
        except Exception as e:
            print(f"[force_subscribe] Error in add_channel: {e}")
            return False

    async def remove_channel(self, ref: str | int) -> bool:
        """Programmatically remove a channel."""
        try:
            res = self._remove_single_channel_internal(str(ref))
            return res.get("status") == "removed"
        except Exception as e:
            print(f"[force_subscribe] Error in remove_channel: {e}")
            return False

    # ------------------------------------------------------------------
    # CORE CHECK: Multi-Channel Force-Subscribe
    # ------------------------------------------------------------------
    async def check(self, client, message_or_callback) -> bool:
        """
        Checks whether the user has joined all required channels.
        Returns True  -> User has joined all required channels.
        Returns False -> User is missing one or more channels (prompt shown).
        """
        user = message_or_callback.from_user
        if not user:
            return True

        uid = user.id
        if uid in self.admin_ids:
            return True

        channels = self._channels()
        if not channels:
            return True

        not_joined = []
        for ch in channels:
            client_ref = self._to_client_ref(ch["ref"])
            try:
                member = await client.get_chat_member(client_ref, uid)
                if member.status in ("kicked", "banned", "left"):
                    not_joined.append(ch)
            except UserNotParticipant:
                not_joined.append(ch)
            except Exception as e:
                # If bot cannot check (e.g. not admin), log and skip so users aren't locked out
                print(f"[force_subscribe] Could not verify membership for '{ch['ref']}' (uid={uid}): {type(e).__name__}: {e}")
                continue

        if not not_joined:
            return True

        # User is missing 1 or more channels: build join prompt with buttons
        buttons = []
        for ch in not_joined:
            link = await self._get_invite_link(client, ch)
            if not link:
                continue
            title = ch.get("title") or ch["ref"]
            custom_text = ch.get("button_text")
            if custom_text:
                label = custom_text
            elif ch.get("join_mode") == "request":
                label = f"🔒 Request to Join: {title}"
            else:
                label = f"➡️ Join: {title}"
            buttons.append([InlineKeyboardButton(label, url=link)])

        buttons.append([InlineKeyboardButton("✅ I've Joined", callback_data="fsub_recheck")])

        missing_list_str = "\n".join([f"• **{ch.get('title') or ch['ref']}**" for ch in not_joined])
        text = (
            "🔒 **Channel Membership Required**\n\n"
            "To use this bot, you must join our required channel(s) below:\n\n"
            f"{missing_list_str}\n\n"
            "👉 Please tap the button(s) below to join, then click **✅ I've Joined** to continue."
        )
        markup = InlineKeyboardMarkup(buttons)

        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.answer(
                "❌ You have not joined all required channels yet! Please join all of them first.",
                show_alert=True
            )
            try:
                await message_or_callback.message.edit_text(text, reply_markup=markup)
            except Exception:
                pass
        else:
            await message_or_callback.reply_text(text, reply_markup=markup)

        return False

    async def _build_invite_link(self, client, chat, join_mode, prefer_public=True, fallback_link=None):
        """Generate appropriate invite link based on join mode and chat settings."""
        if join_mode == "request":
            try:
                invite = await client.create_chat_invite_link(chat.id, creates_join_request=True)
                return invite.invite_link
            except Exception:
                return fallback_link

        if prefer_public and getattr(chat, "username", None):
            return f"https://t.me/{chat.username}"

        try:
            return await client.export_chat_invite_link(chat.id)
        except Exception:
            try:
                invite = await client.create_chat_invite_link(chat.id)
                return invite.invite_link
            except Exception:
                return fallback_link

    async def _get_invite_link(self, client, ch):
        """Retrieve or lazily generate/cache an invite link for a channel document."""
        if ch.get("invite_link"):
            return ch["invite_link"]

        client_ref = self._to_client_ref(ch["ref"])
        try:
            chat = await client.get_chat(client_ref)
            link = await self._build_invite_link(client, chat, ch.get("join_mode", "direct"))
            if link:
                self.col.update_one(
                    {"ref": ch["ref"]},
                    {"$set": {"invite_link": link, "title": chat.title or ch["ref"]}}
                )
            return link
        except Exception as e:
            print(f"[force_subscribe] Error fetching invite link for {ch.get('ref')}: {e}")
            if str(ch.get("ref", "")).startswith("@"):
                return f"https://t.me/{ch['ref'][1:]}"
            return None

    # ------------------------------------------------------------------
    # RECHECK CALLBACK
    # ------------------------------------------------------------------
    def _register_recheck_callback(self):
        @self.app.on_callback_query(filters.regex(r"^fsub_recheck$"))
        async def recheck(client, callback: CallbackQuery):
            ok = await self.check(client, callback)
            if ok:
                await callback.answer("✅ Verification successful! Welcome!", show_alert=False)
                try:
                    await callback.message.edit_text(
                        "✅ **Thank you for joining!**\n\n"
                        "Your channel membership has been verified. You can now use all bot features!\n\n"
                        "Tap /start or /sticker to begin creating stickers."
                    )
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # INTERNAL CHANNEL ADD & REMOVE LOGIC
    # ------------------------------------------------------------------
    async def _add_single_channel_internal(self, client, ref_input, title_override=None,
                                          username_override=None, join_mode="direct",
                                          button_text=None, added_by="admin"):
        """Adds a single channel to MongoDB, resolving chat details and verifying bot admin rights."""
        kind, normalized = self._classify_ref(ref_input)
        if kind is None:
            return {"status": "error", "ref": ref_input, "error": "Invalid channel reference format"}

        # Early check for duplicate
        existing = self.col.find_one({"ref": normalized})
        if existing:
            return {
                "status": "already_exists",
                "ref": normalized,
                "title": existing.get("title", normalized),
                "join_mode": existing.get("join_mode", "direct")
            }

        chat_id = None
        username = username_override
        resolved_title = title_override
        invite_link = None
        is_admin = False
        ref = normalized

        if kind == "invite_link":
            try:
                chat = await client.join_chat(normalized)
                chat_id = chat.id
                ref = str(chat.id)
                username = chat.username
                resolved_title = resolved_title or chat.title or str(chat.id)
                invite_link = await self._build_invite_link(
                    client, chat, join_mode, prefer_public=False, fallback_link=normalized
                )
                try:
                    me = client.me if getattr(client, "me", None) else await client.get_me()
                    m = await client.get_chat_member(chat.id, me.id)
                    is_admin = m.status in ("administrator", "creator")
                except Exception:
                    is_admin = False
            except Exception as e:
                err_str = str(e)
                if "USER_ALREADY_PARTICIPANT" in err_str:
                    return {
                        "status": "error",
                        "ref": ref_input,
                        "error": "Bot is already in this chat. Please use the chat's numeric ID (e.g. -100...) or run /getid inside it."
                    }
                return {"status": "error", "ref": ref_input, "error": err_str}
        else:
            client_ref = self._to_client_ref(ref)
            try:
                chat = await client.get_chat(client_ref)
                chat_id = chat.id
                username = username or chat.username
                resolved_title = resolved_title or chat.title or (f"@{chat.username}" if chat.username else str(chat.id))
                ref = f"@{chat.username}" if chat.username else str(chat.id)
                invite_link = await self._build_invite_link(client, chat, join_mode)

                try:
                    me = client.me if getattr(client, "me", None) else await client.get_me()
                    m = await client.get_chat_member(chat.id, me.id)
                    is_admin = m.status in ("administrator", "creator")
                except Exception:
                    is_admin = False
            except Exception as e:
                resolved_title = resolved_title or normalized
                is_admin = False
                if kind == "username":
                    invite_link = f"https://t.me/{normalized.lstrip('@')}"

        # Deduplicate by chat_id or ref
        dup_query = [{"ref": ref}]
        if chat_id:
            dup_query.append({"chat_id": chat_id})
        existing = self.col.find_one({"$or": dup_query})
        if existing:
            return {
                "status": "already_exists",
                "ref": existing.get("ref", ref),
                "title": existing.get("title", ref),
                "join_mode": existing.get("join_mode", "direct")
            }

        doc = {
            "ref": ref,
            "chat_id": chat_id,
            "title": resolved_title or ref,
            "username": username,
            "invite_link": invite_link,
            "link_type": kind,
            "join_mode": join_mode,
            "button_text": button_text,
            "is_admin": is_admin,
            "added_by": str(added_by),
            "created_at": datetime.utcnow()
        }
        self.col.update_one({"ref": ref}, {"$set": doc}, upsert=True)

        return {
            "status": "added",
            "ref": ref,
            "title": resolved_title or ref,
            "is_admin": is_admin,
            "join_mode": join_mode,
            "button_text": button_text,
            "chat_id": chat_id
        }

    def _remove_single_channel_internal(self, target: str):
        """Removes a channel by ref, chat_id, username, or invite link."""
        target = target.strip()
        query = {
            "$or": [
                {"ref": target},
                {"ref": f"@{target}" if not target.startswith("@") else target},
                {"username": target.lstrip("@")},
                {"invite_link": target}
            ]
        }
        stripped = target.lstrip("-")
        if stripped.isdigit():
            query["$or"].append({"chat_id": int(target)})
            query["$or"].append({"ref": str(target)})

        doc = self.col.find_one(query)
        if doc:
            self.col.delete_one({"_id": doc["_id"]})
            return {"status": "removed", "ref": doc.get("ref"), "title": doc.get("title", doc.get("ref"))}
        return {"status": "not_found", "ref": target}

    # ------------------------------------------------------------------
    # ADMIN COMMANDS: /addchannel, /removechannel, /listch
    # ------------------------------------------------------------------
    def _register_admin_commands(self):
        admin_cmd_filter = (filters.private | filters.group)

        # --------------------------------------------------------------
        # /addchannel and /addch
        # --------------------------------------------------------------
        @self.app.on_message(filters.command(["addchannel", "addch"]) & admin_cmd_filter)
        async def add_channel_cmd(client: Client, message: Message):
            if not message.from_user or message.from_user.id not in self.admin_ids:
                await message.reply_text("🚫 Unauthorized. This command is for the Bot Owner and Admins only.")
                return

            raw_args = message.text.split(maxsplit=1)
            if len(raw_args) < 2:
                await message.reply_text(
                    "📢 **How to Use /addchannel**\n"
                    "━━━━━━━━━━━━━━━━━━\n\n"
                    "**1. Add a single channel:**\n"
                    "• `/addchannel @MyChannel`\n"
                    "• `/addchannel -1001234567890`\n"
                    "• `/addchannel https://t.me/MyChannel`\n\n"
                    "**2. Add MULTIPLE channels at once:**\n"
                    "• `/addchannel @chan1 @chan2 @chan3`\n"
                    "• `/addchannel @chan1, @chan2, -1001234567890`\n\n"
                    "**3. Advanced options (for single channel):**\n"
                    "• `/addchannel @MyChannel --request` (Requires join request approval)\n"
                    "• `/addchannel @MyChannel --button=\"🔥 Join VIP\" Custom Title`\n\n"
                    "💡 Use `/create` for a step-by-step interactive wizard.\n\n"
                    "⚠️ **Make sure the bot is an Admin in the channel(s) so it can verify users!**"
                )
                return

            remainder = raw_args[1].strip()

            # Extract optional flags
            join_mode = "direct"
            if re.search(r'(?:^|\s)--request(?:\s|$)', remainder):
                join_mode = "request"
                remainder = re.sub(r'(?:^|\s)--request(?:\s|$)', ' ', remainder).strip()

            button_text = None
            btn_match = re.search(r'--button=("([^"]+)"|(\S+))', remainder)
            if btn_match:
                button_text = btn_match.group(2) or btn_match.group(3)
                remainder = (remainder[:btn_match.start()] + remainder[btn_match.end():]).strip()

            has_flags = (join_mode == "request" or button_text is not None)

            # Determine if this is a single channel configuration or multi-channel
            if has_flags:
                # Single channel with flags/title
                parts = remainder.split(maxsplit=1)
                ref_input = parts[0].strip()
                title_override = parts[1].strip() if len(parts) > 1 else None

                status_msg = await message.reply_text(f"⏳ Verifying and adding `{ref_input}`...")
                res = await self._add_single_channel_internal(
                    client, ref_input, title_override=title_override,
                    join_mode=join_mode, button_text=button_text,
                    added_by=message.from_user.id
                )

                if res.get("status") == "added":
                    admin_warn = "" if res.get("is_admin") else "\n\n⚠️ **Warning:** Bot is NOT an admin in this channel! Please promote the bot to Admin so it can verify user subscriptions."
                    mode_label = "🔒 Request to Join" if join_mode == "request" else "✅ Direct Join"
                    btn_label = f"\nButton text: {button_text}" if button_text else ""
                    await status_msg.edit_text(
                        f"✅ **Channel added successfully!**\n\n"
                        f"📢 **Title:** {res['title']}\n"
                        f"📌 **Ref:** `{res['ref']}`\n"
                        f"⚙️ **Mode:** {mode_label}{btn_label}{admin_warn}"
                    )
                elif res.get("status") == "already_exists":
                    await status_msg.edit_text(f"ℹ️ Channel `{res['ref']}` ({res['title']}) is already in the must-join list.")
                else:
                    await status_msg.edit_text(f"❌ Failed to add `{res['ref']}`:\n{res.get('error')}")
                return

            # No flags: check for multiple channels or comma-separated items
            if "," in remainder or "\n" in remainder:
                items = [i.strip() for i in re.split(r'[,;\n]+', remainder) if i.strip()]
            else:
                tokens = remainder.split()
                # Check if generated by /create wizard: token 0 is chat_id (-100...), token 1 is @username
                if len(tokens) >= 2 and tokens[0].lstrip("-").isdigit() and tokens[1].startswith("@"):
                    ref_input = tokens[0]
                    uname_ovr = tokens[1]
                    title_ovr = " ".join(tokens[2:]).strip() if len(tokens) > 2 else None
                    status_msg = await message.reply_text(f"⏳ Adding `{ref_input}`...")
                    res = await self._add_single_channel_internal(
                        client, ref_input, title_override=title_ovr,
                        username_override=uname_ovr, join_mode=join_mode,
                        button_text=button_text, added_by=message.from_user.id
                    )
                    if res.get("status") == "added":
                        admin_warn = "" if res.get("is_admin") else "\n\n⚠️ **Warning:** Bot is NOT an admin in this channel!"
                        await status_msg.edit_text(
                            f"✅ **Channel added successfully!**\n\n"
                            f"📢 **Title:** {res['title']}\n"
                            f"📌 **Ref:** `{res['ref']}`{admin_warn}"
                        )
                    elif res.get("status") == "already_exists":
                        await status_msg.edit_text(f"ℹ️ Channel `{res['ref']}` is already in the must-join list.")
                    else:
                        await status_msg.edit_text(f"❌ Failed to add `{res['ref']}`: {res.get('error')}")
                    return

                # If all tokens look like channel references (start with @, -, digit, or t.me)
                all_refs = all(
                    t.startswith("@") or t.startswith("-") or t.isdigit() or "t.me" in t
                    for t in tokens
                )
                if all_refs and len(tokens) > 1:
                    items = tokens
                elif len(tokens) == 1:
                    items = tokens
                else:
                    # Single channel with custom title: token 0 is ref, tokens 1.. are title
                    ref_input = tokens[0]
                    title_override = " ".join(tokens[1:]).strip()
                    status_msg = await message.reply_text(f"⏳ Adding `{ref_input}`...")
                    res = await self._add_single_channel_internal(
                        client, ref_input, title_override=title_override,
                        join_mode=join_mode, button_text=button_text,
                        added_by=message.from_user.id
                    )
                    if res.get("status") == "added":
                        admin_warn = "" if res.get("is_admin") else "\n\n⚠️ **Warning:** Bot is NOT an admin in this channel!"
                        await status_msg.edit_text(
                            f"✅ **Channel added successfully!**\n\n"
                            f"📢 **Title:** {res['title']}\n"
                            f"📌 **Ref:** `{res['ref']}`{admin_warn}"
                        )
                    elif res.get("status") == "already_exists":
                        await status_msg.edit_text(f"ℹ️ Channel `{res['ref']}` is already in the list.")
                    else:
                        await status_msg.edit_text(f"❌ Failed to add `{res['ref']}`: {res.get('error')}")
                    return

            # Process multiple channels
            status_msg = await message.reply_text(f"⏳ Processing {len(items)} channel(s)...")
            results = []
            has_admin_warning = False

            for item in items:
                res = await self._add_single_channel_internal(
                    client, item, join_mode=join_mode, button_text=button_text,
                    added_by=message.from_user.id
                )
                results.append(res)
                if res.get("status") == "added" and not res.get("is_admin"):
                    has_admin_warning = True

            report_lines = ["📢 **Channels Add Results**\n━━━━━━━━━━━━━━━━━━"]
            for r in results:
                st = r.get("status")
                ref = r.get("ref")
                title = r.get("title", ref)
                if st == "added":
                    admin_tag = "👑 Admin" if r.get("is_admin") else "⚠️ Not Admin"
                    report_lines.append(f"✅ Added: **{title}** (`{ref}`) — [{admin_tag}]")
                elif st == "already_exists":
                    report_lines.append(f"ℹ️ Already in list: **{title}** (`{ref}`)")
                else:
                    report_lines.append(f"❌ Failed: `{ref}` — {r.get('error')}")

            total_now = len(self._channels())
            report_lines.append(f"\n📊 **Total Required Channels:** {total_now}")

            if has_admin_warning:
                report_lines.append("\n⚠️ **Note:** For channels marked [⚠️ Not Admin], promote the bot to Admin so it can verify subscribers.")

            report_lines.append("\n💡 Use `/listch` to view all active channels.")
            await status_msg.edit_text("\n".join(report_lines))

        # --------------------------------------------------------------
        # /removechannel (and aliases: /delchannel, /rmchannel, /remove)
        # --------------------------------------------------------------
        @self.app.on_message(filters.command(["removechannel", "delchannel", "rmchannel", "remchannel", "remove"]) & admin_cmd_filter)
        async def remove_channel_cmd(client: Client, message: Message):
            if not message.from_user or message.from_user.id not in self.admin_ids:
                await message.reply_text("🚫 Unauthorized. This command is for the Bot Owner and Admins only.")
                return

            raw_args = message.text.split(maxsplit=1)
            if len(raw_args) < 2:
                await message.reply_text(
                    "📢 **How to Use /removechannel**\n"
                    "━━━━━━━━━━━━━━━━━━\n\n"
                    "**1. Remove a single channel:**\n"
                    "• `/removechannel @MyChannel`\n"
                    "• `/removechannel -1001234567890`\n"
                    "• `/removechannel 1` (By number from /listch)\n\n"
                    "**2. Remove MULTIPLE channels at once:**\n"
                    "• `/removechannel @chan1 @chan2 @chan3`\n"
                    "• `/removechannel 1 2 3`\n\n"
                    "**3. Remove ALL channels:**\n"
                    "• `/removechannel all`\n\n"
                    "💡 Use `/listch` to view the list of channels and their numbers."
                )
                return

            remainder = raw_args[1].strip()

            # Handle "/remove channel @mychannel" or "/remove channels @mychannel"
            if remainder.lower().startswith("channel "):
                remainder = remainder[8:].strip()
            elif remainder.lower().startswith("channels "):
                remainder = remainder[9:].strip()

            if not remainder or remainder.lower() in ("channel", "channels"):
                await message.reply_text("❌ Please specify which channel to remove, e.g. `/removechannel @MyChannel` or `/removechannel all`.")
                return

            # Clear all channels
            if remainder.lower() == "all":
                deleted_count = self.col.delete_many({}).deleted_count
                await message.reply_text(f"🗑 **All force-subscribe channels have been removed!**\n\nRemoved: {deleted_count} channel(s).\nUsers are no longer required to join any channel.")
                return

            # Parse targets (support spaces, commas, newlines)
            tokens = [t.strip() for t in re.split(r'[,;\s\n]+', remainder) if t.strip()]
            channels_snapshot = self._channels()
            results = []

            for token in tokens:
                # Check if token is a number corresponding to an index in /listch
                if token.isdigit() and 1 <= int(token) <= len(channels_snapshot):
                    idx = int(token) - 1
                    target_doc = channels_snapshot[idx]
                    self.col.delete_one({"_id": target_doc["_id"]})
                    results.append({
                        "status": "removed",
                        "ref": target_doc.get("ref"),
                        "title": target_doc.get("title", target_doc.get("ref"))
                    })
                else:
                    res = self._remove_single_channel_internal(token)
                    results.append(res)

            report_lines = ["🗑 **Channels Remove Results**\n━━━━━━━━━━━━━━━━━━"]
            for r in results:
                if r.get("status") == "removed":
                    report_lines.append(f"✅ Removed: **{r.get('title')}** (`{r.get('ref')}`)")
                else:
                    report_lines.append(f"❌ Not found: `{r.get('ref')}`")

            remaining_count = len(self._channels())
            report_lines.append(f"\n📊 **Remaining Required Channels:** {remaining_count}")
            report_lines.append("💡 Use `/listch` to view the current list.")
            await message.reply_text("\n".join(report_lines))

        # --------------------------------------------------------------
        # /listch (and aliases: /listchannel, /listchannels, /channels)
        # --------------------------------------------------------------
        @self.app.on_message(filters.command(["listch", "listchannel", "listchannels", "channels"]) & admin_cmd_filter)
        async def list_channels_cmd(client: Client, message: Message):
            if not message.from_user or message.from_user.id not in self.admin_ids:
                await message.reply_text("🚫 Unauthorized. This command is for the Bot Owner and Admins only.")
                return

            channels = self._channels()
            if not channels:
                await message.reply_text(
                    "📢 **Force-Subscribe Channels**\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "No channels are currently configured.\n\n"
                    "💡 **How to add channels:**\n"
                    "• Single: `/addchannel @MyChannel`\n"
                    "• Multiple: `/addchannel @chan1 @chan2 @chan3`\n"
                    "• Interactive Wizard: `/create`"
                )
                return

            status_msg = await message.reply_text("⏳ Checking live channel statuses...")

            lines = ["📢 **Force-Subscribe Required Channels**\n━━━━━━━━━━━━━━━━━━"]
            for i, c in enumerate(channels, start=1):
                title = c.get("title") or c.get("ref")
                ref = c.get("ref")
                mode = "🔒 Request to Join" if c.get("join_mode") == "request" else "✅ Direct Join"
                btn_str = f" | Button: \"{c['button_text']}\"" if c.get("button_text") else ""

                # Live admin status check
                admin_status = "👑 Admin"
                try:
                    client_ref = self._to_client_ref(ref)
                    me = client.me if getattr(client, "me", None) else await client.get_me()
                    m = await client.get_chat_member(client_ref, me.id)
                    if m.status not in ("administrator", "creator"):
                        admin_status = "⚠️ NOT Admin"
                except Exception:
                    admin_status = "⚠️ Cannot verify (promote bot!)"

                lines.append(
                    f"**{i}. {title}**\n"
                    f"   📌 Ref: `{ref}`\n"
                    f"   ⚙️ Mode: {mode}{btn_str}\n"
                    f"   🛡 Status: {admin_status}"
                )

            lines.append(f"\n📊 **Total Channels:** {len(channels)}")
            lines.append(
                "\n💡 **Quick Actions:**\n"
                "• Add more: `/addchannel @channel1 @channel2`\n"
                "• Remove one: `/removechannel @channel` or `/removechannel <number>`\n"
                "• Remove all: `/removechannel all`"
            )

            await status_msg.edit_text("\n\n".join(lines))

    # ------------------------------------------------------------------
    # CHAT TRACKING (live tracking of bot's admin chats for /mychats)
    # ------------------------------------------------------------------
    def _register_chat_tracking(self):
        @self.app.on_chat_member_updated()
        async def track_self_membership(client, update: ChatMemberUpdated):
            me_id = client.me.id if getattr(client, "me", None) else (await client.get_me()).id
            new = update.new_chat_member
            if not new or not new.user or new.user.id != me_id:
                return

            chat = update.chat
            is_admin = new.status in ("administrator", "creator")
            try:
                self.chats_col.update_one(
                    {"chat_id": chat.id},
                    {"$set": {
                        "chat_id": chat.id,
                        "title": chat.title or chat.first_name or str(chat.id),
                        "username": chat.username,
                        "type": str(chat.type),
                        "status": str(new.status),
                        "is_admin": is_admin,
                        "updated_at": datetime.utcnow()
                    }},
                    upsert=True
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # UTILITY COMMANDS: /mychats, /getid, /fsubdebug
    # ------------------------------------------------------------------
    def _register_utility_commands(self):
        admin_filter = filters.user(self.admin_ids)

        @self.app.on_message(filters.command("mychats") & admin_filter)
        async def my_chats(client, message: Message):
            admin_chats = list(self.chats_col.find({"is_admin": True}))
            if not admin_chats:
                await message.reply_text(
                    "No chats tracked yet where the bot is admin.\n\n"
                    "If the bot was added before tracking started, run `/getid` inside that chat, "
                    "or remove and re-add the bot there."
                )
                return

            lines = []
            for c in admin_chats:
                ref = f"@{c['username']}" if c.get("username") else str(c["chat_id"])
                lines.append(
                    f"• **{c.get('title', ref)}**\n"
                    f"   Type: `{c.get('type')}`  |  Ref for /addchannel: `{ref}`"
                )
            await message.reply_text(
                "🤖 **Chats where the bot is admin:**\n\n" + "\n\n".join(lines) +
                "\n\nCopy the `ref` value straight into `/addchannel <ref>`."
            )

        @self.app.on_message(filters.command("getid") & admin_filter)
        async def get_id(client, message: Message):
            chat = message.chat
            ref = f"@{chat.username}" if chat.username else str(chat.id)
            await message.reply_text(
                f"📌 **Chat Info**\n\n"
                f"Title: **{chat.title or chat.first_name or 'N/A'}**\n"
                f"Type: `{chat.type}`\n"
                f"Chat ID: `{chat.id}`\n"
                f"Username: {'@' + chat.username if chat.username else 'None (private)'}\n\n"
                f"Ref for `/addchannel`: `{ref}`"
            )

        @self.app.on_message(filters.command("fsubdebug") & admin_filter)
        async def fsub_debug(client, message: Message):
            args = message.text.split(maxsplit=1)
            target_uid = message.from_user.id
            if len(args) > 1 and args[1].strip().isdigit():
                target_uid = int(args[1].strip())

            channels = self._channels()
            if not channels:
                await message.reply_text("No must-join channels configured. Add one with /addchannel.")
                return

            lines = [f"🔍 **Debug for User `{target_uid}`:**\n"]
            for ch in channels:
                client_ref = self._to_client_ref(ch["ref"])
                label = ch.get("title") or ch["ref"]
                try:
                    member = await client.get_chat_member(client_ref, target_uid)
                    lines.append(f"• **{label}** (`{ch['ref']}`) → status: `{member.status}`")
                except UserNotParticipant:
                    lines.append(f"• **{label}** (`{ch['ref']}`) → ❌ NOT a participant (blocked)")
                except Exception as e:
                    lines.append(
                        f"• **{label}** (`{ch['ref']}`) → ⚠️ CHECK FAILED ({type(e).__name__}: {e})\n"
                        f"   ↳ Channel is skipped. Ensure bot is admin in this channel."
                    )
            await message.reply_text("\n\n".join(lines))
