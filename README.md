# 🎨 Telegram Sticker Maker Bot

A powerful Telegram Sticker Maker Bot built with **Pyrogram** and **Pillow**.

Create high-quality Telegram stickers directly from photos, images, or text with automatic background removal, resizing, and optimization.

---

## ✨ Features

- 🖼️ Create static Telegram stickers
- 🎭 PNG image support
- 📝 Text to Sticker
- 🎨 Custom fonts
- ⚡ Fast processing
- 🤖 Built with Pyrogram v2
- 💾 MongoDB Support
- 🔒 Owner/Admin System
- 📦 Easy Deployment
- 🌐 Works on Windows, Linux & VPS

---

# 📂 Project Structure

```text
stickermaker/
│
├── bot.py
├── config.py
├── requirements.txt
├── .env
├── handlers/
├── utils/
├── fonts/
├── assets/
├── database/
└── README.md
```

---

# 🚀 Installation

## Clone Repository

```bash
git clone https://github.com/itachiplub-cloud/stickerbot.git

cd stickerbot
```

---

## Create Virtual Environment

### Windows

```bash
python -m venv .venv

.venv\Scripts\activate
```

### Linux / Ubuntu / WSL

```bash
python3 -m venv .venv

source .venv/bin/activate
```

---

## Install Requirements

```bash
pip install -r requirements.txt
```

---

# ⚙ Environment Variables

Copy `.env.example` to `.env`:

```bash
# Windows
copy .env.example .env

# Linux / macOS
cp .env.example .env
```

Open `.env` and fill in your credentials:

```env
API_ID=12345678
API_HASH=your_telegram_api_hash_here
BOT_TOKEN=your_bot_token_here
MONGODB_URI=mongodb://localhost:27017
ADMIN_USER_ID=123456789
```

### Environment Variable Reference

| Variable | Required | Description | Default |
|---|---|---|---|
| `API_ID` | Yes | Telegram API ID from [my.telegram.org](https://my.telegram.org) | - |
| `API_HASH` | Yes | Telegram API Hash from [my.telegram.org](https://my.telegram.org) | - |
| `BOT_TOKEN` | Yes | Telegram Bot Token from [@BotFather](https://t.me/BotFather) | - |
| `ADMIN_USER_ID` | Yes | Numeric Telegram user ID of the admin/owner (comma-separated for multiple) | - |
| `MONGODB_URI` | Yes | MongoDB connection URI (`mongodb://...` or `mongodb+srv://...`) | - |
| `DB_NAME` | No | Database name in MongoDB | `sticker_bot` |
| `COLLECTION_NAME` | No | Collection name for user data | `users` |
| `PACKS_COLLECTION_NAME` | No | Collection name for sticker packs | `sticker_packs` |
| `DEFAULT_EMOJI` | No | Default sticker emoji | `✨` |
| `TEMP_DIR` | No | Directory for temporary files | `temp_stickers` |
| `FORCE_SUB_CHANNELS` | No | Comma-separated channels required for force-sub (e.g. `@channel`) | - |
| `CUSTOM_FONT_PATH` | No | Path to custom `.ttf` font file | - |

---

# ▶ Run Bot

Windows

```bash
python bot.py
```

Linux

```bash
python3 bot.py
```

---

# 📦 Install FFmpeg (Optional)

Ubuntu

```bash
sudo apt update
sudo apt install ffmpeg
```

Windows

Download FFmpeg

https://ffmpeg.org/download.html

---

# 🛠 Commands

| Command | Description |
|----------|-------------|
| /start | Start Bot |
| /help | Help Menu |
| /sticker | Create Sticker |
| /text | Text to Sticker |

---

# 📋 Requirements

- Python 3.11+
- MongoDB
- Pillow
- Pyrogram v2
- TgCrypto

---

# Install Dependencies

```bash
pip install -r requirements.txt
```

---

# ❤️ Support

If you like this project don't forget to ⭐ the repository.

---

# 👨‍💻 Developer

**GitHub**

https://github.com/itachiplub-cloud

---

# 📜 License

MIT License
