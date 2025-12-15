import os
import time
import threading
import logging
from datetime import timedelta
from telegram.ext import Updater, MessageHandler, Filters

# ----------------- LOGGING -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("mentions-bot")

# ----------------- ENV -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
USERNAME = os.getenv("USERNAME", "help_quality")  # без @
FORWARD_CHAT_ID = os.getenv("FORWARD_CHAT_ID")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задано")
if not FORWARD_CHAT_ID:
    raise RuntimeError("FORWARD_CHAT_ID не задано")

FORWARD_CHAT_ID = int(FORWARD_CHAT_ID)

ALLOWED_GROUP_IDS_ENV = os.getenv("ALLOWED_GROUP_IDS", "").strip()
if ALLOWED_GROUP_IDS_ENV:
    ALLOWED_GROUP_IDS = {int(x.strip()) for x in ALLOWED_GROUP_IDS_ENV.split(",") if x.strip()}
else:
    ALLOWED_GROUP_IDS = set()

# ----------------- COUNTER -----------------
COUNTER_FILE = "counter.txt"

def load_counter():
    try:
        with open(COUNTER_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return 1

def save_counter(value: int):
    with open(COUNTER_FILE, "w", encoding="utf-8") as f:
        f.write(str(value))

REQUEST_COUNTER = load_counter()

# ----------------- MEDIA GROUP -----------------
MEDIA_GROUP_DELAY_SEC = 2.5
MEDIA_GROUP_BUFFER = {}  # media_group_id -> {items, has_mention, meta, timer}

# ----------------- HELPERS -----------------
def get_message_content(message):
    if message.text:
        return message.text
    if message.caption:
        return message.caption
    return None

def build_message_link(message):
    chat = message.chat
    if chat.username:
        return f"https://t.me/{chat.username}/{message.message_id}"
    chat_id_str = str(chat.id)
    if chat_id_str.startswith("-100"):
        return f"https://t.me/c/{chat_id_str[4:]}/{message.message_id}"
    return "Посилання недоступне"

def make_meta(message):
    chat_title = message.chat.title or "Без назви"
    user = message.from_user

    if user:
        from_name = f"{user.first_name} {user.last_name}".strip() if user.last_name else user.first_name
        from_username = f"@{user.username}" if user.username else ""
    else:
        from_name = "Невідомий користувач"
        from_username = ""

    if message.date:
        time_str = (message.date + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    else:
        time_str = "Невідомий час"

    return {
        "chat_title": chat_title,
        "from_name": from_name,
        "from_username": from_username,
        "time_str": time_str,
        "link_text": build_message_link(message),
    }

def build_header(meta, number):
    return (
        f"<b>🧨 Запит на опрацювання №{number}</b>\n"
        f"<b>🕓 Дата й час:</b> {meta['time_str']}\n"
        f"<b>🌐 Група:</b> {meta['chat_title']}\n"
        f"<b>🐈‍⬛ Хто тегнув:</b> {meta['from_name']} {meta['from_username']}\n"
        f"<b>🔗 Посилання:</b> {meta['link_text']}"
    )

def flush_media_group(media_group_id, context):
    global REQUEST_COUNTER

    bucket = MEDIA_GROUP_BUFFER.pop(media_group_id, None)
    if not bucket or not bucket["has_mention"]:
        return

    try:
        bucket["timer"].cancel()
    except Exception:
        pass

    number = REQUEST_COUNTER
    meta = bucket["meta"]
    items = sorted(bucket["items"], key=lambda m: m.message_id)

    try:
        context.bot.send_message(
            chat_id=FORWARD_CHAT_ID,
            text=build_header(meta, number),
            parse_mode="HTML",
        )
        for m in items:
            m.forward(chat_id=FORWARD_CHAT_ID)

        REQUEST_COUNTER += 1
        save_counter(REQUEST_COUNTER)

        logger.info("✅ Запит №%s (альбом) переслано", number)
    except Exception as e:
        logger.exception("❌ Помилка альбому: %s", e)

# ----------------- HANDLER -----------------
def check_mentions(update, context):
    global REQUEST_COUNTER

    message = update.message
    if not message:
        return

    chat = message.chat
    if ALLOWED_GROUP_IDS and chat.id not in ALLOWED_GROUP_IDS:
        return

    content = get_message_content(message)
    has_mention = bool(content) and f"@{USERNAME}" in content
    media_group_id = getattr(message, "media_group_id", None)

    # ---- ALBUM ----
    if media_group_id:
        bucket = MEDIA_GROUP_BUFFER.get(media_group_id)
        if not bucket:
            bucket = {"items": [], "has_mention": False, "meta": None, "timer": None}
            MEDIA_GROUP_BUFFER[media_group_id] = bucket
            t = threading.Timer(MEDIA_GROUP_DELAY_SEC, flush_media_group, args=(media_group_id, context))
            t.daemon = True
            bucket["timer"] = t
            t.start()

        bucket["items"].append(message)

        if has_mention and not bucket["has_mention"]:
            bucket["has_mention"] = True
            bucket["meta"] = make_meta(message)

        return

    # ---- SINGLE ----
    if not has_mention:
        return

    number = REQUEST_COUNTER
    meta = make_meta(message)

    try:
        context.bot.send_message(
            chat_id=FORWARD_CHAT_ID,
            text=build_header(meta, number),
            parse_mode="HTML",
        )
        message.forward(chat_id=FORWARD_CHAT_ID)

        REQUEST_COUNTER += 1
        save_counter(REQUEST_COUNTER)

        logger.info("✅ Запит №%s переслано", number)
    except Exception as e:
        logger.exception("❌ Помилка: %s", e)

# ----------------- RUN -----------------
def main():
    logger.info("Бот запущено | старт з №%s", REQUEST_COUNTER)
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(MessageHandler((Filters.text | Filters.caption) & ~Filters.command, check_mentions))
    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()

