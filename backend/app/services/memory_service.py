"""
对话记忆服务（Sliding Window + 自动摘要 骨架）
目前先用最简单的内存 + 最近 N 轮滑窗。
"""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
from typing import Optional


DEFAULT_MAX_MESSAGES = 100


@dataclass
class Message:
    role: str   # "user" / "assistant" / "system"
    content: str


class MemoryManager:
    """内存版会话记忆管理器：deque 滑窗 + 按 session_id 隔离"""

    def __init__(self, max_messages: int = DEFAULT_MAX_MESSAGES):
        self.max_messages = max_messages
        self._store: dict[str, deque[Message]] = {}

    def append(self, session_id: str, question: str, answer: str) -> None:
        q = self._store.setdefault(session_id, deque(maxlen=self.max_messages))
        q.append(Message(role="user", content=question))
        q.append(Message(role="assistant", content=answer))

    def get_messages(self, session_id: str, last_n: Optional[int] = None) -> list[Message]:
        q = self._store.get(session_id)
        if not q:
            return []
        msgs = list(q)
        if last_n:
            return msgs[-last_n:]
        return msgs

    def clear(self, session_id: str) -> None:
        self._store.pop(session_id, None)

    def list_sessions(self) -> list[dict]:
        result = []
        for sid, q in self._store.items():
            last = q[-1] if q else None
            first_q = next((m.content for m in q if m.role == "user"), "新对话")
            title = first_q[:20] + ("…" if len(first_q) > 20 else "")
            result.append({
                "session_id": sid,
                "title": title,
                "message_count": len(q),
                "last_message": last.content[:30] if last else None,
            })
        return result


# 模块级单例
_memory_instance: MemoryManager | None = None


def get_memory_manager() -> MemoryManager:
    global _memory_instance
    if _memory_instance is None:
        _memory_instance = MemoryManager(max_messages=100)
    return _memory_instance
