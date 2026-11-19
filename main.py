import asyncio
import json
import logging
import os
import sys
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from admin_panel import (
    PLANS,
    format_remaining,
    handle_admin_callback,
    handle_admin_text_input,
    open_admin_panel,
    start_health_server,
)
from config import (
    ADMIN_USERNAME,
    AD_REWARD_HOURS,
    AROLINKS_URL,
    BOT_TOKEN,
    BOT_USERNAME,
    DOWNLOAD_DIR,
    FREE_LIFETIME_LIMIT,
    OWNER_ID,
    PORT,
)
from database import Database
from downloader import (
    cancel_user_tasks,
    has_tg_login,
    logout_tg_user,
    process_download_request,
    register_user_task,
    start_tg_phone_login,
    submit_tg_2fa_password,
    submit_tg_otp_code,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("tg_restricted_bot")
db = Database()


def main_menu_keyboard(user_id: int, lang: str) -> InlineKeyboardMarkup:
    hi = lang != "en"
    rows = [
        [
            InlineKeyboardButton("📥 Single Media (सिंगल)" if hi else "📥 Single Media", callback_data="mode_single"),
            InlineKeyboardButton("📦 Batch Download (बैच)" if hi else "📦 Batch Download", callback_data="mode_batch"),
        ],
        [
            InlineKeyboardButton("🧵 Group Topics", callback_data="mode_topics"),
            InlineKeyboardButton("⚙️ Custom Settings", callback_data="custom_settings"),
        ],
        [
            InlineKeyboardButton("🛑 Stop Tasks (रोकें)" if hi else "🛑 Stop Tasks", callback_data="stop_tasks"),
            InlineKeyboardButton("🎁 Ad Verification (3h)", callback_data="ad_verify"),
        ],
        [
            InlineKeyboardButton("💎 Premium Plans", callback_data="premium_plans"),
            InlineKeyboardButton("🎟️ Redeem Code", callback_data="redeem_code"),
        ],
        [
            InlineKeyboardButton("⚡ Speed Test", callback_data="speed_test"),
            InlineKeyboardButton("📊 My Limits", callback_data="my_limits"),
        ],
        [
            InlineKeyboardButton("📖 Help & Guide", callback_data="help_guide"),
            InlineKeyboardButton("🌐 हिंदी / English", callback_data="toggle_lang"),
        ],
        [InlineKeyboardButton(f"👨‍💻 Admin (@{ADMIN_USERNAME})", url=f"https://t.me/{ADMIN_USERNAME}")],
    ]
    if db.is_admin(user_id, OWNER_ID):
        rows.append([InlineKeyboardButton("🔐 Admin Login", callback_data="admin_login")])
    return InlineKeyboardMarkup(rows)


def custom_settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎯 Target Chat ID", callback_data="set_target"), InlineKeyboardButton("🖼️ Thumbnail", callback_data="set_thumb")],
            [InlineKeyboardButton("🏷️ Rename Tag", callback_data="set_rename"), InlineKeyboardButton("📋 Custom Caption", callback_data="set_caption")],
            [InlineKeyboardButton("🔄 Replace Words", callback_data="set_replace"), InlineKeyboardButton("🗑️ Delete Words", callback_data="set_delete")],
            [InlineKeyboardButton("♻️ Reset All", callback_data="set_reset"), InlineKeyboardButton("⬅️ Back", callback_data="back_main")],
        ]
    )


async def check_force_join(bot, user_id: int) -> list[dict]:
    if db.get_setting("force_join_enabled", "1") != "1" or db.is_admin(user_id, OWNER_ID):
        return []
    chats = db.get_force_chats()
    if not chats:
        return []

    async def _one_check(ch: dict) -> dict | None:
        cid = ch["chat_id"]
        try:
            target = int(cid) if cid.lstrip("-").isdigit() else cid
            member = await asyncio.wait_for(bot.get_chat_member(target, user_id), timeout=3.0)
            if member.status in ("left", "kicked"):
                return ch
            return None
        except Exception:
            return ch

    results = await asyncio.gather(*[_one_check(c) for c in chats], return_exceptions=False)
    return [r for r in results if r is not None]


