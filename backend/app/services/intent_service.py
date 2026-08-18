"""
意图识别 + Query 改写服务

在检索之前执行一次快速 LLM 调用（glm-4-flash），完成两件事：
1. 意图分类：chat / kb_query / file_list / follow_up
2. Query 改写：如果是追问，结合历史补全为完整问题

路由逻辑：
  - chat      → 跳过检索，直接 LLM 回答
  - kb_query  → 完整 RAG 流程
  - file_list → 直接返回文件列表，不调 LLM
  - follow_up → 用改写后的 query 走 RAG 流程
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from ..config import API_KEY, BASE_URL

logger = logging.getLogger(__name__)

# 意图标签
INTENT_CHAT = "chat"
INTENT_KB_QUERY = "kb_query"
INTENT_FILE_LIST = "file_list"
INTENT_FOLLOW_UP = "follow_up"

VALID_INTENTS = {INTENT_CHAT, INTENT_KB_QUERY, INTENT_FILE_LIST, INTENT_FOLLOW_UP}


# ==================================
# 分类 Prompt
# ==================================
_CLASSIFY_SYSTEM = """你是一个意图分类器。根据用户问题和最近对话历史，判断意图并返回 JSON。

意图类型：
- chat: 闲聊、问候、情感问题、个人问题，不需要查询知识库
- kb_query: 需要查询知识库才能回答的问题（关于文档内容、知识点、事实性信息）
- file_list: 询问知识库中有哪些文件、文件数量、文件列表
- follow_up: 追问上文（指代不明的代词、延续话题），需要结合历史改写为完整问题

返回格式（严格 JSON，不要 markdown 代码块）：
{"intent": "意图标签", "rewritten_query": "改写后的问题"}

改写规则：
- 如果是 follow_up，必须把指代词替换为具体内容，例如历史中提到"蜂医"，用户问"它有什么功效"，改写为"蜂医有什么功效"
- 如果是 chat / kb_query / file_list，rewritten_query 直接返回原问题
- rewritten_query 不能为空"""

_CLASSIFY_USER_TEMPLATE = """最近对话历史:
{history}

用户问题: {query}"""


# ==================================
# 意图识别主入口
# ==================================
def classify_intent(
    query: str,
    history: list[dict] | None = None,
    model: str | None = None,
) -> tuple[str, str]:
    """
    调用 glm-4-flash 做意图分类 + query 改写。

    返回: (intent, rewritten_query)
      - intent: chat / kb_query / file_list / follow_up
      - rewritten_query: 改写后的问题（follow_up 会补全上下文）

    如果分类失败，默认返回 (kb_query, query)，即走完整 RAG 流程。
    """
    if not query or not query.strip():
        return INTENT_KB_QUERY, query

    # 拼 history（最近 2 条，1 轮）
    history_str = "（无）"
    if history:
        recent = history[-2:]
        lines = []
        for m in recent:
            role = m.get("role", "")
            content = m.get("content", "")[:100]
            lines.append(f"{role}: {content}")
        history_str = "\n".join(lines)

    user_msg = _CLASSIFY_USER_TEMPLATE.format(history=history_str, query=query)

    messages = [
        {"role": "system", "content": _CLASSIFY_SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    try:
        result = _call_llm(messages, model=model)
        return _parse_result(result, query)
    except Exception as e:
        logger.warning(f"[intent] 分类失败，回退到 kb_query: {e}")
        return INTENT_KB_QUERY, query


# ==================================
# LLM 调用（非流式，用 glm-4-flash）
# ==================================
def _call_llm(messages: list[dict], model: str | None = None) -> str:
    """用 ChatOpenAI 做一次非流式调用"""
    from langchain_openai import ChatOpenAI

    model_id = model or "glm-4-flash"

    llm = ChatOpenAI(
        model=model_id,
        api_key=API_KEY or "placeholder",
        base_url=BASE_URL,
        temperature=0.0,
        max_tokens=256,
        streaming=False,
    )
    resp = llm.invoke(messages)
    return resp.content or ""


# ==================================
# 解析 LLM 返回的 JSON
# ==================================
def _parse_result(raw: str, fallback_query: str) -> tuple[str, str]:
    """从 LLM 返回中解析 intent 和 rewritten_query"""
    # 去掉可能的 markdown 代码块包裹
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        raw = "\n".join(lines).strip()

    try:
        data = json.loads(raw)
        intent = data.get("intent", "").strip().lower()
        rewritten = data.get("rewritten_query", "").strip()

        if intent not in VALID_INTENTS:
            logger.warning(f"[intent] 未知意图标签: {intent}，回退到 kb_query")
            return INTENT_KB_QUERY, fallback_query

        if not rewritten:
            rewritten = fallback_query

        logger.info(f"[intent] query='{fallback_query[:50]}' → intent={intent}, rewritten='{rewritten[:50]}'")
        return intent, rewritten

    except json.JSONDecodeError as e:
        logger.warning(f"[intent] JSON 解析失败: {e}, raw={raw[:200]}")
        return INTENT_KB_QUERY, fallback_query
