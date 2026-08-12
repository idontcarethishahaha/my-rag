"""
RAG 编排服务 —— 完全参考 RAG-Pro 的 generate_answer 流程：

  question
    → 检索（大召回 → 截断，返回 DocumentChunk 列表）
    → 取对话记忆（最近 3 轮 / 6 条 Message → 转成 dict）
    → build_rag_messages（严格 RAG-Pro 格式：system + history[-6:] + user[参考资料+置信度+问题]）
    → glm-4.5-flash 流式生成
    → 写入记忆 + SSE 输出（source / thinking / token / done / error）
"""
from __future__ import annotations
import logging
import traceback
from typing import Generator

from ..models.schemas import DocumentChunk
from ..utils.prompt_templates import build_rag_messages
from .retriever_service import retrieve
from .generator_service import chat, chat_stream
from .memory_service import get_memory_manager

logger = logging.getLogger(__name__)


# ==================================
# 工具：DocumentChunk → dict
# ==================================
def _chunks_to_dicts(chunks: list[DocumentChunk]) -> list[dict]:
    return [c.model_dump() for c in chunks]


# ==================================
# 工具：Message(dataclass) → dict，给 build_rag_messages 用
# ==================================
def _messages_to_dicts(messages) -> list[dict]:
    out: list[dict] = []
    for m in messages or []:
        # 兼容 Message 类 / dict
        if isinstance(m, dict):
            role = m.get("role")
            content = m.get("content", "")
        else:
            role = getattr(m, "role", None)
            content = getattr(m, "content", "")
        if role in ("user", "assistant"):
            out.append({"role": role, "content": str(content or "")})
    return out


# ==================================
# 单轮非流式 RAG 问答（一般测试用）
# ==================================
def ask_rag(
    question: str,
    session_id: str,
    top_k: int | None = None,
) -> tuple[str, list[DocumentChunk]]:
    memory = get_memory_manager()

    # 1) 检索
    chunks = retrieve(question, top_k=top_k)

    # 2) 历史对话 → 转成 dict
    history_raw = memory.get_messages(session_id, last_n=6)
    history = _messages_to_dicts(history_raw)

    # 3) 组装 Prompt
    messages = build_rag_messages(
        query=question,
        chunks=_chunks_to_dicts(chunks),
        conversation_history=history,
    )

    # 4) LLM 生成
    answer = chat(messages)

    # 5) 写回记忆
    memory.append(session_id, question, answer)
    return answer, chunks


# ==================================
# 流式 RAG 问答（前端主路径）
# SSE 事件：
#   source  → 检索到的块列表（给前端展示引用来源）
#   thinking → 通知前端显示"思考中"
#   token   → 增量 token
#   done    → 结束
#   error   → 异常（含 traceback）
# ==================================
def ask_rag_stream(
    question: str,
    session_id: str,
    top_k: int | None = None,
) -> Generator[dict, None, None]:
    memory = get_memory_manager()

    try:
        # ---- 1) 检索 → 先把 sources 推给前端 ----
        chunks = retrieve(question, top_k=top_k)
        yield {
            "event": "source",
            "data": [c.model_dump() for c in chunks],
        }

        # ---- 2) 历史对话（最近 6 条） → 转成 dict ----
        history_raw = memory.get_messages(session_id, last_n=6)
        history = _messages_to_dicts(history_raw)

        # ---- 3) 组装 Prompt ----
        messages = build_rag_messages(
            query=question,
            chunks=_chunks_to_dicts(chunks),
            conversation_history=history,
        )

        # ---- 4) 流式生成 ----
        full_answer: list[str] = []
        yield {"event": "thinking", "data": None}

        for token in chat_stream(messages):
            full_answer.append(token)
            yield {"event": "token", "data": token}

        # ---- 5) 写记忆（用户问题 + 助手完整回答成对写入）----
        answer = "".join(full_answer)
        if question and answer:
            memory.append(session_id, question, answer)

        yield {"event": "done", "data": None}

    except Exception as e:
        logger.exception("RAG 流程异常")
        yield {
            "event": "error",
            "data": f"{e}\n{traceback.format_exc()}",
        }
