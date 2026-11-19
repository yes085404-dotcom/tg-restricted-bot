import asyncio
import os
import re
import sqlite3
import time
from typing import Dict, List, Optional, Set, Tuple

from telethon import TelegramClient
from telethon.errors import (
    AuthKeyUnregisteredError,
    ChannelPrivateError,
    ChatForwardsRestrictedError,
    FloodWaitError,
    RPCError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo

from config import API_HASH, API_ID, DB_PATH, DOWNLOAD_DIR, TG_SESSION

MAX_BATCH_FILES = 10000

ACTIVE_TASKS: Dict[int, Set[asyncio.Task]] = {}
_CLIENTS: Dict[int, TelegramClient] = {}
_PENDING_LOGINS: Dict[int, dict] = {}
_DIALOGS_LOADED: Set[int] = set()
_LOCK = asyncio.Lock()


def _init_session_table() -> None:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tg_user_sessions (
                user_id INTEGER PRIMARY KEY,
                session_str TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        conn.commit()


def _save_session_db(user_id: int, session_str: str) -> None:
    _init_session_table()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO tg_user_sessions (user_id, session_str, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                session_str = excluded.session_str,
                updated_at = excluded.updated_at
            """,
            (user_id, session_str, time.time()),
        )
        conn.commit()


def _load_session_db(user_id: int) -> str:
    _init_session_table()
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT session_str FROM tg_user_sessions WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return (row[0] if row and row[0] else "").strip()


def _delete_session_db(user_id: int) -> None:
    _init_session_table()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM tg_user_sessions WHERE user_id = ?", (user_id,))
        conn.commit()


def _safe_string_session(raw_str: str) -> Optional[StringSession]:
    s = (raw_str or "").strip().strip('"').strip("'")
    if not s or len(s) < 20 or " " in s or "\n" in s or "import " in s:
        return None
    try:
        sess = StringSession(s)
        if not getattr(sess, "auth_key", None):
            return None
        return sess
    except Exception:
        return None


def register_user_task(user_id: int, task: asyncio.Task) -> None:
    ACTIVE_TASKS.setdefault(user_id, set()).add(task)

    def _cleanup(t: asyncio.Task) -> None:
        bucket = ACTIVE_TASKS.get(user_id)
        if bucket:
            bucket.discard(t)
            if not bucket:
                ACTIVE_TASKS.pop(user_id, None)

    task.add_done_callback(_cleanup)


def cancel_user_tasks(user_id: int) -> int:
    bucket = list(ACTIVE_TASKS.get(user_id, ()))
    cancelled = 0
    for t in bucket:
        if not t.done():
            t.cancel()
            cancelled += 1
    return cancelled


def has_api_creds() -> bool:
    return bool(API_ID and API_HASH)


async def start_tg_phone_login(user_id: int, phone: str) -> Tuple[str, str]:
    if not has_api_creds():
        return "error", "❌ Render Environment में <b>API_ID</b> और <b>API_HASH</b> सेट करें।"
    clean_phone = re.sub(r"[^\d+]", "", (phone or "").strip())
    if len(re.sub(r"\D", "", clean_phone)) < 6:
        return "error", "❌ मान्य फ़ोन नंबर भेजें (जैसे <code>+919876543210</code>)।"

    async with _LOCK:
        old = _CLIENTS.pop(user_id, None)
        _DIALOGS_LOADED.discard(user_id)
        if old:
            try:
                await old.disconnect()
            except Exception:
                pass
        _PENDING_LOGINS.pop(user_id, None)

        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        sent = await client.send_code_request(clean_phone)
        _PENDING_LOGINS[user_id] = {
            "client": client,
            "phone": clean_phone,
            "phone_code_hash": sent.phone_code_hash,
        }
        return "code", "📩 आपके Telegram पर OTP कोड भेजा गया है। कृपया <b>OTP</b> भेजें:"


async def submit_tg_otp_code(user_id: int, code: str) -> Tuple[str, str]:
    pending = _PENDING_LOGINS.get(user_id)
    if not pending:
        return "error", "⚠️ लॉगिन सेशन नहीं मिला। कृपया दोबारा TG Login शुरू करें।"

    clean_code = re.sub(r"\s+", "", (code or "").strip())
    client: TelegramClient = pending["client"]
    phone: str = pending["phone"]
    code_hash: str = pending["phone_code_hash"]

    try:
        await client.sign_in(phone=phone, code=clean_code, phone_code_hash=code_hash)
    except SessionPasswordNeededError:
        return "2fa", "🔐 आपके अकाउंट पर <b>2-Step Verification (2FA)</b> लगा है। कृपया अपना <b>2FA Password</b> भेजें:"

    session_str = client.session.save()
    _save_session_db(user_id, session_str)
    _CLIENTS[user_id] = client
    _DIALOGS_LOADED.discard(user_id)
    _PENDING_LOGINS.pop(user_id, None)
    return "logged_in", "✅ <b>Telegram Login सफल!</b> अब जब तक लॉगिन रहेगा, आप Private डाउनलोड कर सकते हैं।"


async def submit_tg_2fa_password(user_id: int, password: str) -> Tuple[str, str]:
    pending = _PENDING_LOGINS.get(user_id)
    if not pending:
        return "error", "⚠️ लॉगिन सेशन नहीं मिला। कृपया दोबारा TG Login शुरू करें।"

    pwd = (password or "").strip()
    if not pwd:
        return "2fa", "❌ पासवर्ड खाली नहीं हो सकता। कृपया <b>2FA Password</b> भेजें:"

    client: TelegramClient = pending["client"]
    await client.sign_in(password=pwd)
    session_str = client.session.save()
    _save_session_db(user_id, session_str)
    _CLIENTS[user_id] = client
    _DIALOGS_LOADED.discard(user_id)
    _PENDING_LOGINS.pop(user_id, None)
    return "logged_in", "✅ <b>2FA के साथ Telegram Login सफल!</b> अब जब तक लॉगिन रहेगा, आप Private डाउनलोड कर सकते हैं।"


async def logout_tg_user(user_id: int) -> None:
    async with _LOCK:
        _PENDING_LOGINS.pop(user_id, None)
        _DIALOGS_LOADED.discard(user_id)
        client = _CLIENTS.pop(user_id, None)
        if client:
            try:
                await client.log_out()
            except Exception:
                try:
                    await client.disconnect()
                except Exception:
                    pass
    _delete_session_db(user_id)


async def get_user_client(user_id: int) -> Optional[TelegramClient]:
    if not has_api_creds():
        return None
    async with _LOCK:
        existing = _CLIENTS.get(user_id)
        if existing is not None:
            try:
                if not existing.is_connected():
                    await existing.connect()
                if await existing.is_user_authorized():
                    return existing
            except AuthKeyUnregisteredError:
                _delete_session_db(user_id)
            except Exception:
                pass
            _CLIENTS.pop(user_id, None)
            _DIALOGS_LOADED.discard(user_id)

        saved_str = _load_session_db(user_id)
        sess = _safe_string_session(saved_str) if saved_str else None
        if sess is None and TG_SESSION:
            sess = _safe_string_session(TG_SESSION)
        if sess is None:
            return None

        client = TelegramClient(sess, API_ID, API_HASH)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                _delete_session_db(user_id)
                return None
        except AuthKeyUnregisteredError:
            try:
                await client.disconnect()
            except Exception:
                pass
            _delete_session_db(user_id)
            return None
        except Exception:
            try:
                await client.disconnect()
            except Exception:
                pass
            return None

        _CLIENTS[user_id] = client
        return client


async def has_tg_login(user_id: int) -> bool:
    return (await get_user_client(user_id)) is not None


async def _ensure_dialogs_registered(user_id: int, client: TelegramClient) -> None:
    if user_id in _DIALOGS_LOADED:
        return
    try:
        await client.get_dialogs(limit=200)
        _DIALOGS_LOADED.add(user_id)
    except Exception:
        pass


async def _resolve_tg_entity(user_id: int, client: TelegramClient, peer):
    await _ensure_dialogs_registered(user_id, client)
    try:
        return await client.get_input_entity(peer)
    except Exception:
        _DIALOGS_LOADED.discard(user_id)
        await _ensure_dialogs_registered(user_id, client)
        return await client.get_input_entity(peer)


def _apply_text_rules(text: str, settings: dict) -> str:
    out = text or ""
    for old_w, new_w in settings.get("replace_words") or []:
        if old_w:
            out = out.replace(old_w, new_w)
    for del_w in settings.get("delete_words") or []:
        if del_w:
            out = out.replace(del_w, "")
    tag = (settings.get("rename_tag") or "").strip()
    if tag and out:
        out = f"{out} {tag}".strip()
    elif tag and not out:
        out = tag
    custom_cap = (settings.get("custom_caption") or "").strip()
    if custom_cap:
        out = custom_cap
    return out.strip()


def _parse_single_link(link: str) -> Tuple[Optional[object], Optional[int], Optional[int]]:
    clean = (link or "").strip()
    m_priv_topic = re.match(r"^https?://t\.me/c/(\d+)/(\d+)/(\d+)", clean)
    if m_priv_topic:
        peer = int(f"-100{m_priv_topic.group(1)}")
        return peer, int(m_priv_topic.group(3)), int(m_priv_topic.group(2))

    m_priv = re.match(r"^https?://t\.me/c/(\d+)/(\d+)", clean)
    if m_priv:
        peer = int(f"-100{m_priv.group(1)}")
        return peer, int(m_priv.group(2)), None

    m_priv_channel_only = re.match(r"^https?://t\.me/c/(\d+)/?$", clean)
    if m_priv_channel_only:
        peer = int(f"-100{m_priv_channel_only.group(1)}")
        return peer, 1, None

    m_pub_topic = re.match(r"^https?://t\.me/([A-Za-z0-9_]+)/(\d+)/(\d+)", clean)
    if m_pub_topic and m_pub_topic.group(1) != "c":
        return m_pub_topic.group(1), int(m_pub_topic.group(3)), int(m_pub_topic.group(2))

    m_pub = re.match(r"^https?://t\.me/([A-Za-z0-9_]+)/(\d+)", clean)
    if m_pub and m_pub.group(1) != "c":
        return m_pub.group(1), int(m_pub.group(2)), None

    m_pub_channel_only = re.match(r"^https?://t\.me/([A-Za-z0-9_]+)/?$", clean)
    if m_pub_channel_only and m_pub_channel_only.group(1) not in ("c", "joinchat"):
        return m_pub_channel_only.group(1), 1, None

    return None, None, None


def _build_message_ids(mode_key: str, raw_input: str, first_id: int) -> List[int]:
    txt = (raw_input or "").strip()

    if "|" in txt and ("first_last_range" in mode_key or "range" in mode_key):
        part1, part2 = txt.split("|", 1)
        _, id1, _ = _parse_single_link(part1.strip())
        _, id2, _ = _parse_single_link(part2.strip())
        if id1 and id2:
            start_i, end_i = min(id1, id2), max(id1, id2)
            return list(range(start_i, min(end_i, start_i + MAX_BATCH_FILES - 1) + 1))

    if mode_key == "ask_link_pub_custom":
        ids: List[int] = []
        for part in txt.split(","):
            part = part.strip()
            if "-" in part:
                a_s, b_s = part.split("-", 1)
                if a_s.strip().isdigit() and b_s.strip().isdigit():
                    a, b = int(a_s), int(b_s)
                    step = 1 if b >= a else -1
                    ids.extend(range(a, b + step, step))
            elif part.isdigit():
                ids.append(int(part))
        if ids:
            return ids[:MAX_BATCH_FILES]

    if mode_key == "ask_link_priv_range":
        urls = re.findall(r"https?://t\.me/\S+", txt)
        if len(urls) >= 2:
            _, id1, _ = _parse_single_link(urls[0])
            _, id2, _ = _parse_single_link(urls[1])
            if id1 and id2:
                start_i, end_i = min(id1, id2), max(id1, id2)
                return list(range(start_i, min(end_i, start_i + MAX_BATCH_FILES - 1) + 1))

    if "-" in txt and txt.replace("-", "").replace(" ", "").isdigit():
        a_s, b_s = txt.split("-", 1)
        a, b = int(a_s.strip()), int(b_s.strip())
        step = 1 if b >= a else -1
        return list(range(a, min(b, a + step * (MAX_BATCH_FILES - 1)) + step, step))

    return [first_id]


def _extract_video_dims(tg_msg) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    w, h, dur = None, None, None
    doc = getattr(tg_msg, "document", None)
    if doc and getattr(doc, "attributes", None):
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeVideo):
                w = getattr(attr, "w", None) or None
                h = getattr(attr, "h", None) or None
                dur = int(getattr(attr, "duration", 0) or 0) or None
                break
    return w, h, dur


def _has_downloadable_media(tg_msg) -> bool:
    if not tg_msg:
        return False
    return bool(
        getattr(tg_msg, "media", None)
        or getattr(tg_msg, "photo", None)
        or getattr(tg_msg, "video", None)
        or getattr(tg_msg, "document", None)
        or getattr(tg_msg, "audio", None)
        or getattr(tg_msg, "voice", None)
    )


async def _find_message_with_media(client: TelegramClient, entity, mid: int):
    msg = await client.get_messages(entity, ids=mid)
    if _has_downloadable_media(msg):
        return msg
    grouped_id = getattr(msg, "grouped_id", None) if msg else None
    if grouped_id:
        nearby = await client.get_messages(entity, ids=list(range(max(1, mid - 5), mid + 6)))
        if nearby:
            for item in nearby:
                if item and getattr(item, "grouped_id", None) == grouped_id and _has_downloadable_media(item):
                    return item
    return msg


def _progress_bar(pct: float, length: int = 10) -> str:
    clamped = max(0.0, min(100.0, pct))
    filled = int(round((clamped / 100.0) * length))
    return "█" * filled + "░" * (length - filled)


def _format_live_status(
    idx: int,
    total: int,
    delivered: int,
    dl_pct: int,
    dl_speed: float,
    ul_pct: int,
    ul_speed: float,
    eta_sec: float,
    phase_label: str,
) -> str:
    remaining = max(0, total - delivered)
    overall_pct = int(round(((idx - 1 + (ul_pct / 100.0)) / max(1, total)) * 100))
    overall_pct = max(0, min(100, overall_pct))
    eta_str = f"{int(max(0, eta_sec))}s" if eta_sec >= 0 else "--"
    bar = _progress_bar(overall_pct)
    return (
        "📊 <b>LIVE TRANSFER DASHBOARD</b>\n"
        f"📁 <b>Files:</b> Total <code>{total}</code> | Done <code>{delivered}</code> | Remaining <code>{remaining}</code>\n"
        f"📈 <b>Overall Progress:</b> <code>[{bar}] {overall_pct}%</code> (File <code>{idx}/{total}</code>)\n"
        "──────────────────────\n"
        f"⬇️ <b>Download:</b> <code>{dl_pct}%</code> | ⚡ <code>{dl_speed:.2f} MB/s</code>\n"
        f"⬆️ <b>Upload:</b> <code>{ul_pct}%</code> | ⚡ <code>{ul_speed:.2f} MB/s</code>\n"
        f"⏱️ <b>ETA:</b> <code>{eta_str}</code> | 🔄 <b>Status:</b> <code>{phase_label}</code>"
    )


async def _send_media_with_settings(bot, dest_chat, file_path: str, caption_text: str, thumb_path: Optional[str], tg_msg=None) -> None:
    send_kwargs = {"caption": caption_text or None}
    has_thumb = bool(thumb_path and os.path.exists(thumb_path))
    if has_thumb:
        send_kwargs["thumbnail"] = open(thumb_path, "rb")

    try:
        if tg_msg is not None and getattr(tg_msg, "photo", None):
            await bot.send_photo(chat_id=dest_chat, photo=open(file_path, "rb"), caption=caption_text or None)
        elif tg_msg is not None and getattr(tg_msg, "video", None):
            w, h, dur = _extract_video_dims(tg_msg)
            v_kwargs = dict(send_kwargs)
            if w:
                v_kwargs["width"] = w
            if h:
                v_kwargs["height"] = h
            if dur:
                v_kwargs["duration"] = dur
            v_kwargs["supports_streaming"] = True
            await bot.send_video(chat_id=dest_chat, video=open(file_path, "rb"), **v_kwargs)
        elif tg_msg is not None and getattr(tg_msg, "audio", None):
            await bot.send_audio(chat_id=dest_chat, audio=open(file_path, "rb"), **send_kwargs)
        elif tg_msg is not None and getattr(tg_msg, "voice", None):
            await bot.send_voice(chat_id=dest_chat, voice=open(file_path, "rb"), caption=caption_text or None)
        else:
            lower = file_path.lower()
            if lower.endswith((".mp4", ".mkv", ".mov", ".webm", ".avi")):
                v_kwargs = dict(send_kwargs)
                v_kwargs["supports_streaming"] = True
                await bot.send_video(chat_id=dest_chat, video=open(file_path, "rb"), **v_kwargs)
            elif lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
                await bot.send_photo(chat_id=dest_chat, photo=open(file_path, "rb"), caption=caption_text or None)
            else:
                await bot.send_document(chat_id=dest_chat, document=open(file_path, "rb"), **send_kwargs)
    finally:
        if "thumbnail" in send_kwargs and hasattr(send_kwargs["thumbnail"], "close"):
            try:
                send_kwargs["thumbnail"].close()
            except Exception:
                pass


async def process_download_request(bot, chat_id: int, user_id: int, mode_key: str, raw_input: str, settings: dict, status_msg) -> int:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    first_url_match = re.search(r"https?://t\.me/\S+", raw_input or "")
    target_url = first_url_match.group(0) if first_url_match else (raw_input or "").strip()
    peer, base_msg_id, topic_id = _parse_single_link(target_url)

    if peer is None or base_msg_id is None:
        await status_msg.edit_text("❌ मान्य Telegram लिंक भेजें (जैसे `https://t.me/channel/123` या `https://t.me/c/123456/123`)।", parse_mode="Markdown")
        return 0

    is_private_peer = isinstance(peer, int)
    user_client = await get_user_client(user_id) if is_private_peer else None
    if is_private_peer and user_client is None:
        await status_msg.edit_text(
            "🔒 <b>Private Download के लिए पहले Telegram Login करें!</b>\n<b>Private (TG Login)</b> बटन दबाकर अपना फ़ोन नंबर और OTP (तथा 2FA यदि हो) दर्ज करें।",
            parse_mode="HTML",
        )
        return 0

    msg_ids = _build_message_ids(mode_key, raw_input, base_msg_id)
    dest_chat = chat_id
    raw_target = (settings.get("target_chat_id") or "").strip()
    if raw_target:
        dest_chat = int(raw_target) if raw_target.lstrip("-").isdigit() else raw_target

    thumb_path: Optional[str] = None
    thumb_id = (settings.get("thumbnail_file_id") or "").strip()
    if thumb_id:
        try:
            t_file = await bot.get_file(thumb_id)
            thumb_path = os.path.join(DOWNLOAD_DIR, f"thumb_{user_id}_{int(time.time())}.jpg")
            await t_file.download_to_drive(custom_path=thumb_path)
        except Exception:
            thumb_path = None

    delivered = 0
    total = len(msg_ids)
    last_reason = ""
    batch_start = time.perf_counter()
    last_edit_ts = 0.0

    async def _safe_edit_status(text_html: str, force: bool = False) -> None:
        nonlocal last_edit_ts
        now = time.perf_counter()
        if not force and (now - last_edit_ts) < 0.8:
            return
        last_edit_ts = now
        try:
            await status_msg.edit_text(text_html, parse_mode="HTML")
        except Exception:
            pass

    try:
        for idx, mid in enumerate(msg_ids, start=1):
            file_path: Optional[str] = None
            elapsed_batch = max(time.perf_counter() - batch_start, 0.1)
            avg_per_file = elapsed_batch / max(1, idx - 1) if idx > 1 else 2.0
            eta_est = avg_per_file * (total - idx + 1)

            await _safe_edit_status(
                _format_live_status(idx, total, delivered, 35, 4.5, 0, 0.0, eta_est, "Downloading"),
                force=(idx == 1),
            )

            try:
                if is_private_peer and user_client is not None:
                    t0 = time.perf_counter()
                    try:
                        entity = await _resolve_tg_entity(user_id, user_client, peer)
                        tg_msg = await _find_message_with_media(user_client, entity, mid)
                    except FloodWaitError as fw:
                        await asyncio.sleep(min(int(fw.seconds or 1) + 1, 30))
                        continue
                    except AuthKeyUnregisteredError:
                        _delete_session_db(user_id)
                        _CLIENTS.pop(user_id, None)
                        await _safe_edit_status(
                            "⚠️ <b>Telegram Session समाप्त हो गया है।</b>\nकृपया दोबारा TG Login करें।",
                            force=True,
                        )
                        return delivered
                    except ChannelPrivateError:
                        last_reason = "अकाउंट इस Private चैनल/ग्रुप में जुड़ा नहीं है या एक्सेस बंद है।"
                        continue
                    except RPCError as exc:
                        last_reason = f"Telegram त्रुटि: {exc.__class__.__name__}"
                        continue

                    if not tg_msg:
                        last_reason = f"मैसेज ID {mid} नहीं मिला।"
                        continue
                    if not _has_downloadable_media(tg_msg):
                        last_reason = f"मैसेज ID {mid} में कोई मीडिया फ़ाइल नहीं है।"
                        continue

                    caption_text = _apply_text_rules(tg_msg.message or "", settings)
                    base_prefix = os.path.join(DOWNLOAD_DIR, f"{user_id}_{mid}")
                    try:
                        file_path = await user_client.download_media(tg_msg, file=base_prefix)
                    except ChatForwardsRestrictedError:
                        last_reason = "इस चैनल/मैसेज पर फ़ॉरवर्ड/डाउनलोड प्रतिबंध लगा है।"
                        continue
                    except FloodWaitError as fw:
                        await asyncio.sleep(min(int(fw.seconds or 1) + 1, 30))
                        continue
                    except RPCError as exc:
                        last_reason = f"डाउनलोड त्रुटि: {exc.__class__.__name__}"
                        continue

                    if not file_path or not os.path.exists(file_path):
                        last_reason = "मीडिया फ़ाइल डिस्क पर डाउनलोड नहीं हो सकी।"
                        continue

                    dl_time = max(time.perf_counter() - t0, 0.05)
                    fsize_mb = os.path.getsize(file_path) / (1024 * 1024)
                    dl_speed = fsize_mb / dl_time

                    await _safe_edit_status(
                        _format_live_status(idx, total, delivered, 100, dl_speed, 50, dl_speed * 0.95, eta_est, "Uploading"),
                        force=True,
                    )

                    u0 = time.perf_counter()
                    await _send_media_with_settings(bot, dest_chat, file_path, caption_text, thumb_path, tg_msg=tg_msg)
                    ul_time = max(time.perf_counter() - u0, 0.05)
                    ul_speed = fsize_mb / ul_time
                    delivered += 1

                    await _safe_edit_status(
                        _format_live_status(idx, total, delivered, 100, dl_speed, 100, ul_speed, max(0.0, eta_est - ul_time), "Uploaded"),
                        force=True,
                    )
                else:
                    caption_text = _apply_text_rules("", settings)
                    copied_ok = False
                    try:
                        await bot.copy_message(
                            chat_id=dest_chat,
                            from_chat_id=peer,
                            message_id=mid,
                            message_thread_id=topic_id,
                            caption=caption_text or None,
                        )
                        copied_ok = True
                        delivered += 1
                        await _safe_edit_status(
                            _format_live_status(idx, total, delivered, 100, 10.0, 100, 10.0, eta_est, "Uploaded"),
                        )
                    except Exception:
                        copied_ok = False

                    if not copied_ok:
                        if user_client is None:
                            user_client = await get_user_client(user_id)
                        if user_client is not None:
                            t0 = time.perf_counter()
                            try:
                                entity = await _resolve_tg_entity(user_id, user_client, peer)
                                tg_msg = await _find_message_with_media(user_client, entity, mid)
                            except AuthKeyUnregisteredError:
                                _delete_session_db(user_id)
                                _CLIENTS.pop(user_id, None)
                                tg_msg = None
                            except Exception:
                                tg_msg = None

                            if tg_msg and _has_downloadable_media(tg_msg):
                                caption_text = _apply_text_rules(tg_msg.message or "", settings)
                                file_path = await user_client.download_media(tg_msg, file=os.path.join(DOWNLOAD_DIR, f"{user_id}_{mid}_"))
                                if file_path and os.path.exists(file_path):
                                    dl_time = max(time.perf_counter() - t0, 0.05)
                                    fsize_mb = os.path.getsize(file_path) / (1024 * 1024)
                                    dl_speed = fsize_mb / dl_time
                                    await _safe_edit_status(
                                        _format_live_status(idx, total, delivered, 100, dl_speed, 50, dl_speed * 0.95, eta_est, "Uploading"),
                                    )
                                    u0 = time.perf_counter()
                                    await _send_media_with_settings(bot, dest_chat, file_path, caption_text, thumb_path, tg_msg=tg_msg)
                                    ul_speed = fsize_mb / max(time.perf_counter() - u0, 0.05)
                                    delivered += 1
                                    await _safe_edit_status(
                                        _format_live_status(idx, total, delivered, 100, dl_speed, 100, ul_speed, eta_est, "Uploaded"),
                                    )
            finally:
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass

        if delivered > 0:
            rem_final = max(0, total - delivered)
            await _safe_edit_status(
                "📊 <b>LIVE TRANSFER DASHBOARD — COMPLETE</b>\n"
                f"📁 <b>Files:</b> Total <code>{total}</code> | Done <code>{delivered}</code> | Remaining <code>{rem_final}</code>\n"
                f"📈 <b>Overall Progress:</b> <code>[{_progress_bar(100)}] 100%</code>\n"
                "──────────────────────\n"
                "✅ <b>सभी उपलब्ध मीडिया फ़ाइलें सफलतापूर्वक भेज दी गईं।</b>",
                force=True,
            )
        else:
            detail = f"\n<b>कारण:</b> {last_reason}" if last_reason else ""
            await _safe_edit_status(
                "⚠️ <b>कोई मीडिया/वीडियो नहीं भेजा जा सका।</b>"
                f"{detail}\nजाँचें कि लिंक सही है, वीडियो मौजूद है, या लॉगिन किए हुए Telegram अकाउंट की उस Private चैनल तक पहुँच है।",
                force=True,
            )
        return delivered
    finally:
        if thumb_path and os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
            except Exception:
                pass
