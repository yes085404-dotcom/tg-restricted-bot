import secrets
import string
import time
from typing import List, Tuple

from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import Database

PLANS: List[Tuple[str, str, float]] = [
    ("1h", "1 घंटा / 1 Hour", 1.0),
    ("6h", "6 घंटे / 6 Hours", 6.0),
    ("1d", "1 दिन / 1 Day", 24.0),
    ("3d", "3 दिन / 3 Days", 72.0),
    ("5d", "5 दिन / 5 Days", 120.0),
    ("1m", "1 महीना / 1 Month", 720.0),
    ("6m", "6 महीने / 6 Months", 4320.0),
    ("12m", "12 महीने / 12 Months", 8640.0),
]


def generate_code(length: int = 8) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "TG-" + "".join(secrets.choice(alphabet) for _ in range(length))


def format_remaining(premium_until: float) -> str:
    rem = int(premium_until - time.time())
    if rem <= 0:
        return "0h 0m 0s (Expired)"
    days, rem = divmod(rem, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    return f"{hours}h {minutes}m {seconds}s"


async def start_health_server(port: int) -> web.AppRunner:
    async def health_handler(_request: web.Request) -> web.Response:
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health_handler)
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    return runner


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ Add Admin", callback_data="adm_add_admin"),
                InlineKeyboardButton("➖ Remove Admin", callback_data="adm_rem_admin"),
            ],
            [
                InlineKeyboardButton("🎟️ Gen Redeem Code", callback_data="adm_gen_code"),
                InlineKeyboardButton("👑 Grant Premium", callback_data="adm_grant_prem"),
            ],
            [
                InlineKeyboardButton("🚫 Ban User", callback_data="adm_ban"),
                InlineKeyboardButton("✅ Unban User", callback_data="adm_unban"),
            ],
            [
                InlineKeyboardButton("📢 Force Join Setup", callback_data="adm_fj"),
                InlineKeyboardButton("📣 Broadcast", callback_data="adm_broadcast"),
            ],
            [
                InlineKeyboardButton("📊 Stats & Logs", callback_data="adm_stats"),
                InlineKeyboardButton("🛑 Stop All Tasks", callback_data="stop_tasks"),
            ],
            [
                InlineKeyboardButton("🛠️ Maintenance Toggle", callback_data="adm_maint"),
                InlineKeyboardButton("⬅️ Back to User Menu", callback_data="back_main"),
            ],
        ]
    )


async def open_admin_panel(update: Update, _context: ContextTypes.DEFAULT_TYPE, db: Database, owner_id: int) -> None:
    user = update.effective_user
    if not user or not db.is_admin(user.id, owner_id):
        return
    st = db.stats()
    admins = db.list_admins()
    text = (
        "🛡️ <b>ADMIN / OWNER CONTROL PANEL</b>\n\n"
        f"👤 Users: <b>{st['total_users']}</b> | 👑 Active Premium: <b>{st['active_premium']}</b>\n"
        f"🚫 Banned: <b>{st['banned_users']}</b> | 🎟️ Codes: <b>{st['total_codes']}</b>\n"
        f"🧑‍💼 Extra Admins: <b>{len(admins)}</b>"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=admin_menu_keyboard())
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=admin_menu_keyboard())


