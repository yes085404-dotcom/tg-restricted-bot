import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

from config import DB_PATH, FREE_LIFETIME_LIMIT


class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT DEFAULT '',
                    full_name TEXT DEFAULT '',
                    lang TEXT DEFAULT 'hi',
                    is_banned INTEGER DEFAULT 0,
                    ban_reason TEXT DEFAULT '',
                    used_free_files INTEGER DEFAULT 0,
                    premium_until REAL DEFAULT 0,
                    premium_source TEXT DEFAULT 'none',
                    premium_plan_label TEXT DEFAULT '',
                    joined_at REAL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY,
                    added_by INTEGER DEFAULT 0,
                    added_at REAL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS force_join_chats (
                    chat_id TEXT PRIMARY KEY,
                    title TEXT DEFAULT '',
                    invite_link TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS redeem_codes (
                    code TEXT PRIMARY KEY,
                    duration_hours REAL NOT NULL,
                    label TEXT DEFAULT '',
                    max_uses INTEGER DEFAULT 1,
                    used_count INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    created_by INTEGER DEFAULT 0,
                    created_at REAL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS redemption_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    code TEXT NOT NULL,
                    duration_hours REAL NOT NULL,
                    source_type TEXT DEFAULT 'redeem',
                    redeemed_at REAL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS plan_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT DEFAULT '',
                    plan_key TEXT NOT NULL,
                    plan_label TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at REAL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id INTEGER PRIMARY KEY,
                    target_chat_id TEXT DEFAULT '',
                    thumbnail_file_id TEXT DEFAULT '',
                    rename_tag TEXT DEFAULT '',
                    custom_caption TEXT DEFAULT '',
                    replace_words_json TEXT DEFAULT '[]',
                    delete_words_json TEXT DEFAULT '[]'
                );

                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            for k, v in {
                "force_join_enabled": "1",
                "ad_enabled": "1",
                "maintenance_mode": "0",
            }.items():
                conn.execute("INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?)", (k, v))
            conn.commit()

    def ensure_user(self, user_id: int, username: str = "", full_name: str = "") -> Dict[str, Any]:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, username, full_name, joined_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = CASE WHEN excluded.username != '' THEN excluded.username ELSE users.username END,
                    full_name = CASE WHEN excluded.full_name != '' THEN excluded.full_name ELSE users.full_name END
                """,
                (user_id, username or "", full_name or "", now),
            )
            conn.execute("INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)", (user_id,))
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            return dict(row) if row else {}

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            return dict(row) if row else None

    def get_all_user_ids(self) -> List[int]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT user_id FROM users WHERE is_banned = 0").fetchall()
            return [int(r["user_id"]) for r in rows]

    def stats(self) -> Dict[str, int]:
        now = time.time()
        with self._lock, self._connect() as conn:
            return {
                "total_users": int(conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]),
                "banned_users": int(conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_banned = 1").fetchone()["c"]),
                "active_premium": int(conn.execute("SELECT COUNT(*) AS c FROM users WHERE premium_until > ?", (now,)).fetchone()["c"]),
                "total_codes": int(conn.execute("SELECT COUNT(*) AS c FROM redeem_codes").fetchone()["c"]),
            }

    def set_lang(self, user_id: int, lang: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE users SET lang = ? WHERE user_id = ?", (lang, user_id))
            conn.commit()

    def set_ban(self, user_id: int, banned: bool, reason: str = "") -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE users SET is_banned = ?, ban_reason = ? WHERE user_id = ?", (1 if banned else 0, reason, user_id))
            conn.commit()

    def grant_premium(self, user_id: int, hours: float, source: str, plan_label: str) -> float:
        self.ensure_user(user_id)
        now = time.time()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT premium_until FROM users WHERE user_id = ?", (user_id,)).fetchone()
            curr = float(row["premium_until"] or 0) if row else 0.0
            base = curr if curr > now else now
            new_until = base + (hours * 3600.0)
            conn.execute(
                "UPDATE users SET premium_until = ?, premium_source = ?, premium_plan_label = ? WHERE user_id = ?",
                (new_until, source, plan_label, user_id),
            )
            conn.execute(
                "INSERT INTO redemption_logs (user_id, code, duration_hours, source_type, redeemed_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, plan_label, hours, source, now),
            )
            conn.commit()
            return new_until

    def increment_free_usage(self, user_id: int, count: int = 1) -> int:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE users SET used_free_files = used_free_files + ? WHERE user_id = ?", (count, user_id))
            conn.commit()
            row = conn.execute("SELECT used_free_files FROM users WHERE user_id = ?", (user_id,)).fetchone()
            return int(row["used_free_files"] or 0) if row else 0

    def can_download(self, user_id: int) -> tuple[bool, str]:
        user = self.ensure_user(user_id)
        if user.get("is_banned"):
            return False, "banned"
        if float(user.get("premium_until") or 0) > time.time():
            return True, "premium"
        if int(user.get("used_free_files") or 0) < FREE_LIFETIME_LIMIT:
            return True, "free"
        return False, "limit_exceeded"

    def is_admin(self, user_id: int, owner_id: int) -> bool:
        if user_id == owner_id and owner_id != 0:
            return True
        with self._lock, self._connect() as conn:
            return conn.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)).fetchone() is not None

    def add_admin(self, user_id: int, added_by: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO admins (user_id, added_by, added_at) VALUES (?, ?, ?)", (user_id, added_by, time.time()))
            conn.commit()

    def remove_admin(self, user_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
            conn.commit()

    def list_admins(self) -> List[int]:
        with self._lock, self._connect() as conn:
            return [int(r["user_id"]) for r in conn.execute("SELECT user_id FROM admins ORDER BY added_at DESC").fetchall()]

    def add_force_chat(self, chat_id: str, title: str, invite_link: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO force_join_chats (chat_id, title, invite_link) VALUES (?, ?, ?)",
                (chat_id.strip(), title.strip(), invite_link.strip()),
            )
            conn.commit()

    def get_force_chats(self) -> List[Dict[str, str]]:
        with self._lock, self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT chat_id, title, invite_link FROM force_join_chats").fetchall()]

    def create_redeem_code(self, code: str, hours: float, label: str, max_uses: int, created_by: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO redeem_codes (code, duration_hours, label, max_uses, used_count, is_active, created_by, created_at) VALUES (?, ?, ?, ?, 0, 1, ?, ?)",
                (code.strip().upper(), hours, label, max_uses, created_by, time.time()),
            )
            conn.commit()

    def redeem_code(self, user_id: int, code: str) -> tuple[bool, str, float]:
        clean = code.strip().upper()
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM redeem_codes WHERE code = ?", (clean,)).fetchone()
            if not row:
                return False, "invalid", 0.0
            if int(row["is_active"]) != 1:
                return False, "inactive", 0.0
            if int(row["used_count"]) >= int(row["max_uses"]):
                return False, "exhausted", 0.0
            hours = float(row["duration_hours"])
            label = str(row["label"] or f"Redeem ({hours}h)")
            conn.execute("UPDATE redeem_codes SET used_count = used_count + 1 WHERE code = ?", (clean,))
            conn.commit()
        self.grant_premium(user_id, hours, source="redeem", plan_label=label)
        return True, "ok", hours

    def recent_redemptions(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM redemption_logs ORDER BY redeemed_at DESC LIMIT ?", (limit,)).fetchall()]

    def create_plan_request(self, user_id: int, username: str, plan_key: str, plan_label: str) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO plan_requests (user_id, username, plan_key, plan_label, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
                (user_id, username, plan_key, plan_label, time.time()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def get_user_settings(self, user_id: int) -> Dict[str, Any]:
        self.ensure_user(user_id)
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
            data = dict(row) if row else {}
        data["replace_words"] = json.loads(data.get("replace_words_json") or "[]")
        data["delete_words"] = json.loads(data.get("delete_words_json") or "[]")
        return data

    def update_user_setting(self, user_id: int, field: str, value: Any) -> None:
        allowed = {"target_chat_id", "thumbnail_file_id", "rename_tag", "custom_caption", "replace_words_json", "delete_words_json"}
        if field not in allowed:
            return
        self.ensure_user(user_id)
        with self._lock, self._connect() as conn:
            conn.execute(f"UPDATE user_settings SET {field} = ? WHERE user_id = ?", (value, user_id))
            conn.commit()

    def reset_user_settings(self, user_id: int) -> None:
        self.ensure_user(user_id)
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE user_settings
                SET target_chat_id = '', thumbnail_file_id = '', rename_tag = '',
                    custom_caption = '', replace_words_json = '[]', delete_words_json = '[]'
                WHERE user_id = ?
                """,
                (user_id,),
            )
            conn.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
            return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)", (key, str(value)))
            conn.commit()
