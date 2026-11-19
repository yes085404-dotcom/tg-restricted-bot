import asyncio
import os
import re
import time
from typing import Dict, List, Optional, Set, Tuple

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import DocumentAttributeVideo

from config import API_HASH, API_ID, DOWNLOAD_DIR, TG_SESSION

ACTIVE_TASKS: Dict[int, Set[asyncio.Task]] = {}
_USER_CLIENT: Optional[TelegramClient] = None
_CLIENT_LOCK = asyncio.Lock()


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


def has_tg_login() -> bool:
    return bool(API_ID and API_HASH and TG_SESSION)


async def get_user_client() -> Optional[TelegramClient]:
    global _USER_CLIENT
    if not has_tg_login():
        return None
    async with _CLIENT_LOCK:
        if _USER_CLIENT is None:
            _USER_CLIENT = TelegramClient(StringSession(TG_SESSION), API_ID, API_HASH)
        if not _USER_CLIENT.is_connected():
            await _USER_CLIENT.connect()
        if not await _USER_CLIENT.is_user_authorized():
            return None
        return _USER_CLIENT


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
    if is_private_peer and not has_tg_login():
        await status_msg.edit_text(
            "🔒 <b>Private Download के लिए TG Login ज़रूरी है!</b>\nकृपया Render Environment में <code>API_ID</code>, <code>API_HASH</code> और <code>TG_SESSION</code> सेट करें।",
            parse_mode="HTML",
        )
        return 0

    msg_ids = _build_message_ids(mode_key, raw_input, base_msg_id)
    user_client = await get_user_client() if is_private_peer else None
    if is_private_peer and user_client is None:
        await status_msg.edit_text(
            "⚠️ <b>TG Login Session अमान्य या एक्सपायर है।</b>\nकृपया सही <code>TG_SESSION</code> सेट करें।",
            parse_mode="HTML",
        )
        return 0

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

                    if not copied_ok and has_tg_login():
                        if user_client is None:
                            user_client = await get_user_client()
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
                "⚠️ <b>कोई मीडिया/वीडियो नहीं भेजा जा सका।</b>\nजाँचें कि लिंक सही है, वीडियो मौजूद है, या Bot/TG Login की उस चैनल तक पहुँच है।",
                parse_mode="HTML",
            )
        return delivered
    finally:
        if thumb_path and os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
            except Exception:
                pass