async def send_force_join_prompt(update: Update, missing: list[dict]) -> None:
    buttons = []
    for ch in missing:
        url = ch.get("invite_link") or (f"https://t.me/{ch['chat_id'].lstrip('@')}" if str(ch["chat_id"]).startswith("@") else "")
        if url:
            buttons.append([InlineKeyboardButton(f"➕ Join {ch.get('title') or ch['chat_id']}", url=url)])
    buttons.append([InlineKeyboardButton("✅ Verify", callback_data="verify_join")])
    txt = "🔒 <b>सभी Channel / Group में Join करें और नीचे Verify दबाएँ:</b>"
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
    elif update.message:
        await update.message.reply_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))


async def show_custom_settings(update: Update, user_id: int) -> None:
    s = db.get_user_settings(user_id)
    rep = s["replace_words"]
    dlt = s["delete_words"]
    txt = (
        "⚙️ <b>CUSTOM SETTINGS</b>\n\n"
        f"📌 <b>टारगेट चैट ID:</b> {s['target_chat_id'] or '❌ सेट नहीं (बॉट चैट में आएगा)'}\n"
        f"🖼️ <b>थंबनेल:</b> {'✅ सेट है' if s['thumbnail_file_id'] else '❌ सेट नहीं है'}\n"
        f"🏷️ <b>रीनेम टैग:</b> {s['rename_tag'] or '❌ सेट नहीं है'}\n"
        f"📋 <b>कस्टम कैप्शन:</b> {s['custom_caption'] or '❌ सेट नहीं है'}\n"
        f"🔄 <b>रिप्लेस वर्ड्स:</b> {', '.join([f'{a}->{b}' for a, b in rep]) if rep else '❌ कोई नहीं'}\n"
        f"🗑️ <b>डिलीट वर्ड्स:</b> {', '.join(dlt) if dlt else '❌ कोई नहीं'}"
    )
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.edit_text(txt, parse_mode="HTML", reply_markup=custom_settings_keyboard())
    elif update.message:
        await update.message.reply_text(txt, parse_mode="HTML", reply_markup=custom_settings_keyboard())


