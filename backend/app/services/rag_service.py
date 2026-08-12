"""
RAG 编排服务（Online / Inference Pipeline 的总入口）
串起来：
  question
    → 取对话记忆（滑窗 + 摘要）
    → 检索 Top-K 文档块
    → 组装增强 Prompt（上下文 + 问题 + 记忆）
    → LLM 生成（同步 / 流式）
    → 追加记忆 + 返回答案 + 来源

和 services/ 里其他文件的分工：
  indexer_service.py     离线入库
  retriever_service.py   检索/重排
  generator_service.py   LLM 调用 & Prompt 模板
  memory_service.py      对话记忆
  rag_service.py   ←本文件  总编排
"""
from __future__ import annotations
import uuid
from typing import AsyncGenerator, Generator

from ..config import RAG_TOP_K, RAG_SIMILARITY_THRESHOLD
from ..models.schemas import DocumentChunk
from .retriever_service import retrieve
from .generator_service import build_rag_prompt, chat, chat_stream
from .memory_service import get_memory_manager, MemoryManager


# ==================================
# 单轮非流式 RAG 问答
# ==================================
def ask_rag(
    question: str,
    session_id: str,
    top_k: int | None = None,
) -> tuple[str, list[DocumentChunk]]:
    """
    返回 (answer_text, source_chunks)
    """
    # 1. 取历史
    memory = get_memory_manager()

    # 2. 检索
    chunks = retrieve(question, top_k=top_k)

    # 3. 构造上下文字符串（文件名 + 页码 + 内容）
    context_str = _format_context(chunks)

    # 4. 组装 Prompt（系统提示 + 上下文 + 用户问题 + 历史简版）
    messages = build_rag_prompt(question, context_str)
    # 追加历史（TODO：接入 memory 取最近 N 轮）

    # 5. LLM 生成
    answer = chat(messages)

    # 6. 写回记忆
    memory.append(session_id, question, answer)

    return answer, chunks


# ==================================
# 流式 RAG 问答
# ==================================
def ask_rag_stream(
    question: str,
    session_id: str,
    top_k: int | None = None,
) -> Generator[dict, None, None]:
    """
    流式产出 SSE 事件：
      {"event": "source",  "data": [chunk, ...]}   先推引用来源
      {"event": "token",   "data": "一个字"}       逐 token 答案
      {"event": "done",    "data": None}          结束
      {"event": "error",   "data": "err msg"}     错误
    """
    memory = get_memory_manager()

    try:
        # 1. 检索（同步获取来源，先推给前端展示）
        chunks = retrieve(question, top_k=top_k)
        yield {
            "event": "source",
            "data": [c.model_dump() for c in chunks],
        }

        # 2. 构造上下文 + Prompt
        context_str = _format_context(chunks)
        messages = build_rag_prompt(question, context_str)

        # 3. 流式 LLM
        full_answer: list[str] = []
        yield {"event": "thinking", "data": None}
        for token in chat_stream(messages):
            full_answer.append(token)
            yield {"event": "token", "data": token}

        # 4. 写记忆
        memory.append(session_id, question, "".join(full_answer))

        yield {"event": "done", "data": None}

    except Exception as e:
        yield {"event": "error", "data": str(e)}


# ==================================
# 工具函数
# ==================================
def _format_context(chunks: list[DocumentChunk]) -> str:
    """
    把检索到的块拼成 Prompt 里可读的上下文字符串，
    带编号 + 来源引用，LLM 可据此溯源。
    """
    if not chunks:
        return ""

    parts: list[str] = []
    for i, c in enumerate(chunks, 1):
        src = c.source_file
        if c.page is not None:
            src += f"（第{c.page}页）"
        parts.append(f"[{i}] 来源：{src}\n内容：{c.content}\n")
    return "\n".join(parts)
