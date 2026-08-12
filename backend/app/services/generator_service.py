"""
LLM 生成服务（Generate 阶段）
单一模型：glm-4.5-flash（用户明确要求只有这一个可用模型，不提供多模型切换）
默认走 OpenAI 兼容协议（智谱）。
"""
from __future__ import annotations
import logging
from typing import Iterator

from ..config import API_KEY, BASE_URL, MODEL_ID

logger = logging.getLogger(__name__)

# ==================================
# LLM 单例（只可能是 glm-4.5-flash）
# ==================================
_llm_instance = None


def get_llm():
    """返回可直接 invoke / stream 的 ChatOpenAI 实例。
    只绑定一个模型：glm-4.5-flash（MODEL_ID）"""
    global _llm_instance
    if _llm_instance is not None:
        return _llm_instance

    from langchain_openai import ChatOpenAI

    if not API_KEY:
        logger.warning("[llm] OPENAI_API_KEY 为空，请检查 .env（首次懒加载时会再次尝试）")

    _llm_instance = ChatOpenAI(
        model=MODEL_ID,
        api_key=API_KEY or "placeholder",
        base_url=BASE_URL,
        temperature=0.1,        # 事实性问答：低温度
        max_tokens=4096,
        streaming=True,
    )
    logger.info(f"[llm] 已初始化：模型 {MODEL_ID} @ {BASE_URL}")
    return _llm_instance


# ==================================
# 对外：同步 / 流式（消息统一接受 list[dict] 格式）
# ==================================
def chat(messages: list[dict]) -> str:
    llm = get_llm()
    resp = llm.invoke(messages)
    return resp.content or ""


def chat_stream(messages: list[dict]) -> Iterator[str]:
    """逐 token 流式产出文本。messages 必须是 list[dict]。"""
    llm = get_llm()
    for chunk in llm.stream(messages):
        token = chunk.content
        if token:
            yield token
