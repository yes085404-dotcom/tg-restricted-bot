import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from config import DB_PATH, FREE_LIFETIME_LIMIT


class Database:
    def __init__(self, path: str = DB_PATH) -> None:
        self.path = path
        db_dir = os.path.dirname(self.path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
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
                    premium_plan_key TEXT DEFAULT '',
                    premium_plan_label TEXT DEFAULT '',
                    created_at REAL DEFAULT 0,
                    updated_at REAL DEFAULT 0
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
                    value TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS force_chats (
                    chat_id TEXT PRIMARY KEY,
                    title TEXT DEFAULT '',
                    invite_link TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS redeem_codes (
                    code TEXT PRIMARY KEY,
                    hours REAL NOT NULL,
                    max_uses INTEGER DEFAULT 1,
                    used_count INTEGER DEFAULT 0
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
                """
            )
            defaults = {
                "force_join_enabled": "0",
                "maintenance_mode": "0",
                "ad_enabled": "1",
            }
            for k, v in defaults.items():
                conn.execute(
                    "INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?)",
                    (k, v),
                )
            conn.commit()

    def ensure_user(self, user_id: int, username: str = "", full_name: str = "") -> Dict[str, Any]:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (user_id, username, full_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = CASE WHEN excluded.username != '' THEN excluded.username ELSE users.username END,
                    full_name = CASE WHEN excluded.full_name != '' THEN excluded.full_name ELSE users.full_name END,
                    updated_at = excluded.updated_at
                """,
                (user_id, username, full_name, now, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO user_settings (user_id) VALUES (?)",
                (user_id,),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else {"user_id": user_id, "lang": "hi"}

    def get_user(self, user_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def is_admin(self, user_id: int, owner_id: int = 0) -> bool:
        if owner_id and int(user_id) == int(owner_id):
            return True
        val = self.get_setting("admin_ids", "")
        if not val:
            return False
        ids = [x.strip() for x in val.split(",") if x.strip()]
        return str(user_id) in ids

    def set_lang(self, user_id: int, lang: str) -> None:
        self.ensure_user(user_id)
        with self._connect() as conn:
            conn.execute("UPDATE users SET lang = ?, updated_at = ? WHERE user_id = ?", (lang, time.time(), user_id))
            conn.commit()

    def can_download(self, user_id: int) -> Tuple[bool, str]:
        if self.is_admin(user_id, 0):
            return True, "admin"
        u = self.ensure_user(user_id)
        if float(u.get("premium_until") or 0) > time.time():
            return True, "premium"
        used = int(u.get("used_free_files") or 0)
        if used < FREE_LIFETIME_LIMIT:
            return True, "free"
        return False, "limit_reached"

    def increment_free_usage(self, user_id: int, count: int = 1) -> None:
        if count <= 0:
            return
        self.ensure_user(user_id)
        with self._connect() as conn:
            conn.execute(
                "UPDATE users SET used_free_files = COALESCE(used_free_files, 0) + ?, updated_at = ? WHERE user_id = ?",
                (int(count), time.time(), user_id),
            )
            conn.commit()

    def grant_premium(self, user_id: int, hours: float, plan_key: str = "custom", plan_label: str = "Premium") -> float:
        self.ensure_user(user_id)
        now = time.time()
        u = self.get_user(user_id) or {}
        current = float(u.get("premium_until") or 0)
        base = current if current > now else now
        new_until = base + float(hours) * 3600.0
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE users
                SET premium_until = ?, premium_plan_key = ?, premium_plan_label = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (new_until, plan_key, plan_label, now, user_id),
            )
            conn.commit()
        return new_until

    def redeem_code(self, user_id: int, code: str) -> Tuple[bool, str, float]:
        clean = (code or "").strip().upper()
        if not clean:
            return False, "EMPTY_CODE", 0.0
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM redeem_codes WHERE code = ?", (clean,)).fetchone()
            if not row:
                return False, "INVALID_CODE", 0.0
            max_uses = int(row["max_uses"] or 1)
            used_count = int(row["used_count"] or 0)
            hours = float(row["hours"] or 0)
            if used_count >= max_uses or hours <= 0:
                return False, "EXPIRED_OR_USED", 0.0
            conn.execute("UPDATE redeem_codes SET used_count = used_count + 1 WHERE code = ?", (clean,))
            conn.commit()
        self.grant_premium(user_id, hours, "redeem", f"Redeem ({clean})")
        return True, "OK", hours

    def create_plan_request(self, user_id: int, username: str, plan_key: str, plan_label: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO plan_requests (user_id, username, plan_key, plan_label, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (user_id, username, plan_key, plan_label, time.time()),
            )
            conn.commit()
            return int(cur.lastrowid or 0)

    def get_setting(self, key: str, default: str = "") -> str:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM bot_settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row and row["value"] is not None else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO bot_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, str(value)),
            )
            conn.commit()

    def get_force_chats(self) -> List[Dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT chat_id, title, invite_link FROM force_chats").fetchall()
        return [dict(r) for r in rows]

    def get_user_settings(self, user_id: int) -> Dict[str, Any]:
        self.ensure_user(user_id)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return {
                "target_chat_id": "",
                "thumbnail_file_id": "",
                "rename_tag": "",
                "custom_caption": "",
                "replace_words": [],
                "delete_words": [],
            }
        try:
            replace_words = json.loads(row["replace_words_json"] or "[]")
        except Exception:
            replace_words = []
        try:
            delete_words = json.loads(row["delete_words_json"] or "[]")
        except Exception:
            delete_words = []
        return {
            "target_chat_id": (row["target_chat_id"] or "").strip(),
            "thumbnail_file_id": (row["thumbnail_file_id"] or "").strip(),
            "rename_tag": (row["rename_tag"] or "").strip(),
            "custom_caption": (row["custom_caption"] or "").strip(),
            "replace_words": replace_words if isinstance(replace_words, list) else [],
            "delete_words": delete_words if isinstance(replace_words, list) else [],
        }

    def update_user_setting(self, user_id: int, column: str, value: str) -> None:
        allowed = {
            "target_chat_id",
            "thumbnail_file_id",
            "rename_tag",
            "custom_caption",
            "replace_words_json",
            "delete_words_json",
        }
        if column not in allowed:
            return
        self.ensure_user(user_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE user_settings SET {column} = ? WHERE user_id = ?",
                (value, user_id),
            )
            conn.commit()

    def reset_user_settings(self, user_id: int) -> None:
        self.ensure_user(user_id)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE user_settings
                SET target_chat_id = '',
                    thumbnail_file_id = '',
                    rename_tag = '',
                    custom_caption = '',
                    replace_words_json = '[]',
                    delete_words_json = '[]'
                WHERE user_id = ?
                """,
                (user_id,),
            )
            conn.commit()
