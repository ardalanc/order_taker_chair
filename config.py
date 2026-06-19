import os

ADMIN_IDS = [715337548]

BOT_TOKEN = os.environ.get("BOT_TOKEN")

DB_NAME = "chair"

DATABASE_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "user": os.environ.get("DB_USER", "root"),
    "password": os.environ.get("DB_PASSWORD", ""),
}