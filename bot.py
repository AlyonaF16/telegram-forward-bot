import os
import os.path
import threading
import logging
import json
import time
import hashlib
from datetime import timedelta
from typing import Optional

from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument
from telegram.ext import Updater, MessageHandler, Filters, CommandHandler
from telegram.error import ChatMigrated

# ----------------- LOGGING -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("mentions-bot")

# ----------------- ENV -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
USERNAME = os.getenv("USERNAME", "help_quality")  # без @
FORWARD_CHAT_ID_ENV = os.getenv("FORWARD_CHAT_ID")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задано")
if not FORWARD_CHAT_ID_ENV:
    raise RuntimeError("FORWARD_CHAT_ID не задано в ENV")

# Optional: allowlist для исходных групп
ALLOWED_GROUP_IDS_ENV = os.getenv("ALLOWED_GROUP_IDS", "").strip()
if ALLOWED_GROUP_IDS_ENV:
    ALLOWED_GROUP_IDS = {int(x.strip()) for x in ALLOWED_GROUP_IDS_ENV.split(",") if x.strip()}
else:
    ALLOWED_GROUP_IDS = set()  # пусто = слушаем все группы

# ----------------- ROUTING BY TOPICS -----------------
# FORMAT env: ROUTES="-100group1:thread1,-100group2:thread2"
ROUTES_ENV = os.getenv("ROUTES", "").strip()
ROUTES: dict[int, int] = {}

if ROUTES_ENV:
    for pair in ROUTES_ENV.split(","):
        pair = pair.strip()
        if not pair:
            continue
        try:
            src_str, thread_str = pair.split(":")
            ROUTES[int(src_str.strip())] = int(thread_str.strip())
        except ValueError:
            logger.warning("Невірний формат ROUTES пари: %s", pair)


def get_thread_id_for_chat(chat_id: int) -> Optional[int]:
    return ROUTES.get(chat_id)

# ----------------- COUNTER -----------------
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

# ----------------- FORWARD CHAT ID PERSISTENCE -----------------
FORWARD_CHAT_ID_FILE = "forward_chat_id.txt"


def save_forward_chat_id(value: int) -> None:
    with open(FORWARD_CHAT_ID_FILE, "w", encoding="utf-8") as f:
        f.write(str(value))
    logger.info("Оновив FORWARD_CHAT_ID у файлі: %s", value)


def load_forward_chat_id() -> int:
    if os.path.exists(FORWARD_CHAT_ID_FILE):
        try:
            with open(FORWARD_CHAT_ID_FILE, "r", encoding="utf-8") as f:
                val = int(f.read().strip())
                logger.info("Завантажив FORWARD_CHAT_ID з файлу: %s", val)
                return val
        except Exception as e:
            logger.warning("Не вдалося прочитати forward_chat_id.txt: %s", e)

    val = int(FORWARD_CHAT_ID_ENV)
    logger.info("Використовую FORWARD_CHAT_ID з ENV: %s", val)
    save_forward_chat_id(val)
    return val


FORWARD_CHAT_ID = load_forward_chat_id()


def send_with_migration_retry(func, *, chat_id: int, **kwargs):
    global FORWARD_CHAT_ID
    try:
        return func(chat_id=chat_id, **kwargs)
    except ChatMigrated as e:
        new_id = e.new_chat_id
        logger.warning("Цільова група мігрувала. Новий chat_id = %s", new_id)
        FORWARD_CHAT_ID = new_id
        save_forward_chat_id(new_id)
        return func(chat_id=new_id, **kwargs)

# ----------------- EVENT DEDUP -----------------
DEDUP_FILE = "dedupe.json"
DEDUP_TTL_SEC = 7 * 24 * 60 * 60
_dedup_cache = None


def _load_dedup():
    global _dedup_cache
    if _dedup_cache is not None:
        return _dedup_cache
    try:
        with open(DEDUP_FILE, "r", encoding="utf-8") as f:
            _dedup_cache = json.load(f)
    except Exception:
        _dedup_cache = {}
    return _dedup_cache


