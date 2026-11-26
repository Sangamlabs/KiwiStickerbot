
import asyncio
import logging
from pyrogram import Client
from stick import API_ID, API_HASH, BOT_TOKEN

async def main():
    app = Client(
        "sticker_test",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN
    )

    try:
        await app.start()
        me = await app.get_me()
        print(f"SUCCESS: @{me.username}")
        await app.stop()
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
asyncio.run(main())