async def _prompt_tg_login(message, user_id: int, next_action: str | None = None) -> None:
    context_key = next_action or ""
    logged = await has_tg_login(user_id)
    rows = [
        [InlineKeyboardButton("📱 Telegram Login करें", callback_data=f"tg_do_login|{context_key}")],
    ]
    if logged:
        rows.append([InlineKeyboardButton("🚪 TG Logout करें", callback_data="tg_do_logout")])
    txt = (
        "🔐 <b>Telegram Login</b>\n\n"
        f"स्थिति: <b>{'✅ Logged In (Private डाउनलोड चालू)' if logged else '❌ Logged Out'}</b>\n"
        "लॉगिन करने के लिए नीचे <b>Telegram Login करें</b> दबाएँ और अपना फ़ोन नंबर + OTP (तथा 2FA यदि लगा हो) भेजें।"
    )
    await message.reply_text(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    u = db.ensure_user(user.id, user.username or "", user.full_name or "")
    if u.get("is_banned"):
        await update.message.reply_text(f"🚫 Banned: {u.get('ban_reason') or 'N/A'}")
        return
    if db.get_setting("maintenance_mode", "0") == "1" and not db.is_admin(user.id, OWNER_ID):
        await update.message.reply_text("🛠️ Bot अभी Maintenance में है।")
        return

    if context.args and "ad_verify" in context.args[0]:
        until = db.grant_premium(user.id, float(AD_REWARD_HOURS), "ad", f"Ad Pass ({AD_REWARD_HOURS}h)")
        await update.message.reply_text(
            f"🎉 <b>{AD_REWARD_HOURS} GHANTE TAK PREMIUM UNLOCK HUA</b>\n⏳ शेष समय: <b>{format_remaining(until)}</b>",
            parse_mode="HTML",
        )

    missing = await check_force_join(context.bot, user.id)
    if missing:
        await send_force_join_prompt(update, missing)
        return

    lang = u.get("lang", "hi")
    msg = (
        f"👋 <b>स्वागत है, {user.first_name}!</b>\n🤖 <b>TG RESTRICTED CONTENT BOT</b> (<code>@{BOT_USERNAME}</code>)"
        if lang != "en"
        else f"👋 <b>Welcome, {user.first_name}!</b>\n🤖 <b>TG RESTRICTED CONTENT BOT</b> (<code>@{BOT_USERNAME}</code>)"
    )
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=main_menu_keyboard(user.id, lang))


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not query.message:
        return

    data = query.data or ""
    try:
        await query.answer()
    except Exception:
        pass

    u = db.ensure_user(user.id, user.username or "", user.full_name or "")
    if u.get("is_banned"):
        await query.message.reply_text(f"🚫 Banned: {u.get('ban_reason') or 'N/A'}")
        return

    if await handle_admin_callback(update, context, db, OWNER_ID):
        return

    if data == "verify_join":
        missing = await check_force_join(context.bot, user.id)
        if missing:
            await query.message.reply_text("⚠️ अभी भी कुछ Channel/Group में Join होना बाकी है।")
            return
        await query.message.reply_text("✅ वेरिफ़िकेशन सफल!", reply_markup=main_menu_keyboard(user.id, u.get("lang", "hi")))
        return

    if data == "admin_login" and db.is_admin(user.id, OWNER_ID):
        await open_admin_panel(update, context, db, OWNER_ID)
        return

    if data.startswith("tg_do_login|"):
        next_act = data.split("|", 1)[1].strip() or None
        context.user_data["tg_next_action"] = next_act
        context.user_data["awaiting"] = "tg_login_phone"
        await query.message.reply_text(
            "📱 अपना <b>Telegram फ़ोन नंबर</b> देश के कोड के साथ भेजें (जैसे <code>+919876543210</code>):",
            parse_mode="HTML",
        )
        return

    if data == "tg_do_logout":
        await logout_tg_user(user.id)
        context.user_data["awaiting"] = None
        context.user_data["tg_next_action"] = None
        await query.message.reply_text("🚪 <b>TG Logout हो गया।</b>", parse_mode="HTML")
        return

    if data == "back_main":
        await query.message.edit_text("🏠 <b>Main Dashboard</b>", parse_mode="HTML", reply_markup=main_menu_keyboard(user.id, u.get("lang", "hi")))
        return

    if data == "toggle_lang":
        new_lang = "en" if u.get("lang") == "hi" else "hi"
        db.set_lang(user.id, new_lang)
        await query.message.edit_text(f"🌐 Language: <b>{new_lang.upper()}</b>", parse_mode="HTML", reply_markup=main_menu_keyboard(user.id, new_lang))
        return

    if data == "stop_tasks":
        context.user_data["awaiting"] = None
        context.user_data["tg_next_action"] = None
        context.user_data.pop("first_range_link", None)
        stopped = cancel_user_tasks(user.id)
        await query.message.reply_text(f"🛑 चल रहे टास्क रोक दिए गए हैं ({stopped} cancelled)।")
        return

    if data == "ad_verify":
        if db.get_setting("ad_enabled", "1") != "1":
            await query.message.reply_text("⚠️ Ad Verification अभी बंद है।")
            return
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔗 Open AroLinks Ad", url=AROLINKS_URL)]])
        await query.message.reply_text(
            "🎁 <b>Ad Verification</b>\n\n"
            "1. नीचे दिए गए <b>Open AroLinks Ad</b> बटन पर क्लिक करें।\n"
            "2. AroLinks के सभी स्टेप्स पूरे करके <b>Get Link</b> दबाएँ।\n"
            "3. Bot में वापस आते ही <b>3 GHANTE TAK PREMIUM UNLOCK HUA</b> का मैसेज आ जाएगा।",
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    if data == "premium_plans":
        rows = [[InlineKeyboardButton(f"💎 {label}", callback_data=f"req_plan_{key}")] for key, label, _ in PLANS]
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="back_main")])
        await query.message.edit_text("💎 <b>Premium Plans</b> चुनें:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("req_plan_"):
        key = data.replace("req_plan_", "", 1)
        m = next((p for p in PLANS if p[0] == key), None)
        if m:
            req_id = db.create_plan_request(user.id, user.username or "", key, m[1])
            if OWNER_ID:
                try:
                    await context.bot.send_message(
                        OWNER_ID,
                        f"📩 Request #{req_id} | ID: <code>{user.id}</code> | @{user.username or 'N/A'} | <b>{m[1]}</b>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
            await query.message.reply_text(f"✅ प्लान अनुरोध (<b>{m[1]}</b>) Admin को भेज दिया गया है।", parse_mode="HTML")
        return

    if data == "redeem_code":
        context.user_data["awaiting"] = "user_redeem_code"
        await query.message.reply_text("🎟️ Redeem Code भेजें:")
        return

    if data == "my_limits":
        fresh = db.get_user(user.id) or {}
        prem_until = float(fresh.get("premium_until") or 0)
        used = int(fresh.get("used_free_files") or 0)
        if prem_until > time.time():
            txt = f"👑 <b>Active Premium</b>\n📦 Plan: <b>{fresh.get('premium_plan_label')}</b>\n⏳ बाकी समय: <b>{format_remaining(prem_until)}</b>"
        else:
            txt = f"🆓 <b>Free Plan</b> ({FREE_LIFETIME_LIMIT} Lifetime)\n📥 इस्तेमाल: {used}/{FREE_LIFETIME_LIMIT} | बाकी: <b>{max(0, FREE_LIFETIME_LIMIT - used)}</b>"
        await query.message.reply_text(f"📊 <b>MY LIMITS</b>\n\n{txt}", parse_mode="HTML")
        return

    if data == "speed_test":
        t0 = time.perf_counter()
        await context.bot.get_me()
        ms = (time.perf_counter() - t0) * 1000.0
        await query.message.reply_text(f"⚡ <b>Speed Test</b>\nPing: <b>{ms:.1f} ms</b> | Status: <b>Online</b>", parse_mode="HTML")
        return

    if data == "help_guide":
        await query.message.reply_text(
            "📖 <b>Help & Guide</b>\n• Free Lifetime Limit: <b>10 Files</b>\n• Ad Verification: <b>3 Hours</b> Premium\n• Custom Settings से Target Chat, Thumbnail, Rename Tag, Caption, Replace/Delete Words सेट करें।",
            parse_mode="HTML",
        )
        return

    if data == "custom_settings":
        await show_custom_settings(update, user.id)
        return

    set_prompts = {
        "set_target": ("set_target_chat_id", "🎯 Target Chat ID / `@username` या `clear` भेजें:"),
        "set_thumb": ("set_thumbnail", "🖼️ Thumbnail फोटो या `clear` भेजें:"),
        "set_rename": ("set_rename_tag", "🏷️ Rename Tag या `clear` भेजें:"),
        "set_caption": ("set_custom_caption", "📋 Custom Caption या `clear` भेजें:"),
        "set_replace": ("set_replace_words", "🔄 `Old->New` या `clear` भेजें:"),
        "set_delete": ("set_delete_words", "🗑️ हटाने वाले शब्द (comma से अलग) या `clear` भेजें:"),
    }
    if data in set_prompts:
        context.user_data["awaiting"], prompt_txt = set_prompts[data]
        await query.message.reply_text(prompt_txt, parse_mode="Markdown")
        return

    if data == "set_reset":
        db.reset_user_settings(user.id)
        await show_custom_settings(update, user.id)
        return

    if data in ("mode_single", "mode_batch", "mode_topics"):
        ok, _ = db.can_download(user.id)
        if not ok:
            await query.message.reply_text(
                f"⚠️ आपकी <b>{FREE_LIFETIME_LIMIT} फ़ाइल्स</b> की फ्री सीमा समाप्त हो गई है। Ad Verification या Premium लें।",
                parse_mode="HTML",
            )
            return
        if data == "mode_single":
            await query.message.reply_text(
                "📥 <b>Single Media</b> चुनें:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🌐 Public", callback_data="ask_link_pub_single"), InlineKeyboardButton("🔒 Private (TG Login)", callback_data="ask_link_priv_single")]]
                ),
            )
        elif data == "mode_batch":
            await query.message.reply_text(
                "📦 <b>Batch Download</b> चुनें:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🌐 Public Batch", callback_data="pub_batch_opts"), InlineKeyboardButton("🔒 Private Batch", callback_data="priv_batch_opts")],
                    ]
                ),
            )
        else:
            context.user_data["awaiting"] = "ask_link_topic"
            await query.message.reply_text("🧵 Group Topic लिंक भेजें:")
        return

    if data == "pub_batch_opts":
        await query.message.reply_text(
            "🌐 <b>Public Batch</b>:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("📂 All Media Download", callback_data="ask_link_pub_all")],
                    [InlineKeyboardButton("🔢 Custom (1,5,8 / 1-50)", callback_data="ask_link_pub_custom")],
                    [InlineKeyboardButton("🔗 Link Download", callback_data="ask_link_pub_link")],
                    [InlineKeyboardButton("↔️ First and Last Link", callback_data="ask_link_pub_first_last")],
                    [InlineKeyboardButton("🧵 Group Topic Download", callback_data="ask_link_pub_topic")],
                ]
            ),
        )
        return

    if data == "priv_batch_opts":
        await query.message.reply_text(
            "🔒 <b>Private Batch (TG Login)</b>:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🧵 Group Topic Download", callback_data="ask_link_priv_topic")],
                    [InlineKeyboardButton("🔗 Link Download", callback_data="ask_link_priv_link")],
                    [InlineKeyboardButton("↔️ First and Last Link", callback_data="ask_link_priv_first_last")],
                ]
            ),
        )
        return

    if data.startswith("ask_link_"):
        if "priv_" in data and not await has_tg_login(user.id):
            await _prompt_tg_login(query.message, user.id, next_action=data)
            return
        if "first_last" in data or "range" in data:
            context.user_data.pop("first_range_link", None)
            context.user_data["range_mode_key"] = data
            context.user_data["awaiting"] = "await_first_link"
            await query.message.reply_text("1️⃣ कृपया <b>पहले मीडिया का लिंक</b> भेजें:", parse_mode="HTML")
            return
        context.user_data["awaiting"] = data
        await query.message.reply_text("🔗 लिंक / विवरण भेजें:")
        return