def _save_dedup(data):
    with open(DEDUP_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _prune_dedup(data, now_ts: int):
    dead = [k for k, ts in data.items() if (now_ts - int(ts)) > DEDUP_TTL_SEC]
    for k in dead:
        data.pop(k, None)


def _mk_hash(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:12]


def is_duplicate_event(event_key: str) -> bool:
    now_ts = int(time.time())
    data = _load_dedup()
    _prune_dedup(data, now_ts)
    if event_key in data:
        return True
    data[event_key] = now_ts
    _save_dedup(data)
    return False

# ----------------- PROCESSED MENTIONS -----------------
# Одне повідомлення (або альбом) -> один запит.
PROCESSED_FILE = "processed_mentions.json"
_processed_cache = None


def load_processed():
    global _processed_cache
    if _processed_cache is not None:
        return _processed_cache
    try:
        with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
            _processed_cache = json.load(f)
    except Exception:
        _processed_cache = {}
    return _processed_cache


def save_processed(data):
    with open(PROCESSED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def was_processed(chat_id: int, message_id: int) -> bool:
    data = load_processed()
    return f"{chat_id}:{message_id}" in data


def mark_processed(chat_id: int, message_id: int):
    data = load_processed()
    data[f"{chat_id}:{message_id}"] = int(time.time())
    save_processed(data)

# ----------------- MEDIA GROUP (ALBUMS) -----------------
MEDIA_GROUP_DELAY_SEC = float(os.getenv("MEDIA_GROUP_DELAY_SEC", "2.8"))

# active buffer while album is arriving
MEDIA_GROUP_BUFFER: dict[str, dict] = {}

# archive for albums without mention (so edit later can trigger)
ALBUM_ARCHIVE_TTL_SEC = int(os.getenv("ALBUM_ARCHIVE_TTL_SEC", "3600"))  # 1 hour default
ALBUM_ARCHIVE: dict[str, dict] = {}  # media_group_id -> {items, ts}


def prune_album_archive():
    now = time.time()
    dead = [k for k, v in ALBUM_ARCHIVE.items() if (now - float(v.get("ts", now))) > ALBUM_ARCHIVE_TTL_SEC]
    for k in dead:
        ALBUM_ARCHIVE.pop(k, None)


# ----------------- HELPERS -----------------
def escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def get_message_content(message):
    if message.text:
        return message.text
    if message.caption:
        return message.caption
    return ""


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
    time_str = (message.date + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S") if message.date else "Невідомий час"
    return {
        "chat_title": chat_title,
        "from_name": from_name,
        "from_username": from_username,
        "time_str": time_str,
        "link_text": build_message_link(message),
        "chat_id": message.chat.id,
    }


def build_header(meta, number: int) -> str:
    return (
        f"<b>🧨 Запит на опрацювання №{number}</b>\n"
        f"<b>🕓 Дата й час:</b> {meta['time_str']}\n"
        f"<b>🌐 Група:</b> {escape_html(meta['chat_title'])}\n"
        f"<b>🐈‍⬛ Хто тегнув:</b> {escape_html(meta['from_name'])} {escape_html(meta['from_username'])}\n"
        f"<b>🔗 Посилання:</b> {escape_html(meta['link_text'])}"
    )


def album_to_input_media(items):
    media = []
    for m in items:
        if m.photo:
            media.append(InputMediaPhoto(media=m.photo[-1].file_id))
        elif m.video:
            media.append(InputMediaVideo(media=m.video.file_id))
        elif m.document:
            media.append(InputMediaDocument(media=m.document.file_id))
    return media


def send_album_text_only(context, meta, text: str, thread_id: Optional[int]):
    text = text or ""
    if not text.strip():
        return
    prefix = f"Переслано від {escape_html(meta['from_name'])} {escape_html(meta['from_username'])}".strip()
    body = f"<i>{prefix}</i>\n{escape_html(text)}"
    kwargs = {"text": body, "parse_mode": "HTML", "disable_web_page_preview": True}
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id
    send_with_migration_retry(context.bot.send_message, chat_id=FORWARD_CHAT_ID, **kwargs)


def send_album_bundle(context, *, meta, mention_text, items, media_group_id: str):
    """Send: header + text-only + media group (all items), mark processed."""
    global REQUEST_COUNTER

    items = sorted(items, key=lambda m: m.message_id)
    if not items:
        return

    # If already processed album (first msg) -> stop
    if was_processed(meta["chat_id"], items[0].message_id):
        logger.info("⏭ Альбом вже оброблений (chat=%s msg=%s)", meta["chat_id"], items[0].message_id)
        return

    # Dedup by album event
    event_key = f"ALBUM_EVT:{meta['chat_id']}:{media_group_id}:{_mk_hash(mention_text)}"
    if is_duplicate_event(event_key):
        logger.info("⏭ Пропуск дубля ALBUM_EVT: %s", event_key)
        return

    number = REQUEST_COUNTER
    thread_id = get_thread_id_for_chat(meta["chat_id"])

    # 1) header
    kwargs_header = {"text": build_header(meta, number), "parse_mode": "HTML"}
    if thread_id is not None:
        kwargs_header["message_thread_id"] = thread_id
    send_with_migration_retry(context.bot.send_message, chat_id=FORWARD_CHAT_ID, **kwargs_header)

    # 2) text only
    send_album_text_only(context, meta, mention_text, thread_id)

    # 3) media плиткой
    media = album_to_input_media(items)
    if media:
        for i in range(0, len(media), 10):
            kwargs_media = {"media": media[i:i + 10]}
            if thread_id is not None:
                kwargs_media["message_thread_id"] = thread_id
            send_with_migration_retry(context.bot.send_media_group, chat_id=FORWARD_CHAT_ID, **kwargs_media)
    else:
        # fallback
        for m in items:
            kwargs_fwd = {}
            if thread_id is not None:
                kwargs_fwd["message_thread_id"] = thread_id
            send_with_migration_retry(m.forward, chat_id=FORWARD_CHAT_ID, **kwargs_fwd)

    # mark processed for whole album
    for m in items:
        mark_processed(meta["chat_id"], m.message_id)

    REQUEST_COUNTER += 1
    save_counter(REQUEST_COUNTER)
    logger.info("✅ Запит №%s (album) відправлено, items=%s", number, len(items))


def flush_media_group(media_group_id, context):
    """Finish receiving album. If no mention -> move to archive. If mention -> send."""
    bucket = MEDIA_GROUP_BUFFER.pop(media_group_id, None)
    if not bucket:
        return

    try:
        if bucket.get("timer"):
            bucket["timer"].cancel()
    except Exception:
        pass

    items = bucket.get("items", [])
    has_mention = bucket.get("has_mention", False)

    # If no mention yet -> archive it (so edit later can trigger)
    if not has_mention:
        prune_album_archive()
        ALBUM_ARCHIVE[media_group_id] = {
            "items": items,
            "ts": time.time(),
        }
        logger.info("📦 Альбом без тегу збережено в архів: media_group_id=%s items=%s", media_group_id, len(items))
        return

    # if mention present -> send immediately
    send_album_bundle(
        context,
        meta=bucket["meta"],
        mention_text=bucket.get("mention_text", ""),
        items=items,
        media_group_id=media_group_id,
    )

# ----------------- /topicid -----------------
def topic_id(update, context):
    message = update.message or update.edited_message
    if not message:
        return
    thread_id = getattr(message, "message_thread_id", None)
    message.reply_text(f"chat_id = {message.chat.id}\nmessage_thread_id = {thread_id}")

# ----------------- MAIN HANDLER -----------------
def check_mentions(update, context):
    global REQUEST_COUNTER

    message = update.message or update.edited_message
    if not message:
        return

    chat = message.chat
    if ALLOWED_GROUP_IDS and chat.id not in ALLOWED_GROUP_IDS:
        return

    media_group_id = getattr(message, "media_group_id", None)
    content = get_message_content(message)
    has_mention = (f"@{USERNAME}" in (content or ""))

    logger.info(
        "Update: chat_id=%s msg_id=%s edited=%s media_group_id=%s mention=%s text=%s caption=%s photo=%s video=%s doc=%s",
        chat.id,
        getattr(message, "message_id", None),
        bool(update.edited_message),
        media_group_id,
        has_mention,
        bool(message.text),
        bool(message.caption),
        bool(message.photo),
        bool(message.video),
        bool(message.document),
    )

    # ---- ALBUM ----
    # ВСЕ элементы альбома буферим всегда (даже если mention=False),
    # иначе при тегах в caption только у одного фото — мы потеряем остальные.
    if media_group_id:
        bucket = MEDIA_GROUP_BUFFER.get(media_group_id)
        if not bucket:
            bucket = {"items": [], "has_mention": False, "meta": None, "timer": None, "mention_text": ""}
            MEDIA_GROUP_BUFFER[media_group_id] = bucket

            t = threading.Timer(MEDIA_GROUP_DELAY_SEC, flush_media_group, args=(media_group_id, context))
            t.daemon = True
            bucket["timer"] = t
            t.start()

        bucket["items"].append(message)

        # если тег обнаружили на любом элементе — фиксируем 1 раз
        if has_mention and not bucket["has_mention"]:
            bucket["has_mention"] = True
            bucket["meta"] = make_meta(message)
            bucket["mention_text"] = content or ""

            # Если этот альбом уже успел уйти в ARCHIVE (редактирование спустя время) — достаём и шлём все фото
            prune_album_archive()
            archived = ALBUM_ARCHIVE.pop(media_group_id, None)
            if archived:
                all_items = (archived.get("items") or []) + bucket["items"]
                # убираем дубли по message_id
                uniq = {}
                for m in all_items:
                    uniq[m.message_id] = m
                all_items = list(uniq.values())

                logger.info("📤 Тег додали пізніше. Дістав альбом з архіву: media_group_id=%s total_items=%s",
                            media_group_id, len(all_items))

                # Отправляем сразу (без ожидания таймера)
                send_album_bundle(
                    context,
                    meta=bucket["meta"],
                    mention_text=bucket["mention_text"],
                    items=all_items,
                    media_group_id=media_group_id,
                )

                # чистим активный буфер, чтобы таймер не отправил еще раз
                try:
                    if bucket.get("timer"):
                        bucket["timer"].cancel()
                except Exception:
                    pass
                MEDIA_GROUP_BUFFER.pop(media_group_id, None)

        return

    # ---- SINGLE ----
    if not has_mention:
        return

    # одно сообщение -> один раз
    if was_processed(message.chat.id, message.message_id):
        logger.info("⏭ Пропуск: message уже обработан (chat=%s msg=%s)", message.chat.id, message.message_id)
        return

    # защита от повторных апдейтов
    event_key = f"SINGLE_EVT:{message.chat.id}:{message.message_id}:{_mk_hash(content)}"
    if is_duplicate_event(event_key):
        logger.info("⏭ Пропуск дубля SINGLE_EVT: %s", event_key)
        return

    number = REQUEST_COUNTER
    meta = make_meta(message)
    thread_id = get_thread_id_for_chat(meta["chat_id"])

    # header
    kwargs_header = {"text": build_header(meta, number), "parse_mode": "HTML"}
    if thread_id is not None:
        kwargs_header["message_thread_id"] = thread_id
    send_with_migration_retry(context.bot.send_message, chat_id=FORWARD_CHAT_ID, **kwargs_header)

    # forward as-is
    kwargs_fwd = {}
    if thread_id is not None:
        kwargs_fwd["message_thread_id"] = thread_id
    send_with_migration_retry(message.forward, chat_id=FORWARD_CHAT_ID, **kwargs_fwd)

    mark_processed(message.chat.id, message.message_id)

    REQUEST_COUNTER += 1
    save_counter(REQUEST_COUNTER)
    logger.info("✅ Запит №%s (single) відправлено", number)

# ----------------- RUN -----------------
def main():
    logger.info(
        "Bot started | USERNAME=@%s | start counter=%s | FORWARD_CHAT_ID=%s | ROUTES=%s | ALLOWED=%s | ALBUM_TTL=%ss",
        USERNAME, REQUEST_COUNTER, FORWARD_CHAT_ID, ROUTES,
        (len(ALLOWED_GROUP_IDS) if ALLOWED_GROUP_IDS else "ALL"),
        ALBUM_ARCHIVE_TTL_SEC
    )

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("topicid", topic_id))

    # edited messages (когда добавили тег после редактирования)
    dp.add_handler(
        MessageHandler(
            Filters.update.edited_message & (Filters.text | Filters.caption | Filters.photo | Filters.video | Filters.document),
            check_mentions,
        )
    )

    # normal messages
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


