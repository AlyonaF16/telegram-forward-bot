
import os
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

def get_message_text(message):
    # текст звичайного повідомлення
    if message.text:
        return message.text
    # підпис до фото/відео/документу
    if message.caption:
        return message.caption
    return None

def check_mentions(update, context):
    message = update.message

    # Немає повідомлення або тексту — нічого не робимо
    content = get_message_text(message)
    if not content:
        return

    if f"@{USERNAME}" not in content:
        return

    # Якщо ALLOWED_GROUP_IDS заданий — фільтруємо за групами
    if ALLOWED_GROUP_IDS and message.chat.id not in ALLOWED_GROUP_IDS:
        return

    # Перевіряємо наявність згадки
    if f"@{USERNAME}" not in message.text:
        return

    chat = message.chat
    user = message.from_user

    # Назва групи
    chat_title = chat.title or "Без назви"

    # Хто тегнув
    if user:
        if user.last_name:
            from_name = f"{user.first_name} {user.last_name}"
        else:
            from_name = user.first_name
        from_username = f"@{user.username}" if user.username else ""
    else:
        from_name = "Невідомий користувач"
        from_username = ""

    # Час — message.date в UTC, додаємо +2 години під Київ
    if message.date:
        kyiv_time = message.date + timedelta(hours=2)
        time_str = kyiv_time.strftime("%Y-%m-%d %H:%M:%S")
    else:
        time_str = "Невідомий час"

    # Посилання на повідомлення
    msg_link = build_message_link(message)
    if msg_link:
        link_text = msg_link
    else:
        link_text = "Посилання недоступне (тип групи не підтримує прямі URL)"

    # Формуємо службове повідомлення (HTML формат, жирні заголовки)
    header_text = (
 		f"<b>🧨 Запит на опрацювання🤪</b>\n"
                f"<b>🕓 Дата й час:</b> {time_str}\n"
                f"<b>🌐 Група:</b> {chat_title}\n"
                f"<b>🐈‍⬛ Хто тегнув:</b> {from_name} {from_username}\n"
                f"<b>🔗 Посилання:</b> {link_text}"
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