async def _run_download_job(bot, chat_id: int, user_id: int, awaiting_mode: str, text: str, mode_type: str, pmsg) -> None:
    try:
        settings = db.get_user_settings(user_id)
        delivered = await process_download_request(bot, chat_id, user_id, awaiting_mode, text, settings, pmsg)
        if delivered > 0 and mode_type == "free":
            db.increment_free_usage(user_id, delivered)
    except asyncio.CancelledError:
        try:
            await pmsg.edit_text("🛑 टास्क रोक दिया गया।")
        except Exception:
            pass
    except Exception as exc:
        logger.exception("Download job failed: %s", exc)
        try:
            await pmsg.edit_text(f"⚠️ डाउनलोड में त्रुटि आई: <code>{str(exc)[:180]}</code>", parse_mode="HTML")
        except Exception:
            pass


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return
    if await handle_admin_text_input(update, context, db, OWNER_ID):
        return

    awaiting = context.user_data.get("awaiting")
    text = (msg.text or "").strip()

    if awaiting == "set_thumbnail":
        if msg.photo:
            db.update_user_setting(user.id, "thumbnail_file_id", msg.photo[-1].file_id)
            context.user_data["awaiting"] = None
            await show_custom_settings(update, user.id)
            return
        if text.lower() == "clear":
            db.update_user_setting(user.id, "thumbnail_file_id", "")
            context.user_data["awaiting"] = None
            await show_custom_settings(update, user.id)
            return

    if not awaiting:
        return

    if awaiting == "tg_login_phone":
        try:
            state, reply_txt = await start_tg_phone_login(user.id, text)
            if state == "code":
                context.user_data["awaiting"] = "tg_login_otp"
            else:
                context.user_data["awaiting"] = None
            await msg.reply_text(reply_txt, parse_mode="HTML")
        except Exception as exc:
            context.user_data["awaiting"] = None
            await msg.reply_text(f"⚠️ Login त्रुटि: <code>{str(exc)[:180]}</code>", parse_mode="HTML")
        return

    if awaiting == "tg_login_otp":
        try:
            state, reply_txt = await submit_tg_otp_code(user.id, text)
            if state == "2fa":
                context.user_data["awaiting"] = "tg_login_2fa"
                await msg.reply_text(reply_txt, parse_mode="HTML")
            elif state == "logged_in":
                next_act = context.user_data.pop("tg_next_action", None)
                if next_act and ("first_last" in next_act or "range" in next_act):
                    context.user_data.pop("first_range_link", None)
                    context.user_data["range_mode_key"] = next_act
                    context.user_data["awaiting"] = "await_first_link"
                    await msg.reply_text(reply_txt, parse_mode="HTML")
                    await msg.reply_text("1️⃣ कृपया <b>पहले मीडिया का लिंक</b> भेजें:", parse_mode="HTML")
                else:
                    context.user_data["awaiting"] = next_act
                    await msg.reply_text(reply_txt, parse_mode="HTML")
                    if next_act:
                        await msg.reply_text("🔗 अब Private लिंक / विवरण भेजें:")
            else:
                context.user_data["awaiting"] = None
                await msg.reply_text(reply_txt, parse_mode="HTML")
        except Exception as exc:
            context.user_data["awaiting"] = None
            await msg.reply_text(f"⚠️ OTP त्रुटि: <code>{str(exc)[:180]}</code>", parse_mode="HTML")
        return

    if awaiting == "tg_login_2fa":
        try:
            state, reply_txt = await submit_tg_2fa_password(user.id, text)
            if state == "2fa":
                await msg.reply_text(reply_txt, parse_mode="HTML")
                return
            next_act = context.user_data.pop("tg_next_action", None)
            if next_act and ("first_last" in next_act or "range" in next_act):
                context.user_data.pop("first_range_link", None)
                context.user_data["range_mode_key"] = next_act
                context.user_data["awaiting"] = "await_first_link"
                await msg.reply_text(reply_txt, parse_mode="HTML")
                await msg.reply_text("1️⃣ कृपया <b>पहले मीडिया का लिंक</b> भेजें:", parse_mode="HTML")
            else:
                context.user_data["awaiting"] = next_act
                await msg.reply_text(reply_txt, parse_mode="HTML")
                if next_act:
                    await msg.reply_text("🔗 अब Private लिंक / विवरण भेजें:")
        except Exception as exc:
            context.user_data["awaiting"] = None
            await msg.reply_text(f"⚠️ 2FA त्रुटि: <code>{str(exc)[:180]}</code>", parse_mode="HTML")
        return

    if awaiting == "await_first_link":
        context.user_data["first_range_link"] = text
        context.user_data["awaiting"] = "await_last_link"
        await msg.reply_text("2️⃣ अब <b>अंतिम मीडिया का लिंक</b> भेजें:", parse_mode="HTML")
        return

    if awaiting == "await_last_link":
        first_link = context.user_data.pop("first_range_link", "")
        range_key = context.user_data.pop("range_mode_key", "first_last_range")
        context.user_data["awaiting"] = None
        combined = f"{first_link}|{text}"
        awaiting = range_key
        text = combined
    else:
        context.user_data["awaiting"] = None

    if awaiting == "user_redeem_code":
        ok, code_err, hrs = db.redeem_code(user.id, text)
        if ok:
            fresh = db.get_user(user.id) or {}
            await msg.reply_text(
                f"🎉 Redeem सफल! <b>{hrs}h</b> Premium चालू।\n⏳ बाकी समय: <b>{format_remaining(float(fresh.get('premium_until') or 0))}</b>",
                parse_mode="HTML",
            )
        else:
            await msg.reply_text(f"❌ Code अमान्य/समाप्त (`{code_err}`)", parse_mode="Markdown")
        return

    simple_map = {
        "set_target_chat_id": ("target_chat_id", "" if text.lower() == "clear" else text),
        "set_rename_tag": ("rename_tag", "" if text.lower() == "clear" else text),
        "set_custom_caption": ("custom_caption", "" if text.lower() == "clear" else text),
    }
    if awaiting in simple_map:
        col, val = simple_map[awaiting]
        db.update_user_setting(user.id, col, val)
        await show_custom_settings(update, user.id)
        return

    if awaiting == "set_replace_words":
        if text.lower() == "clear":
            db.update_user_setting(user.id, "replace_words_json", "[]")
        elif "->" in text:
            s = db.get_user_settings(user.id)
            pairs = s["replace_words"]
            a, b = text.split("->", 1)
            pairs.append([a.strip(), b.strip()])
            db.update_user_setting(user.id, "replace_words_json", json.dumps(pairs))
        await show_custom_settings(update, user.id)
        return

    if awaiting == "set_delete_words":
        if text.lower() == "clear":
            db.update_user_setting(user.id, "delete_words_json", "[]")
        else:
            s = db.get_user_settings(user.id)
            words = s["delete_words"]
            for w in [x.strip() for x in text.split(",") if x.strip()]:
                if w not in words:
                    words.append(w)
            db.update_user_setting(user.id, "delete_words_json", json.dumps(words))
        await show_custom_settings(update, user.id)
        return

    ok, mode_type = db.can_download(user.id)
    if not ok:
        await msg.reply_text(f"⚠️ आपकी <b>{FREE_LIFETIME_LIMIT} फ़ाइल्स</b> की फ्री सीमा समाप्त हो गई है।", parse_mode="HTML")
        return

    pmsg = await msg.reply_text(
        "📊 <b>LIVE TRANSFER DASHBOARD</b>\n📁 <b>Files:</b> Starting...\n🔄 <b>Status:</b> <code>Initializing</code>",
        parse_mode="HTML",
    )
    job = asyncio.create_task(_run_download_job(context.bot, msg.chat_id, user.id, awaiting, text, mode_type, pmsg))
    register_user_task(user.id, job)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Error: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("⚠️ अस्थायी एरर आया था; Bot चालू है।")
    except TelegramError:
        pass


async def main_loop() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN in Render / .env")
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    runner = await start_health_server(PORT)
    backoff = 2
    try:
        while True:
            app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
            app.add_handler(CommandHandler("start", start_cmd))
            app.add_handler(CallbackQueryHandler(callback_router))
            app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
            app.add_error_handler(error_handler)
            try:
                await app.initialize()
                await app.start()
                await app.updater.start_polling(drop_pending_updates=True)
                backoff = 2
                while app.updater.running:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Loop error: %s. Retry in %ss", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                try:
                    if app.updater and app.updater.running:
                        await app.updater.stop()
                    await app.stop()
                    await app.shutdown()
                except Exception:
                    pass
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main_loop())
