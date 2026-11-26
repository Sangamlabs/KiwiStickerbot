
import requests
import os
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("BOT_TOKEN")

response = requests.get(
    f"https://api.telegram.org/bot{token}/getMe",
    timeout=20
)

print("HTTP:", response.status_code)
print("Response:", response.json())
