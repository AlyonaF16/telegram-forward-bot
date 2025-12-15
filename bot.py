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
    ALLOWED_GROUP_IDS = set()  # пусто = слушаем все группы

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
MEDIA_GROUP_BUFFER = {}  # media_group_id -> {items, has_mention, meta, timer}


# ----------------- HELPERS -----------------
def escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def get_message_content(message):
    """Возвращает text или caption."""
    if message.text:
        return message.text
    if message.caption:
        return message.caption
    return None


def build_message_link(message):
    """URL на сообщение (если возможно)."""
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

    content = get_message_content(message) or ""

    return {
        "chat_title": chat_title,
        "from_name": from_name,
        "from_username": from_username,
        "time_str": time_str,
        "link_text": build_message_link(message),
        "content": content,
    }


def build_header(meta, number: int) -> str:
    # Чуть аккуратнее визуально + одинаково везде
    return (
        f"<b>Слуга якості</b>\n"
        f"<b>🧨 Запит на опрацювання №{number}</b>\n"
        f"<b>🕓 Дата й час:</b> {meta['time_str']}\n"
        f"<b>🌐 Група:</b> {escape_html(meta['chat_title'])}\n"
        f"<b>🐈‍⬛ Хто тегнув:</b> {escape_html(meta['from_name'])} {escape_html(meta['from_username'])}\n"
        f"<b>🔗 Посилання:</b> {escape_html(meta['link_text'])}"
    )


def send_text_block(context, text: str):
    """Красивый блок текста, без дублей."""
    safe = escape_html(text)
    if not safe.strip():
        return
    context.bot.send_message(
        chat_id=FORWARD_CHAT_ID,
        text=f"<b>💬 Текст:</b>\n{safe}",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def album_to_input_media(items):
    """Собираем список InputMedia* для sendMediaGroup."""
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
    """Header + текст отдельно + альбом плиткой. Счетчик +1 один раз."""
    global REQUEST_COUNTER

    bucket = MEDIA_GROUP_BUFFER.pop(media_group_id, None)
    if not bucket:
        return

    try:
        t = bucket.get("timer")
        if t:
            t.cancel()
    except Exception:
        pass

    if not bucket.get("has_mention") or not bucket.get("meta"):
        logger.info("Album %s: mention not found, skip", media_group_id)
        return

    number = REQUEST_COUNTER
    meta = bucket["meta"]
    items = sorted(bucket["items"], key=lambda m: m.message_id)

    try:
        # 1) Header
        context.bot.send_message(
            chat_id=FORWARD_CHAT_ID,
            text=build_header(meta, number),
            parse_mode="HTML",
        )

        # 2) Текст (caption) отдельным сообщением, иначе он потеряется при sendMediaGroup
        if meta.get("content"):
            send_text_block(context, meta["content"])

        # 3) Альбом плиткой
        media = album_to_input_media(items)

        if not media:
            # fallback
            for m in items:
                m.forward(chat_id=FORWARD_CHAT_ID)
        else:
            # лимит Telegram: 10 медиа за раз
            for i in range(0, len(media), 10):
                context.bot.send_media_group(
                    chat_id=FORWARD_CHAT_ID,
                    media=media[i:i + 10],
                )

        # 4) Counter
        REQUEST_COUNTER += 1
        save_counter(REQUEST_COUNTER)
        logger.info("✅ Запит №%s (альбом плиткою) відправлено", number)

    except Exception as e:
        logger.exception("❌ Album %s error: %s", media_group_id, e)


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
            logger.info("🔥 Mention found in album %s", media_group_id)

        return

    # ---- SINGLE ----
    if not has_mention:
        return

    number = REQUEST_COUNTER
    meta = make_meta(message)

    try:
        # 1) Header
        context.bot.send_message(
            chat_id=FORWARD_CHAT_ID,
            text=build_header(meta, number),
            parse_mode="HTML",
        )

        is_media = bool(message.photo or message.video or message.document)

        if not is_media:
            # Чистый текст: шлем один раз красивым блоком, без forward (чтобы не было дубля)
            if meta.get("content"):
                send_text_block(context, meta["content"])
        else:
            # Медиа: forward (так сохраняется и медиа, и caption)
            message.forward(chat_id=FORWARD_CHAT_ID)

        # 3) Counter
        REQUEST_COUNTER += 1
        save_counter(REQUEST_COUNTER)
        logger.info("✅ Запит №%s переслано", number)

    except Exception as e:
        logger.exception("❌ Single message error: %s", e)


# ----------------- RUN -----------------
def main():
    logger.info("Bot started | USERNAME=@%s | start counter=%s", USERNAME, REQUEST_COUNTER)

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    # Важно: ловим и текст, и caption, и медиа без caption (чтобы собрать весь альбом)
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