async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database, owner_id: int) -> bool:
    query = update.callback_query
    user = update.effective_user
    if not query or not user or not db.is_admin(user.id, owner_id):
        return False
    data = query.data or ""
    if not data.startswith("adm_"):
        return False
    await query.answer()

    if data in ("adm_add_admin", "adm_rem_admin") and user.id != owner_id:
        await query.message.reply_text("❌ यह अधिकार केवल Main Owner के पास है।")
        return True

    prompts = {
        "adm_add_admin": "नए Admin की Numeric User ID भेजें:",
        "adm_rem_admin": "हटाने वाले Admin की Numeric User ID भेजें:",
        "adm_gen_code": "`<घंटे> <अधिकतम_इस्तेमाल>` भेजें (उदाहरण: `1 1` या `24 5`):",
        "adm_grant_prem": "`<User_ID> <घंटे>` भेजें (उदाहरण: `123456789 24`):",
        "adm_ban": "`<User_ID> [कारण]` भेजें:",
        "adm_unban": "`<User_ID>` भेजें:",
        "adm_fj": "`<chat_id_या_@username> | <Title> | <Invite_Link>` भेजें:",
        "adm_broadcast": "सभी Users को भेजने के लिए मैसेज लिखें:",
    }
    if data in prompts:
        context.user_data["awaiting"] = data
        await query.message.reply_text(prompts[data], parse_mode="Markdown")
        return True

    if data == "adm_stats":
        st = db.stats()
        logs = db.recent_redemptions(8)
        lines = "\n".join([f"• `{r['user_id']}` | `{r['source_type']}` | `{r['duration_hours']}h` | `{r['code']}`" for r in logs]) or "कोई रिकॉर्ड नहीं।"
        await query.message.reply_text(
            f"📊 <b>Stats</b>\nUsers: <b>{st['total_users']}</b> | Premium: <b>{st['active_premium']}</b> | Banned: <b>{st['banned_users']}</b>\n\n{lines}",
            parse_mode="HTML",
        )
        return True

    if data == "adm_maint":
        nxt = "0" if db.get_setting("maintenance_mode", "0") == "1" else "1"
        db.set_setting("maintenance_mode", nxt)
        await query.message.reply_text(f"🛠️ Maintenance Mode: <b>{'ON' if nxt == '1' else 'OFF'}</b>", parse_mode="HTML")
        return True

    return False


async def handle_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE, db: Database, owner_id: int) -> bool:
    user = update.effective_user
    msg = update.message
    if not user or not msg or not msg.text or not db.is_admin(user.id, owner_id):
        return False
    mode = context.user_data.get("awaiting")
    if not mode or not str(mode).startswith("adm_"):
        return False

    text = msg.text.strip()
    context.user_data["awaiting"] = None

    try:
        if mode == "adm_add_admin":
            db.add_admin(int(text), user.id)
            await msg.reply_text(f"✅ Admin जोड़ा गया: `{int(text)}`", parse_mode="Markdown")
            return True
        if mode == "adm_rem_admin":
            db.remove_admin(int(text))
            await msg.reply_text(f"🗑️ Admin हटाया गया: `{int(text)}`", parse_mode="Markdown")
            return True
        if mode == "adm_gen_code":
            parts = text.split()
            hours = float(parts[0])
            max_uses = int(parts[1]) if len(parts) > 1 else 1
            code = generate_code(8)
            db.create_redeem_code(code, hours, f"Code ({hours}h)", max_uses, user.id)
            await msg.reply_text(f"🎟️ Code: <code>{code}</code>\nअवधि: <b>{hours}h</b> | Max Uses: <b>{max_uses}</b>", parse_mode="HTML")
            return True
        if mode == "adm_grant_prem":
            uid, hrs = int(text.split()[0]), float(text.split()[1])
            db.grant_premium(uid, hrs, "admin", f"Admin ({hrs}h)")
            await msg.reply_text(f"✅ User `{uid}` को <b>{hrs}h</b> Premium दिया गया।", parse_mode="HTML")
            return True
        if mode == "adm_ban":
            parts = text.split(maxsplit=1)
            db.set_ban(int(parts[0]), True, parts[1] if len(parts) > 1 else "Banned")
            await msg.reply_text(f"🚫 User `{int(parts[0])}` Ban किया गया।", parse_mode="HTML")
            return True
        if mode == "adm_unban":
            db.set_ban(int(text), False, "")
            await msg.reply_text(f"✅ User `{int(text)}` Unban किया गया।", parse_mode="HTML")
            return True
        if mode == "adm_fj":
            parts = [p.strip() for p in text.split("|")]
            db.add_force_chat(parts[0], parts[1] if len(parts) > 1 else parts[0], parts[2] if len(parts) > 2 else "")
            await msg.reply_text("✅ Force Join Chat जोड़ा गया।")
            return True
        if mode == "adm_broadcast":
            sent = 0
            for uid in db.get_all_user_ids():
                try:
                    await context.bot.send_message(uid, f"📢 <b>सूचना:</b>\n\n{text}", parse_mode="HTML")
                    sent += 1
                except Exception:
                    pass
            await msg.reply_text(f"✅ Broadcast भेजा गया: <b>{sent}</b> Users को।", parse_mode="HTML")
            return True
    except Exception as exc:
        await msg.reply_text(f"⚠️ त्रुटि: `{exc}`", parse_mode="Markdown")
        return True
    return False
