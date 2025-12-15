import os
import threading
import logging
from datetime import timedelta

from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument
from telegram.ext import Updater, MessageHandler, Filters

# ----------------- LOGGING -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
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
    ALLOWED_GROUP_IDS = set()  # пусто = слухаємо всі групи

# ----------------- COUNTER (GLOBAL) -----------------
COUNTER_FILE = "counter.txt"


def load_counter() -> int:
    try:
        with open(COUNTER_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return 1


def save_counter(value: int) -> None:
    with open(COUNTER_FILE, "w", encoding="utf-8") as f:
        f.write(str(value))


REQUEST_COUNTER = load_counter()

# ----------------- MEDIA GROUP (ALBUMS) -----------------
MEDIA_GROUP_DELAY_SEC = 2.5
# media_group_id -> {items, has_mention, meta, timer, mention_msg_id}
MEDIA_GROUP_BUFFER = {}

# ----------------- HELPERS -----------------
def escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def get_message_content(message):
    """Повертає text або caption."""
    if message.text:
        return message.text
    if message.caption:
        return message.caption
    return None


def build_message_link(message):
    """URL на повідомлення (якщо можливо)."""
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


def build_header(meta, number: int) -> str:
    return (
        f"<b>🧨 Запит на опрацювання №{number}🤪</b>\n"
        f"<b>🕓 Дата й час:</b> {meta['time_str']}\n"
        f"<b>🌐 Група:</b> {escape_html(meta['chat_title'])}\n"
        f"<b>🐈‍⬛ Хто тегнув:</b> {escape_html(meta['from_name'])} {escape_html(meta['from_username'])}\n"
        f"<b>🔗 Посилання:</b> {escape_html(meta['link_text'])}"
    )


def album_to_input_media(items):
    """Конвертує повідомлення альбому у InputMedia* для sendMediaGroup."""
    media = []
    for m in items:
        if m.photo:
            file_id = m.photo[-1].file_id
            media.append(InputMediaPhoto(media=file_id))
        elif m.video:
            media.append(InputMediaVideo(media=m.video.file_id))
        elif m.document:
            media.append(InputMediaDocument(media=m.document.file_id))
    return media


def flush_media_group(media_group_id, context):
    """
    Альбом:
    - Header
    - Текст як "переслано" (forward повідомлення, де був тег/текст)
    - Фото плиткою (sendMediaGroup), але БЕЗ дубля — виключаємо форварднуте повідомлення
    """
    global REQUEST_COUNTER

    bucket = MEDIA_GROUP_BUFFER.pop(media_group_id, None)
    if not bucket or not bucket.get("has_mention"):
        return

    try:
        if bucket.get("timer"):
            bucket["timer"].cancel()
    except Exception:
        pass

    number = REQUEST_COUNTER
    meta = bucket["meta"]
    mention_msg_id = bucket.get("mention_msg_id")
    items = sorted(bucket["items"], key=lambda m: m.message_id)

    try:
        # 1) Header
        context.bot.send_message(
            chat_id=FORWARD_CHAT_ID,
            text=build_header(meta, number),
            parse_mode="HTML",
        )

        # 2) Forward "текстового" елемента альбому (той, де був тег)
        forwarded_message = None
        if mention_msg_id:
            for m in items:
                if m.message_id == mention_msg_id:
                    forwarded_message = m
                    break
        if not forwarded_message:
            forwarded_message = items[0]

        # forward дає "як переслано" і не губить caption
        forwarded_message.forward(chat_id=FORWARD_CHAT_ID)

        # 3) Фото плиткою, але БЕЗ дубля: виключаємо forwarded_message
        remaining_items = [m for m in items if m.message_id != forwarded_message.message_id]
        media = album_to_input_media(remaining_items)

        if media:
            for i in range(0, len(media), 10):
                context.bot.send_media_group(
                    chat_id=FORWARD_CHAT_ID,
                    media=media[i:i + 10],
                )
        # якщо залишилося 0 — значить альбом був з 1 елемента (рідко), і все вже форварднулося

        REQUEST_COUNTER += 1
        save_counter(REQUEST_COUNTER)
        logger.info("✅ Запит №%s (альбом без дубля) відправлено", number)

    except Exception as e:
        logger.exception("❌ Помилка альбому %s: %s", media_group_id, e)


# ----------------- HANDLER -----------------
def check_mentions(update, context):
    global REQUEST_COUNTER

    message = update.message
    if not message:
        return

    chat = message.chat
    if ALLOWED_GROUP_IDS and chat.id not in ALLOWED_GROUP_IDS:
        return

    media_group_id = getattr(message, "media_group_id", None)

    logger.info(
        "Update: chat_id=%s msg_id=%s text=%s caption=%s photo=%s video=%s doc=%s media_group_id=%s",
        chat.id,
        getattr(message, "message_id", None),
        bool(message.text),
        bool(message.caption),
        bool(message.photo),
        bool(message.video),
        bool(message.document),
        media_group_id,
    )

    content = get_message_content(message)
    has_mention = bool(content) and (f"@{USERNAME}" in content)

    # ---- ALBUM ----
    if media_group_id:
        bucket = MEDIA_GROUP_BUFFER.get(media_group_id)
        if not bucket:
            bucket = {
                "items": [],
                "has_mention": False,
                "meta": None,
                "timer": None,
                "mention_msg_id": None,
            }
            MEDIA_GROUP_BUFFER[media_group_id] = bucket

            t = threading.Timer(MEDIA_GROUP_DELAY_SEC, flush_media_group, args=(media_group_id, context))
            t.daemon = True
            bucket["timer"] = t
            t.start()

        bucket["items"].append(message)

        if has_mention and not bucket["has_mention"]:
            bucket["has_mention"] = True
            bucket["meta"] = make_meta(message)
            bucket["mention_msg_id"] = message.message_id  # запам’ятали, де саме був тег/текст
            logger.info("🔥 Згадка знайдена в альбомі %s (msg_id=%s)", media_group_id, message.message_id)

        return

    # ---- SINGLE ----
    if not has_mention:
        return

    number = REQUEST_COUNTER
    meta = make_meta(message)

    try:
        # Header
        context.bot.send_message(
            chat_id=FORWARD_CHAT_ID,
            text=build_header(meta, number),
            parse_mode="HTML",
        )

        # Текст -> forward (як переслано)
        # 1 фото/відео/док -> forward (без змін)
        message.forward(chat_id=FORWARD_CHAT_ID)

        REQUEST_COUNTER += 1
        save_counter(REQUEST_COUNTER)
        logger.info("✅ Запит №%s переслано", number)

    except Exception as e:
        logger.exception("❌ Помилка при пересиланні: %s", e)


# ----------------- RUN -----------------
def main():
    logger.info("Bot started | USERNAME=@%s | start counter=%s", USERNAME, REQUEST_COUNTER)

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Важливо: ловимо і текст, і caption, і медіа без caption (для елементів альбому)
    dp.add_handler(
        MessageHandler(
            (Filters.text | Filters.caption | Filters.photo | Filters.video | Filters.document) & ~Filters.command,
            check_mentions,
        )
    )

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()

