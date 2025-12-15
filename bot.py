from datetime import timedelta
from telegram.ext import Updater, MessageHandler, Filters

BOT_TOKEN = "8235276430:AAH5Y-e1610FeGx7-FqpBC5yQTZwCWaVeOw"
USERNAME = "help_quality"       # твой username (без @)
FORWARD_CHAT_ID = -5095459026   # chat_id группы, куда пересылать


def build_message_link(message):
    chat = message.chat
    if chat.username:
        return f"https://t.me/{chat.username}/{message.message_id}"
    chat_id_str = str(chat.id)
    if chat_id_str.startswith("-100"):
        internal_id = chat_id_str[4:]
        return f"https://t.me/c/{internal_id}/{message.message_id}"
    return None

def check_mentions(update, context):
    global REQUEST_COUNTER

    message = update.message
    if message and message.text:
        if f"@{USERNAME}" in message.text:

            chat = message.chat
            user = message.from_user

            chat_title = chat.title or "Без назви"

            if user:
                if user.last_name:
                    from_name = f"{user.first_name} {user.last_name}"
                else:
                    from_name = user.first_name
                from_username = f"@{user.username}" if user.username else ""
            else:
                from_name = "Невідомий користувач"
                from_username = ""

            if message.date:
                kyiv_time = message.date + timedelta(hours=2)
                time_str = kyiv_time.strftime("%Y-%m-%d %H:%M:%S")
            else:
                time_str = "Невідомий час"

            msg_link = build_message_link(message)
            if msg_link:
                link_text = msg_link
            else:
                link_text = "Посилання недоступне"

            # Формуємо службове повідомлення ТЕПЕР З НУМЕРАЦІЄЮ
            header_text = (
                f"<b>🧨 Запит на опрацювання🤪</b>\n"
                f"<b>🕓 Дата й час:</b> {time_str}\n"
                f"<b>🌐 Група:</b> {chat_title}\n"
                f"<b>🐈‍⬛ Хто тегнув:</b> {from_name} {from_username}\n"
                f"<b>🔗 Посилання:</b> {link_text}"
            )

            # Надсилаємо службовий текст
            context.bot.send_message(
                chat_id=FORWARD_CHAT_ID,
                text=header_text,
		parse_mode="HTML"
            )

            # Форвард оригінального повідомлення
            message.forward(chat_id=FORWARD_CHAT_ID)

def main():
    print("Твій слуга працює...")
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, check_mentions))

    updater.start_polling()
    updater.idle()

if __name__ == "__main__":
    main()
