"""
LLM 生成服务（Generate 阶段）
默认走 OpenAI 兼容协议，不强制 provider。
如需 Ollama 本地模型，改 LLM_PROVIDER=ollama 环境变量即可。
"""
from __future__ import annotations
from typing import Iterator

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage

from ..config import API_KEY, BASE_URL, MODEL_ID


# ==================================
# Prompt 模板
# ==================================
RAG_SYSTEM_PROMPT = """你是一个严谨、诚实的知识库问答助手。

请基于下面【参考上下文】中的信息回答用户问题。必须遵守规则：
1. 回答要尽量基于【参考上下文】，不要编造上下文里没有提到的内容。
2. 如果上下文不足以回答问题，请诚实说："根据现有资料，暂时无法回答这个问题"，不要胡编。
3. 尽量引用具体出处（文件名 / 页码），但不要在回答里机械罗列"根据上下文"。
4. 使用简洁、清晰的中文表达；如果用户要求代码示例则输出代码块。

【参考上下文】
{context}
"""


def build_rag_prompt(question: str, context_str: str) -> list[BaseMessage]:
    """把问题 + 检索到的上下文组装成 Message 列表"""
    sys_text = RAG_SYSTEM_PROMPT.format(context=context_str or "（暂无参考资料，直接回答用户问题）")
    return [
        SystemMessage(content=sys_text),
        HumanMessage(content=question),
    ]


# ==================================
# LLM 工厂（模块级单例）
# ==================================
_llm_instance: BaseChatModel | None = None


def get_llm() -> BaseChatModel:
    """单例：返回 LangChain ChatModel 实例"""
    global _llm_instance
    if _llm_instance is not None:
        return _llm_instance

    import os
    provider = os.getenv("LLM_PROVIDER", "openai").lower()

    # ---- Ollama 本地模型 ----
    if provider == "ollama":
        from langchain_ollama import ChatOllama
        _llm_instance = ChatOllama(
            model=MODEL_ID,
            base_url=BASE_URL,
            temperature=0.3,
        )
        return _llm_instance

    # ---- 默认：OpenAI 兼容协议（智谱/通义/SiliconFlow/DashScope 通用）----
    from langchain_openai import ChatOpenAI
    _llm_instance = ChatOpenAI(
        model=MODEL_ID,
        api_key=API_KEY,
        base_url=BASE_URL,
        temperature=0.3,
        streaming=True,
    )
    return _llm_instance


# ==================================
# 同步 / 流式
# ==================================
def chat(messages: list[BaseMessage]) -> str:
    llm = get_llm()
    resp = llm.invoke(messages)
    return resp.content or ""


def chat_stream(messages: list[BaseMessage]) -> Iterator[str]:
    """逐 token 流式产出文本"""
    llm = get_llm()
    for chunk in llm.stream(messages):
        token = chunk.content
        if token:
            yield token
