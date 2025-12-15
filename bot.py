
import time
import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("mentions-bot")

from datetime import timedelta
from telegram.ext import Updater, MessageHandler, Filters

# ----------------- НАЛАШТУВАННЯ ЧЕРЕЗ ENV -----------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
USERNAME = os.getenv("USERNAME", "help_quality")  # без @
FORWARD_CHAT_ID = os.getenv("FORWARD_CHAT_ID")

if BOT_TOKEN is None:
    raise RuntimeError("BOT_TOKEN не задано в змінних середовища")

if FORWARD_CHAT_ID is None:
    raise RuntimeError("FORWARD_CHAT_ID не задано в змінних середовища")

try:
    FORWARD_CHAT_ID = int(FORWARD_CHAT_ID)
except ValueError:
    raise RuntimeError("FORWARD_CHAT_ID має бути числом (наприклад -1001234567890)")

# Якщо хочеш моніторити тільки певні групи — можна задати ALLOWED_GROUP_IDS у змінних середовища:
# ALLOWED_GROUP_IDS="-1001234567890,-1009876543210"
ALLOWED_GROUP_IDS_ENV = os.getenv("ALLOWED_GROUP_IDS", "").strip()
if ALLOWED_GROUP_IDS_ENV:
    ALLOWED_GROUP_IDS = {
        int(x.strip()) for x in ALLOWED_GROUP_IDS_ENV.split(",") if x.strip()
    }
else:
    ALLOWED_GROUP_IDS = set()  # порожній = слухати всі групи


# ----------------- ДОПОМОЖНІ ФУНКЦІЇ -----------------

MEDIA_GROUP_BUFFER = {}  # media_group_id -> {"items": [message], "ts": float, "has_mention": bool, "meta": dict}
MEDIA_GROUP_DELAY_SEC = 2.5

def build_message_link(message):
    """
    Повертає посилання на повідомлення, якщо Telegram дозволяє його сформувати.
    Працює для:
    - публічних груп/каналів з username
    - супергруп із chat.id, що починається на -100 (формат t.me/c/...)
    """
    chat = message.chat

    # Публічна група/канал з username
    if chat.username:
        return f"https://t.me/{chat.username}/{message.message_id}"

    # Приватна супергрупа (chat.id виглядає як -100xxxxxxxxxx)
    chat_id_str = str(chat.id)
    if chat_id_str.startswith("-100"):
        internal_id = chat_id_str[4:]
        return f"https://t.me/c/{internal_id}/{message.message_id}"

    # Для звичайних приватних груп/чатів стабільного URL може не бути
    return None


# ----------------- ОСНОВНА ЛОГІКА -----------------

def get_message_content(message):
    # текст звичайного повідомлення
    if message.text:
        return message.text
    # підпис до фото/відео/документу
    if message.caption:
        return message.caption
    return None

def flush_media_group(media_group_id, context):
    global REQUEST_COUNTER

    bucket = MEDIA_GROUP_BUFFER.pop(media_group_id, None)
    if not bucket:
        return

    if not bucket["has_mention"]:
        return

    # Сортуємо по message_id щоб порядок був як у чаті
    items = sorted(bucket["items"], key=lambda m: m.message_id)
    meta = bucket["meta"]

    header_text = (
        f"<b>🧨 Запит на опрацювання🤪</b>\n"
        f"<b>🕓 Дата й час:</b> {meta['time_str']}\n"
        f"<b>🌐 Група:</b> {meta['chat_title']}\n"
        f"<b>🐈‍⬛ Хто тегнув:</b> {meta['from_name']} {meta['from_username']}\n"
        f"<b>🔗 Посилання:</b> {meta['link_text']}"
    )

    try:
        logger.info("Альбом: відправляю header (Запит №%s), media_group_id=%s", current_request_no, media_group_id)
        context.bot.send_message(
            chat_id=FORWARD_CHAT_ID,
            text=header_text,
            parse_mode="HTML",
        )

        for m in items:
            logger.info("Альбом: форварджу msg_id=%s", m.message_id)
            m.forward(chat_id=FORWARD_CHAT_ID)

        logger.info("✅ Запит №%s (альбом) успішно переслав, media_group_id=%s", current_request_no, media_group_id)

    except Exception as e:
        logger.exception("❌ Помилка при пересиланні альбому (Запит №%s): %s", current_request_no, e)

def check_mentions(update, context):
    global REQUEST_COUNTER

    message = update.message
    if not message:
        logger.info("Update без message")
        return

    chat = message.chat
    chat_title = chat.title or "Без назви"

    # Лог: що взагалі прилетіло
    logger.info(
        "Update: chat_id=%s chat='%s' msg_id=%s has_text=%s has_caption=%s media_group_id=%s",
        chat.id,
        chat_title,
        getattr(message, "message_id", None),
        bool(message.text),
        bool(message.caption),
        getattr(message, "media_group_id", None),
    )

    # Якщо ALLOWED_GROUP_IDS заданий — фільтруємо за групами
    if ALLOWED_GROUP_IDS and chat.id not in ALLOWED_GROUP_IDS:
        logger.info("Пропуск: чат не в ALLOWED_GROUP_IDS")
        return

    content = get_message_text(message)
    has_mention = bool(content) and (f"@{USERNAME}" in content)

    media_group_id = getattr(message, "media_group_id", None)

    # --- 1) АЛЬБОМ (2+ фото/відео) ---
    if media_group_id:
        now = time.time()
        bucket = MEDIA_GROUP_BUFFER.get(media_group_id)

        if not bucket:
            bucket = {"items": [], "ts": now, "has_mention": False, "meta": None}
            MEDIA_GROUP_BUFFER[media_group_id] = bucket

        bucket["items"].append(message)

        # Якщо згадка знайдена — запам'ятовуємо метадані (1 раз)
        if has_mention and not bucket["has_mention"]:
            bucket["has_mention"] = True
            bucket["meta"] = make_meta_from_message(message)
            logger.info("Знайшов згадку в альбомі: media_group_id=%s, чат='%s'", media_group_id, chat_title)

        # Флашимо, коли пройшов таймер від першого елемента альбому
        if now - bucket["ts"] >= MEDIA_GROUP_DELAY_SEC:
            flush_media_group(media_group_id, context)

        return

    # --- 2) НЕ альбом — стандартна логіка ---
    if not has_mention:
        return

    meta = make_meta_from_message(message)
 	
    header_text = (
        f"<b>🧨 Запит на опрацювання🤪</b>\n"
        f"<b>🕓 Дата й час:</b> {meta['time_str']}\n"
        f"<b>🌐 Група:</b> {meta['chat_title']}\n"
        f"<b>🐈‍⬛ Хто тегнув:</b> {meta['from_name']} {meta['from_username']}\n"
        f"<b>🔗 Посилання:</b> {meta['link_text']}"
    )

    # Надсилаємо службове повідомлення в групу-приймач
    context.bot.send_message(
        chat_id=FORWARD_CHAT_ID,
        text=header_text,
        parse_mode="HTML",
    )

    # Потім форвардимо саме повідомлення з тегом
    message.forward(chat_id=FORWARD_CHAT_ID)

    # Лог у консолі Render
    print(
        f"Пересилаю запит із групи '{chat_title}' від {from_name} {from_username}"
    )


def main():
    print("Твій слуга працює...")
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(
        MessageHandler(
            (Filters.text | Filters.caption) & ~Filters.command, 
            check_mentions,
        )
    )

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":

    main()






