import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "isurajsethh").lstrip("@")
BOT_USERNAME = os.getenv("BOT_USERNAME", "TG_RESTRICTED_CONTENT_2027_BOT").lstrip("@")

AROLINKS_URL = os.getenv("AROLINKS_URL", "https://arolinks.com/P4fkk").strip()
AD_REWARD_HOURS = int(os.getenv("AD_REWARD_HOURS", "3"))
FREE_LIFETIME_LIMIT = int(os.getenv("FREE_LIFETIME_LIMIT", "10"))

PORT = int(os.getenv("PORT", "10000"))
DB_PATH = os.getenv("DB_PATH", "bot_data.sqlite3")
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
