"""
RAG 编排服务 —— 完全参考 RAG-Pro 的 generate_answer 流程：

  question
    → 意图识别（LLM 分类 + query 改写）
    → 路由：
        chat      → 跳过检索，直接 LLM 回答
        file_list → 直接返回文件列表
        follow_up → 用改写后的 query 走 RAG
        kb_query  → 完整 RAG 流程
    → 检索（大召回 → 截断，返回 DocumentChunk 列表）
    → 取对话记忆（最近 3 轮 / 6 条 Message → 转成 dict）
    → build_rag_messages（严格 RAG-Pro 格式：system + history[-6:] + user[参考资料+置信度+问题]）
    → glm-4.5-flash 流式生成（支持深度思考：thinking_token / thinking_done）
    → 写入记忆 + SSE 输出（source / thinking / thinking_token / thinking_done / token / done / error）
"""
from __future__ import annotations
import logging
import traceback
from typing import Generator

from ..models.schemas import DocumentChunk
from ..utils.prompt_templates import build_rag_messages, SYSTEM_PROMPT_RAG
from .retriever_service import retrieve
from .generator_service import chat, chat_stream
from .memory_service import get_memory_manager
from .indexer_service import get_file_list
from .intent_service import classify_intent, INTENT_CHAT, INTENT_KB_QUERY, INTENT_FILE_LIST, INTENT_FOLLOW_UP

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
    enable_deep_think: bool = False,
    model: str | None = None,
) -> tuple[str, str, list[DocumentChunk], dict]:
    """返回 (answer, thinking_text, chunks, debug_info)。
    debug_info: {intent, original_query, rewritten_query, retrieval}
    """
    memory = get_memory_manager()

    # 0) 历史对话 → 转成 dict（意图分类也要用）
    history_raw = memory.get_messages(session_id, last_n=6)
    history = _messages_to_dicts(history_raw)

    # 1) 意图识别 + query 改写
    intent, rewritten_query = classify_intent(question, history=history)

    # 2) 路由：根据意图走不同分支
    if intent == INTENT_FILE_LIST:
        file_list = get_file_list()
        if not file_list:
            answer = "当前知识库没有任何文件喵 (｡•ᴗ-｡)♡"
        else:
            lines = [f"- {f['file_name']} ({f['chunks_count']} 块)" for f in file_list]
            answer = "知识库中当前有以下文件喵：\n" + "\n".join(lines)
        memory.append(session_id, question, answer)
        debug = {
            "intent": intent,
            "original_query": question,
            "rewritten_query": None,
            "retrieval": None,
        }
        return answer, "", [], debug

    if intent == INTENT_CHAT:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT_RAG},
        ]
        for m in history:
            messages.append({"role": m["role"], "content": m["content"]})
        messages.append({"role": "user", "content": question})
        answer, thinking_text = chat(messages, enable_deep_think=enable_deep_think, model=model)
        memory.append(session_id, question, answer)
        debug = {
            "intent": intent,
            "original_query": question,
            "rewritten_query": None,
            "retrieval": None,
        }
        return answer, thinking_text, [], debug

    # kb_query / follow_up：走完整 RAG 流程
    # 3) 检索（用改写后的 query）
    chunks, retrieval_debug = retrieve(rewritten_query, top_k=top_k)

    # 4) 组装 Prompt（注入真实文件列表）
    file_list = get_file_list()
    messages = build_rag_messages(
        query=rewritten_query,
        chunks=_chunks_to_dicts(chunks),
        conversation_history=history,
        file_list=file_list,
    )

    # 5) LLM 生成（支持深度思考）
    answer, thinking_text = chat(messages, enable_deep_think=enable_deep_think, model=model)

    # 6) 写回记忆（写原始 question，不是改写后的）
    memory.append(session_id, question, answer)
    search_queries = retrieval_debug.get("search_queries", [])
    main_query = search_queries[0] if search_queries else rewritten_query
    debug = {
        "intent": intent,
        "original_query": question,
        "rewritten_query": main_query if main_query != question else None,
        "retrieval": retrieval_debug,
    }
    return answer, thinking_text, chunks, debug


