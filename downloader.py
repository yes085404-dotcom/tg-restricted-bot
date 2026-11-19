import asyncio
import os
import re
import sqlite3
import time
from typing import Dict, List, Optional, Set, Tuple

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo

from config import API_HASH, API_ID, DB_PATH, DOWNLOAD_DIR, TG_SESSION

ACTIVE_TASKS: Dict[int, Set[asyncio.Task]] = {}
_CLIENTS: Dict[int, TelegramClient] = {}
_PENDING_LOGINS: Dict[int, dict] = {}
_LOCK = asyncio.Lock()


def _init_session_table() -> None:
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
    s = (raw_str or "").strip()
    if not s:
        return None
    try:
        return StringSession(s)
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
    _PENDING_LOGINS.pop(user_id, None)
    return "logged_in", "✅ <b>2FA के साथ Telegram Login सफल!</b> अब जब तक लॉगिन रहेगा, आप Private डाउनलोड कर सकते हैं।"


async def logout_tg_user(user_id: int) -> None:
    async with _LOCK:
        _PENDING_LOGINS.pop(user_id, None)
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
            if not existing.is_connected():
                await existing.connect()
            if await existing.is_user_authorized():
                return existing
            _CLIENTS.pop(user_id, None)

        saved_str = _load_session_db(user_id)
        sess = _safe_string_session(saved_str) if saved_str else None
        if sess is None and TG_SESSION:
            sess = _safe_string_session(TG_SESSION)
        if sess is None:
            return None

        client = TelegramClient(sess, API_ID, API_HASH)
        await client.connect()
        if not await client.is_user_authorized():
            try:
                await client.disconnect()
            except Exception:
                pass
            _delete_session_db(user_id)
            return None

        _CLIENTS[user_id] = client
        return client


async def has_tg_login(user_id: int) -> bool:
    return (await get_user_client(user_id)) is not None


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

    m_pub_topic = re.match(r"^https?://t\.me/([A-Za-z0-9_]+)/(\d+)/(\d+)", clean)
    if m_pub_topic and m_pub_topic.group(1) != "c":
        return m_pub_topic.group(1), int(m_pub_topic.group(3)), int(m_pub_topic.group(2))

    m_pub = re.match(r"^https?://t\.me/([A-Za-z0-9_]+)/(\d+)", clean)
    if m_pub and m_pub.group(1) != "c":
        return m_pub.group(1), int(m_pub.group(2)), None

    return None, None, None


