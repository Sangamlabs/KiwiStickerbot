# -*- coding: utf-8 -*-
"""
handlers/quote.py
=================
/q, /quote, and /r — turn a replied message into a quote-card sticker (avatar + name + text).
Supports:
- Forwarded messages (uses original sender's name & avatar)
- `/q r` and `/r` to include the replied-to parent message box inside the sticker
"""
import io
import unicodedata

from PIL import Image, ImageDraw, ImageFont
from pyrogram import Client, filters
from pyrogram.types import Message

W = 512  # sticker side that must stay exactly 512
HMAX = 512
CARD_BG = (24, 37, 51, 255)         # Telegram dark bubble background (#182533)
TEXT_C = (255, 255, 255, 255)       # crisp white message text
NAME_C = (100, 181, 246, 255)       # Telegram sky-blue name & reply accent (#64B5F6)
WHITE = (255, 255, 255, 255)
AVATAR_BG = (43, 82, 120, 255)      # fallback avatar circle (#2B5278)
REPLY_BG = (35, 56, 77, 255)        # lighter slate-blue for the quoted reply box (#23384D)

# (bold, regular) pairs, first existing wins — DejaVu on VPS/Linux, Arial on Windows/macOS
_FONT_PAIRS = [
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
     "/System/Library/Fonts/Supplemental/Arial.ttf"),
]


def _fonts():
    for bold, regular in _FONT_PAIRS:
        try:
            return (
                ImageFont.truetype(bold, 30),
                ImageFont.truetype(regular, 34),
                ImageFont.truetype(bold, 22),
                ImageFont.truetype(regular, 24),
            )
        except OSError:
            continue
    d = ImageFont.load_default()
    return d, d, d, d


_NAME_F, _TEXT_F, _REPLY_NAME_F, _REPLY_TEXT_F = _fonts()


def _renderable(text, font):
    """Normalize fancy Unicode math/script letters via NFKC, then drop unrenderable glyphs."""
    try:
        text = unicodedata.normalize("NFKC", text)
        notdef = bytes(font.getmask("\ue010"))
        has_notdef = any(notdef)
        out = []
        for ch in text:
            if ch.isspace():
                out.append(ch)
                continue
            m = bytes(font.getmask(ch))
            if has_notdef and m == notdef:  # font draws a box for it
                continue
            if not any(m) and ord(ch) > 0x2000:  # invisible: missing glyph
                continue
            out.append(ch)
        return "".join(out)
    except Exception:
        return text


def _truncate_px(text, font, max_px):
    text = _renderable(text, font).strip()
    if not text:
        return ""
    if font.getlength(text) <= max_px:
        return text
    while text and font.getlength(text + "…") > max_px:
        text = text[:-1]
    return text.rstrip() + "…"


def _wrap(text, font, max_w):
    """Greedy word wrap; hard-break words wider than the line."""
    draw = ImageDraw.Draw(Image.new("L", (8, 8)))
    lines, line = [], ""
    for word in text.split():
        probe = f"{line} {word}".strip()
        if draw.textlength(probe, font=font) <= max_w:
            line = probe
            continue
        if line:
            lines.append(line)
            line = ""
        while draw.textlength(word, font=font) > max_w:
            cut = len(word)
            while cut > 1 and draw.textlength(word[:cut], font=font) > max_w:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        line = word
    if line:
        lines.append(line)
    return lines


def _circle_avatar(data: bytes, size=96):
    """Circular-crop a downloaded profile photo; returns RGBA or None."""
    try:
        img = Image.open(io.BytesIO(data)).convert("RGBA")
        side = min(img.size)
        img = img.crop((
            (img.width - side) // 2,
            (img.height - side) // 2,
            (img.width + side) // 2,
            (img.height + side) // 2,
        )).resize((size, size))
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
        img.putalpha(mask)
        return img
    except Exception:
        return None


def _dark_bg(w, h):
    """Solid Telegram dark-mode bubble background."""
    return Image.new("RGBA", (w, h), CARD_BG)


