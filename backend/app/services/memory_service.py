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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


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
    # content: 纯文字 str，或 OpenAI 多模态 list[dict]（形如 [{"type":"image_url","image_url":...}, {"type":"text","text":...}]）
    content: Any
    metadata: dict | None = None
    # 用户消息的图片 URL 列表（相对静态路径，如 /uploads/xxx.jpg）
    #  等价于从 content 中提取 image_url，但为了老数据兼容，保留独立字段。
    image_urls: list[str] = field(default_factory=list)


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

    # -------- 内部辅助：确保会话存在 + 取下一个 sort_index --------
    def _ensure_session(self, conn, session_id: str, title_source: str) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            "SELECT session_id FROM conversations WHERE session_id = ?",
            (session_id,),
        )
        if not cur.fetchone():
            title = self._truncate_title(title_source) or "新对话"
            conn.execute(
                "INSERT INTO conversations(session_id, title, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (session_id, title, now, now),
            )
        cur = conn.execute(
            "SELECT COALESCE(MAX(sort_index), -1) FROM messages WHERE session_id = ?",
            (session_id,),
        )
        return cur.fetchone()[0] + 1

    def _touch_session(self, conn, session_id: str, title_source: str) -> None:
        """更新会话 updated_at；若标题还是默认「新对话」则改为 title_source 摘要"""
        now = datetime.now().isoformat(timespec="seconds")
        cur = conn.execute(
            "SELECT title FROM conversations WHERE session_id = ?",
            (session_id,),
        )
        row = cur.fetchone()
        new_title = self._truncate_title(title_source) or "新对话"
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

    # -------- 单独追加用户消息（流式开始时调用）--------
    def append_user_message(self, session_id: str, question: str,
                            user_metadata: dict | None = None) -> None:
        """只写 user 消息（供流式接口在生成前调用）。
        user_metadata.image_urls 会同时写入 content 的多模态 JSON。
        """
        with _lock, _get_conn() as conn:
            now = datetime.now().isoformat(timespec="seconds")
            next_idx = self._ensure_session(conn, session_id, question or "[图片]")

            image_urls = None
            if user_metadata and isinstance(user_metadata, dict) and user_metadata.get("image_urls"):
                image_urls = list(user_metadata["image_urls"])
            if image_urls:
                parts: list[dict[str, Any]] = [
                    {"type": "image_url", "image_url": {"url": u}} for u in image_urls
                ]
                if question and question != "[图片]":
                    parts.append({"type": "text", "text": question})
                content_to_store = json.dumps(parts, ensure_ascii=False)
            else:
                content_to_store = question

            user_metadata_json = json.dumps(user_metadata, ensure_ascii=False) if user_metadata else None
            conn.execute(
                "INSERT INTO messages(session_id, role, content, metadata, created_at, sort_index) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, "user", content_to_store, user_metadata_json, now, next_idx),
            )
            self._touch_session(conn, session_id, question or "[图片]")
            conn.commit()
            self._trim_if_needed(conn, session_id)

    # -------- 单独追加助手消息（流式结束后调用）--------
    def append_assistant_message(self, session_id: str, answer: str,
                                 metadata: dict | None = None) -> None:
        """只写 assistant 消息（供流式接口在生成完成后调用）"""
        with _lock, _get_conn() as conn:
            now = datetime.now().isoformat(timespec="seconds")
            next_idx = self._ensure_session(conn, session_id, answer or "回答")
            assistant_metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
            conn.execute(
                "INSERT INTO messages(session_id, role, content, metadata, created_at, sort_index) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, "assistant", answer, assistant_metadata_json, now, next_idx),
            )
            self._touch_session(conn, session_id, answer or "回答")
            conn.commit()
            self._trim_if_needed(conn, session_id)

    # -------- 追加消息（用户 + 助手成对）--------
    def append(self, session_id: str, question: str, answer: str, metadata: dict | None = None,
               user_metadata: dict | None = None) -> None:
        """
        追加一对消息（user + assistant）。适用于非流式一次性写入。
        流式接口请改用 append_user_message + append_assistant_message。
        - metadata: 存到 assistant 消息上（如 agent_output 图表数据）
        - user_metadata: 存到 user 消息上（如 image_urls 图片列表）
        """
        self.append_user_message(session_id, question, user_metadata=user_metadata)
        self.append_assistant_message(session_id, answer, metadata=metadata)

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

        result: list[Message] = []
        for r in rows:
            role = r["role"]
            raw_content = r["content"] or ""
            raw_metadata = json.loads(r["metadata"]) if r["metadata"] else None

            # —— 多模态 content 解析 ——
            content_out: Any = raw_content
            image_urls_out: list[str] = []

            if role == "user":
                parsed_list = None
                if isinstance(raw_content, str):
                    # 尝试解析为多模态 JSON list
                    stripped = raw_content.strip()
                    if stripped.startswith("[") and stripped.endswith("]"):
                        try:
                            parsed_list = json.loads(raw_content)
                        except Exception:
                            parsed_list = None
                if isinstance(parsed_list, list):
                    # 多模态格式：[{"type":"image_url",...}, {"type":"text",...}]
                    texts: list[str] = []
                    for part in parsed_list:
                        if not isinstance(part, dict):
                            continue
                        ptype = part.get("type")
                        if ptype == "image_url":
                            iu = part.get("image_url") or {}
                            url = iu.get("url") if isinstance(iu, dict) else None
                            if url:
                                image_urls_out.append(url)
                        elif ptype == "text":
                            txt = part.get("text")
                            if isinstance(txt, str):
                                texts.append(txt)
                    content_out = "\n".join(t for t in texts if t) if texts else "[图片]"
                else:
                    # 老版本：用 user_metadata.image_urls 兼容
                    if isinstance(raw_metadata, dict) and raw_metadata.get("image_urls"):
                        image_urls_out = list(raw_metadata["image_urls"])

            result.append(Message(
                role=role,
                content=content_out,
                metadata=raw_metadata,
                image_urls=image_urls_out,
            ))
        return result

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