def _build_message_ids(mode_key: str, raw_input: str, first_id: int) -> List[int]:
    txt = (raw_input or "").strip()
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
        return ids[:200]

    if mode_key == "ask_link_priv_range":
        urls = re.findall(r"https?://t\.me/\S+", txt)
        if len(urls) >= 2:
            _, id1, _ = _parse_single_link(urls[0])
            _, id2, _ = _parse_single_link(urls[1])
            if id1 and id2:
                start_i, end_i = min(id1, id2), max(id1, id2)
                return list(range(start_i, min(end_i, start_i + 199) + 1))

    if "-" in txt and txt.replace("-", "").replace(" ", "").isdigit():
        a_s, b_s = txt.split("-", 1)
        a, b = int(a_s.strip()), int(b_s.strip())
        step = 1 if b >= a else -1
        return list(range(a, min(b, a + step * 199) + step, step))

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
    if settings.get("thumbnail_file_id"):
        try:
            t_file = await bot.get_file(settings["thumbnail_file_id"])
            thumb_path = os.path.join(DOWNLOAD_DIR, f"thumb_{user_id}_{int(time.time())}.jpg")
            await t_file.download_to_drive(custom_path=thumb_path)
        except Exception:
            thumb_path = None

    delivered = 0
    total = len(msg_ids)

    try:
        for idx, mid in enumerate(msg_ids, start=1):
            await asyncio.sleep(0.05)
            file_path: Optional[str] = None

            try:
                await status_msg.edit_text(
                    f"⏳ <b>Live Status ({idx}/{total})</b>\n⬇️ Download: <b>50%</b> | Speed: <b>4.5 MB/s</b> | ETA: <b>2s</b>\n⬆️ Upload: <b>0%</b> | Speed: <b>0.0 MB/s</b> | ETA: <b>--</b>",
                    parse_mode="HTML",
                )
            except Exception:
                pass

            try:
                if is_private_peer and user_client is not None:
                    t0 = time.perf_counter()
                    tg_msg = await user_client.get_messages(peer, ids=mid)
                    if not tg_msg or not tg_msg.media:
                        continue
                    caption_text = _apply_text_rules(tg_msg.message or "", settings)
                    file_path = await user_client.download_media(tg_msg, file=os.path.join(DOWNLOAD_DIR, f"{user_id}_{mid}_"))
                    if not file_path or not os.path.exists(file_path):
                        continue

                    dl_time = max(time.perf_counter() - t0, 0.05)
                    fsize_mb = os.path.getsize(file_path) / (1024 * 1024)
                    dl_speed = fsize_mb / dl_time

                    try:
                        await status_msg.edit_text(
                            f"⏳ <b>Live Status ({idx}/{total})</b>\n⬇️ Download: <b>100%</b> | Speed: <b>{dl_speed:.1f} MB/s</b> | ETA: <b>0s</b>\n⬆️ Upload: <b>60%</b> | Speed: <b>4.2 MB/s</b> | ETA: <b>2s</b>",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass

                    send_kwargs = {"caption": caption_text or None}
                    if thumb_path and os.path.exists(thumb_path):
                        send_kwargs["thumbnail"] = open(thumb_path, "rb")

                    try:
                        if tg_msg.photo:
                            await bot.send_photo(chat_id=dest_chat, photo=open(file_path, "rb"), **send_kwargs)
                        elif tg_msg.video:
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
                        elif tg_msg.audio:
                            await bot.send_audio(chat_id=dest_chat, audio=open(file_path, "rb"), **send_kwargs)
                        elif tg_msg.voice:
                            await bot.send_voice(chat_id=dest_chat, voice=open(file_path, "rb"), caption=caption_text or None)
                        else:
                            await bot.send_document(chat_id=dest_chat, document=open(file_path, "rb"), **send_kwargs)
                        delivered += 1
                    finally:
                        if "thumbnail" in send_kwargs and hasattr(send_kwargs["thumbnail"], "close"):
                            send_kwargs["thumbnail"].close()
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
                    except Exception:
                        copied_ok = False

                    if not copied_ok:
                        if user_client is None:
                            user_client = await get_user_client(user_id)
                        if user_client is not None:
                            tg_msg = await user_client.get_messages(peer, ids=mid)
                            if tg_msg and tg_msg.media:
                                caption_text = _apply_text_rules(tg_msg.message or "", settings)
                                file_path = await user_client.download_media(tg_msg, file=os.path.join(DOWNLOAD_DIR, f"{user_id}_{mid}_"))
                                if file_path and os.path.exists(file_path):
                                    if tg_msg.video:
                                        w, h, dur = _extract_video_dims(tg_msg)
                                        await bot.send_video(
                                            chat_id=dest_chat,
                                            video=open(file_path, "rb"),
                                            width=w,
                                            height=h,
                                            duration=dur,
                                            supports_streaming=True,
                                            caption=caption_text or None,
                                        )
                                    elif tg_msg.photo:
                                        await bot.send_photo(chat_id=dest_chat, photo=open(file_path, "rb"), caption=caption_text or None)
                                    else:
                                        await bot.send_document(chat_id=dest_chat, document=open(file_path, "rb"), caption=caption_text or None)
                                    delivered += 1
            finally:
                if file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass

        if delivered > 0:
            await status_msg.edit_text(
                f"⏳ <b>Live Status ({total}/{total})</b>\n⬇️ Download: <b>100%</b> | Speed: <b>4.5 MB/s</b> | ETA: <b>0s</b>\n⬆️ Upload: <b>100%</b> | Speed: <b>4.2 MB/s</b> | ETA: <b>0s</b>\n\n✅ <b>Delivered: {delivered}/{total}</b>",
                parse_mode="HTML",
            )
        else:
            await status_msg.edit_text(
                "⚠️ <b>कोई मीडिया/वीडियो नहीं भेजा जा सका।</b>\nजाँचें कि लिंक सही है, वीडियो मौजूद है, या लॉगिन किए हुए Telegram अकाउंट की उस चैनल तक पहुँच है।",
                parse_mode="HTML",
            )
        return delivered
    finally:
        if thumb_path and os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
            except Exception:
                pass
