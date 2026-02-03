import os
import os.path
import threading
import logging
import json
import time
from datetime import timedelta
from typing import Optional, Dict, Any, List

from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument
from telegram.ext import Updater, MessageHandler, Filters, CommandHandler
from telegram.error import ChatMigrated


# ----------------- LOGGING -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("mentions-bot")


# ----------------- DATA DIR (Render Disk) -----------------
DATA_DIR = os.getenv("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)

COUNTER_FILE = os.path.join(DATA_DIR, "counter.txt")
PROCESSED_FILE = os.path.join(DATA_DIR, "processed_mentions.json")
STATE_FILE = os.path.join(DATA_DIR, "seen_state.json")
ALBUM_FILE = os.path.join(DATA_DIR, "album_store.json")
FORWARD_CHAT_ID_FILE = os.path.join(DATA_DIR, "forward_chat_id.txt")


# ----------------- ENV -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
USERNAME = os.getenv("USERNAME", "help_quality").lstrip("@")  # без @
FORWARD_CHAT_ID_ENV = os.getenv("FORWARD_CHAT_ID")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задано")
if not FORWARD_CHAT_ID_ENV:
    raise RuntimeError("FORWARD_CHAT_ID не задано в ENV")

# Сколько держим память, чтобы ловить добавление тега через часы
STATE_TTL_SEC = int(os.getenv("STATE_TTL_SEC", "28800"))  # 8 часов

# Optional allowlist групп
ALLOWED_GROUP_IDS_ENV = os.getenv("ALLOWED_GROUP_IDS", "").strip()
if ALLOWED_GROUP_IDS_ENV:
    ALLOWED_GROUP_IDS = {int(x.strip()) for x in ALLOWED_GROUP_IDS_ENV.split(",") if x.strip()}
else:
    ALLOWED_GROUP_IDS = set()  # пусто = слушаем все группы

# ROUTES env: "-100src1:thread1,-100src2:thread2"
ROUTES_ENV = os.getenv("ROUTES", "").strip()
ROUTES: Dict[int, int] = {}
if ROUTES_ENV:
    for pair in ROUTES_ENV.split(","):
        pair = pair.strip()
        if not pair:
            continue
        try:
            src_str, thr_str = pair.split(":")
            ROUTES[int(src_str.strip())] = int(thr_str.strip())
        except ValueError:
            logger.warning("Неверный формат ROUTES пары: %s", pair)


def get_thread_id_for_chat(chat_id: int) -> Optional[int]:
    return ROUTES.get(chat_id)


# ----------------- COUNTER -----------------
def load_counter() -> int:
    try:
        with open(COUNTER_FILE, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return 1


def save_counter(val: int) -> None:
    with open(COUNTER_FILE, "w", encoding="utf-8") as f:
        f.write(str(val))


REQUEST_COUNTER = load_counter()


# ----------------- FORWARD CHAT ID (migration-safe) -----------------
def save_forward_chat_id(val: int) -> None:
    with open(FORWARD_CHAT_ID_FILE, "w", encoding="utf-8") as f:
        f.write(str(val))
    logger.info("Saved FORWARD_CHAT_ID=%s", val)


def load_forward_chat_id() -> int:
    if os.path.exists(FORWARD_CHAT_ID_FILE):
        try:
            with open(FORWARD_CHAT_ID_FILE, "r", encoding="utf-8") as f:
                return int(f.read().strip())
        except Exception:
            pass
    val = int(FORWARD_CHAT_ID_ENV)
    save_forward_chat_id(val)
    return val


FORWARD_CHAT_ID = load_forward_chat_id()


def send_with_migration_retry(func, *, chat_id: int, **kwargs):
    global FORWARD_CHAT_ID
    try:
        return func(chat_id=chat_id, **kwargs)
    except ChatMigrated as e:
        FORWARD_CHAT_ID = e.new_chat_id
        save_forward_chat_id(FORWARD_CHAT_ID)
        return func(chat_id=FORWARD_CHAT_ID, **kwargs)


# ----------------- HELPERS -----------------
def escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def get_message_content(message) -> str:
    if message.text:
        return message.text
    if message.caption:
        return message.caption
    return ""


def has_tag(text: str) -> bool:
    return f"@{USERNAME}" in (text or "")


def build_message_link(message) -> str:
    chat = message.chat
    if chat.username:
        return f"https://t.me/{chat.username}/{message.message_id}"
    chat_id_str = str(chat.id)
    if chat_id_str.startswith("-100"):
        return f"https://t.me/c/{chat_id_str[4:]}/{message.message_id}"
    return "Посилання недоступне"


def make_meta(message) -> Dict[str, Any]:
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
        "chat_id": message.chat.id,
        "chat_title": chat_title,
        "from_name": from_name,
        "from_username": from_username,
        "time_str": time_str,
        "link_text": build_message_link(message),
    }


def build_header(meta: Dict[str, Any], number: int) -> str:
    return (
        f"<b>🧨 Запит на опрацювання №{number}</b>\n"
        f"<b>🕓 Дата й час:</b> {meta['time_str']}\n"
        f"<b>🌐 Група:</b> {escape_html(meta['chat_title'])}\n"
        f"<b>🐈‍⬛ Хто тегнув:</b> {escape_html(meta['from_name'])} {escape_html(meta['from_username'])}\n"
        f"<b>🔗 Посилання:</b> {escape_html(meta['link_text'])}"
    )


def now_ts() -> int:
    return int(time.time())


# ----------------- PROCESSED (anti-duplicate sends) -----------------
_processed_cache = None


def load_processed() -> Dict[str, int]:
    global _processed_cache
    if _processed_cache is not None:
        return _processed_cache
    try:
        with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
            _processed_cache = json.load(f)
    except Exception:
        _processed_cache = {}
    return _processed_cache


def save_processed(data: Dict[str, int]) -> None:
    with open(PROCESSED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def is_processed(key: str) -> bool:
    return key in load_processed()


def mark_processed_key(key: str) -> None:
    data = load_processed()
    data[key] = now_ts()
    save_processed(data)


# ----------------- STATE for SINGLE (had_mention before?) -----------------
_state_cache = None


def load_state() -> Dict[str, Dict[str, Any]]:
    global _state_cache
    if _state_cache is not None:
        return _state_cache
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            _state_cache = json.load(f)
    except Exception:
        _state_cache = {}
    return _state_cache


def save_state(data: Dict[str, Dict[str, Any]]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def prune_state() -> None:
    data = load_state()
    t = now_ts()
    dead = [k for k, v in data.items() if (t - int(v.get("ts", t))) > STATE_TTL_SEC]
    for k in dead:
        data.pop(k, None)
    if dead:
        save_state(data)


def state_key_for_message(chat_id: int, message_id: int) -> str:
    return f"MSG:{chat_id}:{message_id}"


def set_seen_state(chat_id: int, message_id: int, had_mention: bool) -> None:
    prune_state()
    data = load_state()
    data[state_key_for_message(chat_id, message_id)] = {"ts": now_ts(), "had_mention": bool(had_mention)}
    save_state(data)


def get_seen_state(chat_id: int, message_id: int) -> Optional[Dict[str, Any]]:
    prune_state()
    return load_state().get(state_key_for_message(chat_id, message_id))


# ----------------- ALBUM STORE (file_id list for later) -----------------
_album_cache = None


def load_albums() -> Dict[str, Dict[str, Any]]:
    global _album_cache
    if _album_cache is not None:
        return _album_cache
    try:
        with open(ALBUM_FILE, "r", encoding="utf-8") as f:
            _album_cache = json.load(f)
    except Exception:
        _album_cache = {}
    return _album_cache


def save_albums(data: Dict[str, Dict[str, Any]]) -> None:
    with open(ALBUM_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def prune_albums() -> None:
    data = load_albums()
    t = now_ts()
    dead = [k for k, v in data.items() if (t - int(v.get("ts", t))) > STATE_TTL_SEC]
    for k in dead:
        data.pop(k, None)
    if dead:
        save_albums(data)


def album_key(chat_id: int, media_group_id: str) -> str:
    return f"ALB:{chat_id}:{media_group_id}"


def add_album_media(chat_id: int, media_group_id: str, message) -> None:
    prune_albums()
    data = load_albums()
    key = album_key(chat_id, str(media_group_id))

    rec = data.get(key) or {"ts": now_ts(), "items": [], "had_mention": False, "mention_text": "", "meta": None}
    rec["ts"] = now_ts()

    if message.photo:
        rec["items"].append({"type": "photo", "file_id": message.photo[-1].file_id})
    elif message.video:
        rec["items"].append({"type": "video", "file_id": message.video.file_id})
    elif message.document:
        rec["items"].append({"type": "document", "file_id": message.document.file_id})

    data[key] = rec
    save_albums(data)


def set_album_state(chat_id: int, media_group_id: str, had_mention: bool, mention_text: str, meta: Dict[str, Any]) -> None:
    prune_albums()
    data = load_albums()
    key = album_key(chat_id, str(media_group_id))
    rec = data.get(key) or {"ts": now_ts(), "items": [], "had_mention": False, "mention_text": "", "meta": None}

    rec["ts"] = now_ts()
    rec["had_mention"] = bool(had_mention)
    rec["mention_text"] = mention_text or rec.get("mention_text", "")
    rec["meta"] = meta or rec.get("meta")

    data[key] = rec
    save_albums(data)


def get_album(chat_id: int, media_group_id: str) -> Optional[Dict[str, Any]]:
    prune_albums()
    return load_albums().get(album_key(chat_id, str(media_group_id)))


def build_media_group_from_album(items: List[Dict[str, str]]):
    media = []
    for it in items:
        t = it.get("type")
        fid = it.get("file_id")
        if not fid:
            continue
        if t == "photo":
            media.append(InputMediaPhoto(media=fid))
        elif t == "video":
            media.append(InputMediaVideo(media=fid))
        elif t == "document":
            media.append(InputMediaDocument(media=fid))
    return media


# ----------------- ALBUM timer (optional) -----------------
MEDIA_GROUP_DELAY_SEC = float(os.getenv("MEDIA_GROUP_DELAY_SEC", "2.8"))
MEDIA_GROUP_BUFFER: Dict[str, Dict[str, Any]] = {}


def flush_album(media_group_id: str, context):
    MEDIA_GROUP_BUFFER.pop(str(media_group_id), None)


def ensure_album_timer(media_group_id: str, context):
    mg = str(media_group_id)
    if mg in MEDIA_GROUP_BUFFER:
        return
    t = threading.Timer(MEDIA_GROUP_DELAY_SEC, flush_album, args=(mg, context))
    t.daemon = True
    MEDIA_GROUP_BUFFER[mg] = {"timer": t}
    t.start()


# ----------------- SENDERS -----------------
def send_header(context, meta: Dict[str, Any], number: int, thread_id: Optional[int]):
    kwargs = {"text": build_header(meta, number), "parse_mode": "HTML"}
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id
    send_with_migration_retry(context.bot.send_message, chat_id=FORWARD_CHAT_ID, **kwargs)


def send_text_only_like_forward(context, meta: Dict[str, Any], text: str, thread_id: Optional[int]):
    if not (text or "").strip():
        return
    prefix = f"Переслано від {escape_html(meta['from_name'])} {escape_html(meta['from_username'])}".strip()
    body = f"<i>{prefix}</i>\n{escape_html(text)}"
    kwargs = {"text": body, "parse_mode": "HTML", "disable_web_page_preview": True}
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id
    send_with_migration_retry(context.bot.send_message, chat_id=FORWARD_CHAT_ID, **kwargs)


def send_album(context, chat_id: int, media_group_id: str, number: int):
    alb = get_album(chat_id, media_group_id)
    if not alb:
        logger.warning("Album not found in store chat=%s mg=%s", chat_id, media_group_id)
        return

    meta = alb.get("meta")
    items = alb.get("items") or []
    mention_text = alb.get("mention_text") or ""

    if not meta or not items:
        logger.warning("Album store incomplete chat=%s mg=%s meta=%s items=%s", chat_id, media_group_id, bool(meta), len(items))
        return

    thread_id = get_thread_id_for_chat(chat_id)

    pkey = f"ALBUM_SENT:{chat_id}:{media_group_id}"
    if is_processed(pkey):
        logger.info("Skip already sent album %s", pkey)
        return

    send_header(context, meta, number, thread_id)
    send_text_only_like_forward(context, meta, mention_text, thread_id)

    media = build_media_group_from_album(items)
    if media:
        for i in range(0, len(media), 10):
            kwargs = {"media": media[i:i + 10]}
            if thread_id is not None:
                kwargs["message_thread_id"] = thread_id
            send_with_migration_retry(context.bot.send_media_group, chat_id=FORWARD_CHAT_ID, **kwargs)

    mark_processed_key(pkey)


def send_single_forward(context, message, number: int):
    meta = make_meta(message)
    chat_id = meta["chat_id"]
    thread_id = get_thread_id_for_chat(chat_id)

    pkey = f"MSG_SENT:{chat_id}:{message.message_id}"
    if is_processed(pkey):
        logger.info("Skip already sent msg %s", pkey)
        return

    send_header(context, meta, number, thread_id)

    kwargs_fwd = {}
    if thread_id is not None:
        kwargs_fwd["message_thread_id"] = thread_id
    send_with_migration_retry(message.forward, chat_id=FORWARD_CHAT_ID, **kwargs_fwd)

    mark_processed_key(pkey)


# ----------------- /topicid -----------------
def topic_id(update, context):
    message = update.message or update.edited_message
    if not message:
        return
    tid = getattr(message, "message_thread_id", None)
    message.reply_text(f"chat_id = {message.chat.id}\nmessage_thread_id = {tid}")


# ----------------- MAIN HANDLER -----------------
def handle(update, context):
    global REQUEST_COUNTER

    message = update.message or update.edited_message
    if not message:
        return

    chat = message.chat
    if ALLOWED_GROUP_IDS and chat.id not in ALLOWED_GROUP_IDS:
        return

    is_edited = bool(update.edited_message)
    content = get_message_content(message)
    mention_now = has_tag(content)
    media_group_id = getattr(message, "media_group_id", None)

    logger.info(
        "Update: chat_id=%s msg_id=%s edited=%s media_group_id=%s mention_now=%s",
        chat.id, message.message_id, is_edited, media_group_id, mention_now
    )

    # -------- ALBUM --------
    if media_group_id:
        ensure_album_timer(media_group_id, context)
        add_album_media(chat.id, str(media_group_id), message)

        alb = get_album(chat.id, str(media_group_id))
        had_mention_before = bool(alb.get("had_mention")) if alb else False

        # Отправляем альбом только при первом появлении тега
        if mention_now and not had_mention_before:
            meta = make_meta(message)
            set_album_state(chat.id, str(media_group_id), True, content or "", meta)

            num = REQUEST_COUNTER
            REQUEST_COUNTER += 1
            save_counter(REQUEST_COUNTER)

            send_album(context, chat.id, str(media_group_id), num)
        else:
            if not had_mention_before:
                meta = make_meta(message)
                set_album_state(chat.id, str(media_group_id), False, alb.get("mention_text", "") if alb else "", meta)

        return

    # -------- SINGLE (не альбом) --------
    prev = get_seen_state(chat.id, message.message_id)
    had_mention_before = bool(prev.get("had_mention")) if prev else False

    # обновляем state (если уже True — остаётся True)
    if mention_now or had_mention_before:
        set_seen_state(chat.id, message.message_id, True)
    else:
        set_seen_state(chat.id, message.message_id, False)

    # ✅ Правило:
    # 1) Если это НОВОЕ сообщение с тегом — переслать сразу.
    # 2) Если это EDITED и тег появился впервые — переслать.
    # 3) Все остальные случаи — игнор (включая повторы старых апдейтов).
    is_new_message = (prev is None)

    should_send_new = (is_new_message and (not is_edited) and mention_now)
    should_send_edit_added = (is_edited and mention_now and not had_mention_before)

    if should_send_new or should_send_edit_added:
        num = REQUEST_COUNTER
        REQUEST_COUNTER += 1
        save_counter(REQUEST_COUNTER)
        send_single_forward(context, message, num)

    return


# ----------------- RUN -----------------
def main():
    logger.info(
        "Bot started | DATA_DIR=%s | USER=@%s | counter=%s | STATE_TTL_SEC=%s | ROUTES=%s | ALLOWED=%s",
        DATA_DIR, USERNAME, REQUEST_COUNTER, STATE_TTL_SEC, ROUTES,
        (len(ALLOWED_GROUP_IDS) if ALLOWED_GROUP_IDS else "ALL")
    )

    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("topicid", topic_id))

    dp.add_handler(
        MessageHandler(
            Filters.update.edited_message & (Filters.text | Filters.caption | Filters.photo | Filters.video | Filters.document),
            handle,
        )
    )
    dp.add_handler(
        MessageHandler(
            (Filters.text | Filters.caption | Filters.photo | Filters.video | Filters.document) & ~Filters.command,
            handle,
        )
    )

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
