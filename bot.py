import os
import os.path
import threading
import logging
import json
import time
from datetime import timedelta
from typing import Optional, Dict, Any, List, Tuple

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

STATE_TTL_SEC = int(os.getenv("STATE_TTL_SEC", "28800"))  # 8 часов памяти
MEDIA_GROUP_DELAY_SEC = float(os.getenv("MEDIA_GROUP_DELAY_SEC", "3.0"))  # сборка альбома

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


# ----------------- STATE for SINGLE -----------------
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


# ----------------- ALBUM STORE (dedupe by message_id) -----------------
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


def _normalize_album_record(rec: Dict[str, Any]) -> Dict[str, Any]:
    if "items_map" in rec and isinstance(rec["items_map"], dict):
        return rec
    rec["items_map"] = {}
    rec.pop("items", None)
    return rec


def _extract_media_item(message) -> Optional[Dict[str, str]]:
    if message.photo:
        return {"type": "photo", "file_id": message.photo[-1].file_id}
    if message.video:
        return {"type": "video", "file_id": message.video.file_id}
    if message.document:
        return {"type": "document", "file_id": message.document.file_id}
    return None


def add_album_media(chat_id: int, media_group_id: str, message) -> None:
    prune_albums()
    data = load_albums()
    key = album_key(chat_id, str(media_group_id))

    rec = data.get(key) or {"ts": now_ts(), "had_mention": False, "mention_text": "", "meta": None}
    rec = _normalize_album_record(rec)
    rec["ts"] = now_ts()

    item = _extract_media_item(message)
    if item:
        rec["items_map"][str(message.message_id)] = item  # ✅ dedupe by message_id

    data[key] = rec
    save_albums(data)


def set_album_state(chat_id: int, media_group_id: str, had_mention: bool, mention_text: str, meta: Dict[str, Any]) -> None:
    prune_albums()
    data = load_albums()
    key = album_key(chat_id, str(media_group_id))

    rec = data.get(key) or {"ts": now_ts(), "had_mention": False, "mention_text": "", "meta": None}
    rec = _normalize_album_record(rec)

    rec["ts"] = now_ts()
    rec["had_mention"] = bool(had_mention)
    rec["mention_text"] = mention_text or rec.get("mention_text", "")
    rec["meta"] = meta or rec.get("meta")

    data[key] = rec
    save_albums(data)


def get_album(chat_id: int, media_group_id: str) -> Optional[Dict[str, Any]]:
    prune_albums()
    rec = load_albums().get(album_key(chat_id, str(media_group_id)))
    if not rec:
        return None
    return _normalize_album_record(rec)


def build_media_group_from_album(items_map: Dict[str, Dict[str, str]]):
    def sort_key(k: str) -> Tuple[int, str]:
        try:
            return (0, int(k))
        except Exception:
            return (1, k)

    media = []
    for msg_key in sorted(items_map.keys(), key=sort_key):
        it = items_map[msg_key]
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


# ----------------- ALBUM SEND SCHEDULER -----------------
_album_timers: Dict[str, threading.Timer] = {}
_album_lock = threading.Lock()


def schedule_album_send(context, chat_id: int, media_group_id: str):
    key = f"{chat_id}:{media_group_id}"

    def _fire():
        send_album_if_ready(context, chat_id, media_group_id)

    with _album_lock:
        old = _album_timers.get(key)
        if old:
            try:
                old.cancel()
            except Exception:
                pass
        t = threading.Timer(MEDIA_GROUP_DELAY_SEC, _fire)
        t.daemon = True
        _album_timers[key] = t
        t.start()