def _fallback_avatar(name: str, size=72):
    av = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(av)
    d.ellipse((0, 0, size, size), fill=AVATAR_BG)
    clean = _renderable(name or "?", _NAME_F).strip()
    letter = (clean or "?")[0].upper()
    f = _NAME_F
    for bold, _ in _FONT_PAIRS:  # ~46% of the circle, first font that exists
        try:
            f = ImageFont.truetype(bold, max(18, int(size * 0.46)))
            break
        except OSError:
            continue
    bb = d.textbbox((0, 0), letter, font=f)
    d.text(
        ((size - (bb[2] - bb[0])) // 2 - bb[0], (size - (bb[3] - bb[1])) // 2 - bb[1]),
        letter,
        font=f,
        fill=WHITE,
    )
    return av


def _quote_sticker(
    name: str,
    text: str,
    avatar: bytes = None,
    reply_name: str = None,
    reply_text: str = None,
) -> io.BytesIO:
    pad, av_size = 28, 72

    max_name_px = W - pad - av_size - 16 - pad
    name = _truncate_px(name or "Anonymous", _NAME_F, max_name_px) or "Anonymous"

    has_reply = bool(reply_name and reply_text)
    max_lines = 5 if has_reply else 7

    body = _renderable(text, _TEXT_F)
    all_lines = _wrap(body, _TEXT_F, W - 2 * pad)
    if not all_lines:
        all_lines = ["..."]
    lines = all_lines[:max_lines]
    if len(all_lines) > max_lines:
        lines[-1] = lines[-1].rstrip("…") + "…"

    top = pad + av_size + 20
    reply_h = 68
    reply_gap = 14
    body_y = top + (reply_h + reply_gap if has_reply else 0)

    h = min(HMAX, max(200, body_y + len(lines) * 40 + 22))
    canvas = Image.new("RGBA", (W, h), (0, 0, 0, 0))
    mask = Image.new("L", (W, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, h - 1), radius=28, fill=255)
    canvas.paste(_dark_bg(W, h), (0, 0), mask)

    av = _circle_avatar(avatar, size=av_size) if avatar else None
    if av is None:
        av = _fallback_avatar(name or "?", size=av_size)
    canvas.paste(av, (pad, pad), av)

    d = ImageDraw.Draw(canvas)
    d.text((pad + av_size + 16, pad + 14), name, font=_NAME_F, fill=NAME_C)

    if has_reply:
        overlay = Image.new("RGBA", (W, h), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        rx1, ry1 = pad, top
        rx2, ry2 = W - pad, top + reply_h
        od.rounded_rectangle((rx1, ry1, rx2, ry2), radius=12, fill=REPLY_BG)
        od.rounded_rectangle((rx1, ry1, rx1 + 6, ry2), radius=3, fill=NAME_C)
        canvas = Image.alpha_composite(canvas, overlay)
        d = ImageDraw.Draw(canvas)

        reply_inner_max = (rx2 - rx1) - 24
        r_name = _truncate_px(reply_name, _REPLY_NAME_F, reply_inner_max) or "User"
        r_text = _truncate_px(" ".join(reply_text.split()), _REPLY_TEXT_F, reply_inner_max) or "..."
        d.text((rx1 + 16, ry1 + 8), r_name, font=_REPLY_NAME_F, fill=NAME_C)
        d.text((rx1 + 16, ry1 + 36), r_text, font=_REPLY_TEXT_F, fill=TEXT_C)

    y = body_y
    for ln in lines:
        if y + 36 > h - 10:
            break
        d.text((pad, y), ln, font=_TEXT_F, fill=TEXT_C)
        y += 40

    buf = io.BytesIO()
    canvas.save(buf, format="WEBP", lossless=True)
    buf.seek(0)
    buf.name = "quote.webp"
    return buf


def _is_forwarded(msg: Message) -> bool:
    return bool(msg.forward_from or msg.forward_from_chat or msg.forward_sender_name)


def _extract_sender_name(msg: Message):
    """Prefer original forwarded author when the message is forwarded."""
    if msg.forward_from:
        src = msg.forward_from
    elif msg.forward_from_chat:
        src = msg.forward_from_chat
    elif msg.forward_sender_name:
        return msg.forward_sender_name, None
    else:
        src = msg.from_user or getattr(msg, "sender_chat", None)

    first = getattr(src, "first_name", "") or ""
    last = getattr(src, "last_name", "") or ""
    full_name = f"{first} {last}".strip()
    name = full_name or getattr(src, "title", None) or getattr(src, "username", None) or "Anonymous"
    return name, src


def _extract_msg_preview(msg: Message):
    txt = (msg.text or msg.caption or "").strip()
    if txt:
        return txt
    if msg.sticker:
        return f"{msg.sticker.emoji or ''} Sticker".strip()
    if msg.photo:
        return "📷 Photo"
    if msg.video:
        return "🎥 Video"
    if msg.animation:
        return "🎞 GIF"
    if msg.voice or msg.audio:
        return "🎵 Audio"
    if msg.document:
        return "📎 Document"
    return ""


async def _resolve_parent_reply(client: Client, r: Message):
    """Resolve the replied-to message for `/q r` and `/r`."""
    if r.reply_to_message:
        return r.reply_to_message

    if r.reply_to_message_id:
        try:
            parent = await client.get_messages(r.chat.id, r.reply_to_message_id)
            if parent and not getattr(parent, "empty", False):
                return parent
        except Exception:
            pass

    # If the user forwarded a reply chain into DM, the parent message is often r.id - 1
    if _is_forwarded(r) and r.id > 1:
        try:
            prev = await client.get_messages(r.chat.id, r.id - 1)
            if prev and not getattr(prev, "empty", False) and _is_forwarded(prev):
                if r.date and prev.date and abs((r.date - prev.date).total_seconds()) <= 15:
                    return prev
        except Exception:
            pass

    return None


async def _fetch_avatar(client: Client, src) -> bytes | None:
    """Download the user's or chat's profile photo using multiple fallbacks."""
    if not src:
        return None

    # 1. Try photo directly attached on the User/Chat object (works for forwarded users too)
    photo_obj = getattr(src, "photo", None)
    if photo_obj:
        for attr in ("big_file_id", "small_file_id"):
            fid = getattr(photo_obj, attr, None)
            if fid:
                try:
                    bio = await client.download_media(fid, in_memory=True)
                    if bio:
                        return bio.getvalue()
                except Exception:
                    pass

    uid = getattr(src, "id", None)
    if not uid:
        return None

    # 2. Try get_chat_photos
    try:
        async for p in client.get_chat_photos(uid, limit=1):
            bio = await client.download_media(p.file_id, in_memory=True)
            if bio:
                return bio.getvalue()
            break
    except Exception:
        pass

    # 3. Try fetching full chat/user object
    try:
        chat = await client.get_chat(uid)
        c_photo = getattr(chat, "photo", None)
        if c_photo:
            fid = getattr(c_photo, "big_file_id", None) or getattr(c_photo, "small_file_id", None)
            if fid:
                bio = await client.download_media(fid, in_memory=True)
                if bio:
                    return bio.getvalue()
    except Exception:
        pass

    return None


def register(app, fsub):
    from stick import (
        small_caps,
        reply_or_dm,
        register_user_command,
        CATEGORY_STICKER,
    )

    @app.on_message(filters.command(["q", "quote", "r"]) & (filters.private | filters.group))
    async def quote_cmd(client: Client, message: Message):
        if not await fsub.check(client, message):
            return

        r = message.reply_to_message
        parts = message.text.split(maxsplit=1)
        cmd_name = parts[0].lstrip("/").split("@")[0].lower()
        cmd_arg = parts[1].strip() if len(parts) > 1 else ""

        include_reply = (cmd_name == "r")
        if cmd_arg.lower() == "r":
            include_reply = True
            cmd_arg = ""
        elif cmd_arg.lower().startswith("r "):
            include_reply = True
            cmd_arg = cmd_arg[2:].strip()

        if not r and not cmd_arg:
            await reply_or_dm(
                client,
                message,
                small_caps(
                    "💬 Usage:\n"
                    "• Reply to any message with /q to make a quote sticker.\n"
                    "• Reply with /q r or /r to also include the replied-to message!"
                ),
            )
            return

        if r:
            name, src = _extract_sender_name(r)
            text = cmd_arg or _extract_msg_preview(r)
        else:
            name, src = _extract_sender_name(message)
            text = cmd_arg

        if not text:
            await reply_or_dm(client, message, small_caps("❌ Only text or captioned messages can be quoted."))
            return

        reply_name = None
        reply_text = None
        if include_reply and r:
            parent = await _resolve_parent_reply(client, r)
            if parent:
                reply_name, _ = _extract_sender_name(parent)
                reply_text = _extract_msg_preview(parent) or "Message"

        avatar = await _fetch_avatar(client, src)

        sticker_buf = _quote_sticker(
            name,
            text,
            avatar=avatar,
            reply_name=reply_name,
            reply_text=reply_text,
        )
        reply_target = r if r else message
        await reply_target.reply_sticker(sticker=sticker_buf)

    register_user_command("q", "Turn a replied message into a quote-card sticker (use /q r to include reply).", category=CATEGORY_STICKER)
    register_user_command("r", "Turn a replied message (with its parent reply) into a quote sticker.", category=CATEGORY_STICKER)