# ==================================
# 流式 RAG 问答（前端主路径）
# SSE 事件（按触发顺序）：
#   debug           → 调试信息：意图路由、query 改写、检索/rerank 排名变化（前端显示调试面板用）
#   source          → 检索到的块列表（给前端展示引用来源）
#   thinking        → 通知前端显示"思考中"loading
#   thinking_token  → 思考过程文本增量
#   thinking_done   → 思考阶段结束（准备开始正式回答）
#   token           → 正式回答增量 token
#   done            → 结束
#   error           → 异常（含 traceback）
# ==================================
def ask_rag_stream(
    question: str,
    session_id: str,
    top_k: int | None = None,
    enable_deep_think: bool = False,
    model: str | None = None,
) -> Generator[dict, None, None]:
    memory = get_memory_manager()

    try:
        # ---- 0) 历史对话（最近 6 条） → 转成 dict ----
        history_raw = memory.get_messages(session_id, last_n=6)
        history = _messages_to_dicts(history_raw)

        # ---- 1) 意图识别 + query 改写 ----
        intent, rewritten_query = classify_intent(question, history=history)
        logger.info(f"[rag_stream] intent={intent}, rewritten='{rewritten_query[:80]}'")

        # ---- 2) 路由：file_list → 直接返回文件列表 ----
        if intent == INTENT_FILE_LIST:
            file_list = get_file_list()
            if not file_list:
                answer = "当前知识库没有任何文件喵 (｡•ᴗ-｡)♡"
            else:
                lines = [f"- {f['file_name']} ({f['chunks_count']} 块)" for f in file_list]
                answer = "知识库中当前有以下文件喵：\n" + "\n".join(lines)
            # 调试信息：file_list 不做检索，retrieval = null
            yield {
                "event": "debug",
                "data": {
                    "intent": intent,
                    "original_query": question,
                    "rewritten_query": None,
                    "retrieval": None,
                },
            }
            yield {"event": "source", "data": []}
            yield {"event": "thinking", "data": None}
            yield {"event": "thinking_done", "data": None}
            # 按行推送，模拟流式效果
            for line in answer.split("\n"):
                yield {"event": "token", "data": line + "\n"}
            memory.append(session_id, question, answer)
            yield {"event": "done", "data": None}
            return

        # ---- 3) 路由：chat → 跳过检索，直接 LLM 回答 ----
        if intent == INTENT_CHAT:
            yield {
                "event": "debug",
                "data": {
                    "intent": intent,
                    "original_query": question,
                    "rewritten_query": None,
                    "retrieval": None,
                },
            }
            yield {"event": "source", "data": []}
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT_RAG},
            ]
            for m in history:
                messages.append({"role": m["role"], "content": m["content"]})
            messages.append({"role": "user", "content": question})
            yield {"event": "thinking", "data": None}
            full_answer: list[str] = []
            full_thinking: list[str] = []
            thinking_phase_finished = False
            for ttype, token in chat_stream(messages, enable_deep_think=enable_deep_think, model=model):
                if ttype == "thinking":
                    full_thinking.append(token)
                    yield {"event": "thinking_token", "data": token}
                elif ttype == "content":
                    if not thinking_phase_finished:
                        thinking_phase_finished = True
                        yield {"event": "thinking_done", "data": None}
                    full_answer.append(token)
                    yield {"event": "token", "data": token}
            if not thinking_phase_finished and full_thinking:
                yield {"event": "thinking_done", "data": None}
            answer = "".join(full_answer)
            if question and answer:
                memory.append(session_id, question, answer)
            yield {"event": "done", "data": None}
            return

        # ---- 4) kb_query / follow_up → 完整 RAG 流程 ----
        # 检索（用改写后的 query）
        chunks, retrieval_debug = retrieve(rewritten_query, top_k=top_k)
        # 用多查询分解的主查询作为改写结果展示
        search_queries = retrieval_debug.get("search_queries", [])
        main_query = search_queries[0] if search_queries else rewritten_query
        # 调试信息：意图 + 改写 + 检索/rerank 排名变化
        yield {
            "event": "debug",
            "data": {
                "intent": intent,
                "original_query": question,
                "rewritten_query": main_query if main_query != question else None,
                "retrieval": retrieval_debug,
            },
        }
        yield {
            "event": "source",
            "data": [c.model_dump() for c in chunks],
        }

        # 组装 Prompt（注入真实文件列表）
        file_list = get_file_list()
        messages = build_rag_messages(
            query=rewritten_query,
            chunks=_chunks_to_dicts(chunks),
            conversation_history=history,
            file_list=file_list,
        )

        # 流式生成（思考 + 回答分阶段推送）
        full_answer = []
        full_thinking = []

        # 先发一次 thinking 事件，触发前端 loading
        yield {"event": "thinking", "data": None}

        # 用于标记：思考段 → 回答段 的切换
        thinking_phase_finished = False

        for ttype, token in chat_stream(messages, enable_deep_think=enable_deep_think, model=model):
            if ttype == "thinking":
                full_thinking.append(token)
                yield {"event": "thinking_token", "data": token}
            elif ttype == "content":
                if not thinking_phase_finished:
                    thinking_phase_finished = True
                    yield {"event": "thinking_done", "data": None}
                full_answer.append(token)
                yield {"event": "token", "data": token}

        if not thinking_phase_finished and full_thinking:
            yield {"event": "thinking_done", "data": None}

        # 写记忆（写原始 question，不是改写后的）
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