def send_album_if_ready(context, chat_id: int, media_group_id: str):
    global REQUEST_COUNTER

    alb = get_album(chat_id, media_group_id)
    if not alb or not alb.get("had_mention"):
        return

    pkey = f"ALBUM_SENT:{chat_id}:{media_group_id}"
    if is_processed(pkey):
        return

    num = REQUEST_COUNTER
    REQUEST_COUNTER += 1
    save_counter(REQUEST_COUNTER)

    send_album(context, chat_id, media_group_id, num)


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
        return

    meta = alb.get("meta")
    items_map = alb.get("items_map") or {}
    mention_text = alb.get("mention_text") or ""

    if not meta or not items_map:
        return

    thread_id = get_thread_id_for_chat(chat_id)
    pkey = f"ALBUM_SENT:{chat_id}:{media_group_id}"
    if is_processed(pkey):
        return

    send_header(context, meta, number, thread_id)
    send_text_only_like_forward(context, meta, mention_text, thread_id)

    media = build_media_group_from_album(items_map)
    for i in range(0, len(media), 10):
        kwargs = {"media": media[i:i + 10]}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        send_with_migration_retry(context.bot.send_media_group, chat_id=FORWARD_CHAT_ID, **kwargs)

    mark_processed_key(pkey)
    logger.info("✅ Album sent chat=%s mg=%s items=%s", chat_id, media_group_id, len(items_map))


def send_single_forward(context, message, number: int):
    meta = make_meta(message)
    chat_id = meta["chat_id"]
    thread_id = get_thread_id_for_chat(chat_id)

    pkey = f"MSG_SENT:{chat_id}:{message.message_id}"
    if is_processed(pkey):
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

    # ----------------- ALBUM -----------------
    if media_group_id:
        add_album_media(chat.id, str(media_group_id), message)

        alb = get_album(chat.id, str(media_group_id))
        had_mention_before = bool(alb.get("had_mention")) if alb else False

        # ⚠️ предохранитель по дублям
        already_sent_album = is_processed(f"ALBUM_SENT:{chat.id}:{media_group_id}")

        # ✅ Новое альбомное сообщение с тегом (обычно caption на 1-м элементе)
        if (not is_edited) and mention_now and (not had_mention_before) and (not already_sent_album):
            meta = make_meta(message)
            set_album_state(chat.id, str(media_group_id), True, content or "", meta)
            schedule_album_send(context, chat.id, str(media_group_id))
            return

        # ✅ Edited альбом: пересылать ТОЛЬКО если бот раньше видел альбом без тега,
        # и сейчас тег появился впервые.
        # Если alb отсутствует/пустой раньше — значит бот не видел "до" → не слать.
        if is_edited:
            # если мы раньше не фиксировали этот альбом вообще — игнор
            if not alb:
                return
            # если уже отправляли — игнор
            if already_sent_album:
                return
            # если раньше тега не было, а теперь появился — слать
            if (not had_mention_before) and mention_now:
                meta = make_meta(message)
                set_album_state(chat.id, str(media_group_id), True, content or "", meta)
                schedule_album_send(context, chat.id, str(media_group_id))
                return
            # иначе (смайл/буква/что угодно) — игнор
            return

        # если тег уже был ранее — просто перезапускаем таймер, чтобы добрать элементы (и отправить 1 раз)
        if had_mention_before and (not already_sent_album):
            schedule_album_send(context, chat.id, str(media_group_id))

        return

    # ----------------- SINGLE -----------------
    prev = get_seen_state(chat.id, message.message_id)
    had_mention_before = bool(prev.get("had_mention")) if prev else False

    already_sent = is_processed(f"MSG_SENT:{chat.id}:{message.message_id}")
    if already_sent:
        # ✅ если уже отправляли — любые edits игнор
        return

    # записываем state (для будущих edits)
    if mention_now or had_mention_before:
        set_seen_state(chat.id, message.message_id, True)
    else:
        set_seen_state(chat.id, message.message_id, False)

    is_new = (prev is None)

    # 1) новое сообщение с тегом
    should_send_new = (not is_edited) and is_new and mention_now

    # 2) edited: ТОЛЬКО если prev существует и было без тега, стало с тегом
    should_send_edit_added = is_edited and (prev is not None) and (not had_mention_before) and mention_now

    if should_send_new or should_send_edit_added:
        num = REQUEST_COUNTER
        REQUEST_COUNTER += 1
        save_counter(REQUEST_COUNTER)
        send_single_forward(context, message, num)

    return


# ----------------- RUN -----------------
def main():
    logger.info(
        "Bot started | DATA_DIR=%s | USER=@%s | counter=%s | TTL=%s | MEDIA_DELAY=%s | ROUTES=%s | ALLOWED=%s",
        DATA_DIR, USERNAME, REQUEST_COUNTER, STATE_TTL_SEC, MEDIA_GROUP_DELAY_SEC, ROUTES,
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

