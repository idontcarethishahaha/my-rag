"""
对话记忆服务 —— SQLite 持久化版
相比内存版：重启不丢会话；支持会话列表、消息历史、自动生成标题。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


DEFAULT_MAX_MESSAGES = 100

# SQLite 数据库文件路径（相对 backend 目录）
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "chat_memory.db")
DB_PATH = os.path.abspath(DB_PATH)
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# 线程锁：SQLite 多线程写要加锁
_lock = threading.Lock()


@dataclass
class Message:
    role: str   # "user" / "assistant" / "system"
    content: str
    metadata: dict | None = None


# ==================================
# SQLite 初始化 + 连接
# ==================================

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    session_id TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    metadata   TEXT,
    created_at TEXT NOT NULL,
    sort_index INTEGER NOT NULL,
    FOREIGN KEY (session_id) REFERENCES conversations(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, sort_index);
"""


def _init_db() -> None:
    with _get_conn() as conn:
        conn.executescript(_SCHEMA_SQL)
        conn.commit()

        # 迁移：确保 metadata 列存在
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN metadata TEXT")
            conn.commit()
        except Exception:
            pass  # 列已存在时 SQLite 会报错，忽略


@contextmanager
def _get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


# 模块加载时自动建表
_init_db()


# ==================================
# 核心：MemoryManager（兼容旧接口，内部改 SQLite）
# ==================================

class MemoryManager:
    """SQLite 持久化的会话记忆管理器"""

    def __init__(self, max_messages: int = DEFAULT_MAX_MESSAGES):
        self.max_messages = max_messages

    # -------- 内部辅助 --------
    @staticmethod
    def _truncate_title(text: str, limit: int = 20) -> str:
        t = text.strip().replace("\n", " ")
        return t[:limit] + ("…" if len(t) > limit else "")

    # -------- 会话创建 --------
    def create_session(self, session_id: Optional[str] = None,
                       title: str = "新对话") -> dict:
        """创建一个新会话并写入 DB，返回 {session_id, title, created_at}"""
        sid = session_id or uuid.uuid4().hex
        now = datetime.now().isoformat(timespec="seconds")
        with _lock, _get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO conversations(session_id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (sid, title, now, now),
            )
            conn.commit()
        return {"session_id": sid, "title": title, "created_at": now}

    # -------- 追加消息（用户 + 助手成对）--------
    def append(self, session_id: str, question: str, answer: str, metadata: dict | None = None) -> None:
        with _lock, _get_conn() as conn:
            now = datetime.now().isoformat(timespec="seconds")

            # 确保会话存在（不存在就新建，标题用 question 前几个字）
            cur = conn.execute(
                "SELECT session_id FROM conversations WHERE session_id = ?",
                (session_id,),
            )
            if not cur.fetchone():
                title = self._truncate_title(question) or "新对话"
                conn.execute(
                    "INSERT INTO conversations(session_id, title, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (session_id, title, now, now),
                )

            # 当前 sort_index 最大值
            cur = conn.execute(
                "SELECT COALESCE(MAX(sort_index), -1) FROM messages WHERE session_id = ?",
                (session_id,),
            )
            next_idx = cur.fetchone()[0] + 1

            conn.execute(
                "INSERT INTO messages(session_id, role, content, created_at, sort_index) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, "user", question, now, next_idx),
            )
            assistant_metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
            conn.execute(
                "INSERT INTO messages(session_id, role, content, metadata, created_at, sort_index) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, "assistant", answer, assistant_metadata_json, now, next_idx + 1),
            )

            # 更新会话时间 + 标题（如果是默认「新对话」就改成 question）
            cur = conn.execute(
                "SELECT title FROM conversations WHERE session_id = ?",
                (session_id,),
            )
            row = cur.fetchone()
            new_title = self._truncate_title(question) or "新对话"
            if row and (row["title"] == "新对话" or not row["title"]):
                conn.execute(
                    "UPDATE conversations SET title = ?, updated_at = ? WHERE session_id = ?",
                    (new_title, now, session_id),
                )
            else:
                conn.execute(
                    "UPDATE conversations SET updated_at = ? WHERE session_id = ?",
                    (now, session_id),
                )
            conn.commit()

            # 超过 max_messages 就裁剪
            self._trim_if_needed(conn, session_id)

    def _trim_if_needed(self, conn, session_id: str) -> None:
        cur = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?",
            (session_id,),
        )
        cnt = cur.fetchone()[0]
        if cnt > self.max_messages:
            to_delete = cnt - self.max_messages
            conn.execute(
                "DELETE FROM messages WHERE session_id = ? ORDER BY sort_index ASC LIMIT ?",
                (session_id, to_delete),
            )
            conn.commit()

    # -------- 读取消息 --------
    def get_messages(self, session_id: str, last_n: Optional[int] = None) -> list[Message]:
        with _get_conn() as conn:
            sql = (
                "SELECT role, content, metadata FROM messages "
                "WHERE session_id = ? ORDER BY sort_index ASC"
            )
            if last_n:
                sql += f" LIMIT {last_n} OFFSET " \
                       f"(SELECT COUNT(*) FROM messages WHERE session_id = ?) - {last_n}"
                rows = conn.execute(sql, (session_id, session_id)).fetchall()
            else:
                rows = conn.execute(sql, (session_id,)).fetchall()
            return [
                Message(
                    role=r["role"],
                    content=r["content"],
                    metadata=json.loads(r["metadata"]) if r["metadata"] else None,
                )
                for r in rows
            ]

    # -------- 清空单个会话 --------
    def clear(self, session_id: str) -> None:
        with _lock, _get_conn() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM conversations WHERE session_id = ?", (session_id,))
            conn.commit()

    # -------- 列出所有会话（按更新时间倒序）--------
    def list_sessions(self) -> list[dict]:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT c.session_id, c.title, c.created_at, c.updated_at, "
                "       (SELECT COUNT(*) FROM messages m WHERE m.session_id = c.session_id) AS msg_count, "
                "       (SELECT content FROM messages m WHERE m.session_id = c.session_id "
                "        ORDER BY sort_index DESC LIMIT 1) AS last_content "
                "FROM conversations c ORDER BY c.updated_at DESC"
            ).fetchall()
        result = []
        for r in rows:
            last_content = r["last_content"] or ""
            last_trim = last_content[:30] + ("…" if len(last_content) > 30 else "") if last_content else None
            result.append({
                "session_id": r["session_id"],
                "title": r["title"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "message_count": r["msg_count"],
                "last_message": last_trim,
            })
        return result


# ==================================
# 模块级单例（保持旧接口不变）
# ==================================

_memory_instance: MemoryManager | None = None


def get_memory_manager() -> MemoryManager:
    global _memory_instance
    if _memory_instance is None:
        _memory_instance = MemoryManager(max_messages=DEFAULT_MAX_MESSAGES)
    return _memory_instance
