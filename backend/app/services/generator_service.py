"""
LLM 生成服务（完全参考 tomatocat-agent 的 LLMProvider 实现）

关键改动：
1. 支持多模型：glm-4-flash、glm-4.5-flash 等，默认 glm-4.5-flash
2. 普通模式 → LangChain ChatOpenAI
3. 深度思考模式 → 直接用 AsyncOpenAI + extra_body={"enable_thinking": True}
   （参考 tomatocat-agent LLMProvider，智谱通过 reasoning_content 返回思考）
4. 智谱是国内 API → 绕过 HTTP_PROXY 直连（tomatocat 相同策略）
5. 流式解析 delta.reasoning_content / delta.content
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Iterator, Tuple
from urllib.parse import urlparse

from ..config import API_KEY, BASE_URL, MODEL_ID

logger = logging.getLogger(__name__)

# 智谱等国内域名，绕过代理直连（与 tomatocat 保持一致）
_DOMESTIC_HOSTS = {"open.bigmodel.cn", "api.minimax.chat", "dashscope.aliyuncs.com"}

# 可用模型列表
AVAILABLE_MODELS = [
    {
        "id": "glm-4.5-flash",
        "name": "GLM-4.5-Flash",
        "provider": "智谱",
        "default": True,
        "supports_deep_think": True,
    },
    {
        "id": "glm-4-flash",
        "name": "GLM-4-Flash",
        "provider": "智谱",
        "default": False,
        "supports_deep_think": False,  # glm-4-flash 不支持深度思考
    },
]

_llm_instances = {}  # {model_id: ChatOpenAI 实例}


def _bypass_proxy(base_url: str) -> bool:
    try:
        host = urlparse(base_url or "").hostname or ""
        return any(h in host for h in _DOMESTIC_HOSTS)
    except Exception:
        return False


def _resolve_model(model: str | None) -> str:
    """解析模型 ID：传入则用传入的，否则用默认配置 MODEL_ID"""
    if model:
        return model
    return MODEL_ID


def get_llm(model: str | None = None):
    """普通模式：LangChain ChatOpenAI（不启用思考）"""
    model_id = _resolve_model(model)
    if model_id in _llm_instances:
        return _llm_instances[model_id]

    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model=model_id,
        api_key=API_KEY or "placeholder",
        base_url=BASE_URL,
        temperature=0.1,
        max_tokens=4096,
        streaming=True,
    )
    _llm_instances[model_id] = llm
    logger.info(f"[llm] 普通模式初始化：{model_id} @ {BASE_URL}")
    return llm


def model_supports_deep_think(model: str | None) -> bool:
    """判断某个模型是否支持深度思考"""
    model_id = _resolve_model(model)
    for m in AVAILABLE_MODELS:
        if m["id"] == model_id:
            return m.get("supports_deep_think", False)
    return False


# ==================================
# 同步入口
# ==================================
def chat(messages: list[dict], enable_deep_think: bool = False, model: str | None = None) -> Tuple[str, str]:
    """同步调用。返回 (answer, thinking_text)。"""
    return asyncio.run(_chat_async(messages, enable_deep_think=enable_deep_think, model=model))


async def _chat_async(messages: list[dict], enable_deep_think: bool = False, model: str | None = None) -> Tuple[str, str]:
    model_id = _resolve_model(model)
    # 只有模型支持深度思考时才走深度思考分支
    use_deep_think = enable_deep_think and model_supports_deep_think(model_id)
    if use_deep_think:
        return await _deep_think_non_stream(messages, model=model_id)

    llm = get_llm(model_id)
    resp = llm.invoke(messages)
    return resp.content or "", ""


# ==================================
# 流式入口（前端主路径）
# ==================================
def chat_stream(
    messages: list[dict],
    enable_deep_think: bool = False,
    model: str | None = None,
) -> Iterator[Tuple[str, str]]:
    """
    同步生成器（封装异步的流式调用）。

    Yields: (type, token)
      - type='thinking'  思考过程增量
      - type='content'   回答增量
    """
    model_id = _resolve_model(model)
    use_deep_think = enable_deep_think and model_supports_deep_think(model_id)
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        async_gen = (
            _deep_think_stream(messages, model=model_id)
            if use_deep_think
            else _normal_stream(messages, model=model_id)
        )
        gen = _syncify(async_gen, loop)
        for item in gen:
            yield item
    finally:
        loop.close()


def _syncify(async_gen, loop):
    """把异步生成器迭代事件循环，封装成同步生成器。"""
    async def _drain(q, async_gen):
        try:
            async for item in async_gen:
                await q.put(("item", item))
        except Exception as e:
            logger.exception("[llm] 流式生成异常")
            await q.put(("error", e))
        finally:
            await q.put(("done", None))

    async def main():
        q: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(_drain(q, async_gen))
        while True:
            kind, payload = await q.get()
            if kind == "item":
                yield payload
            elif kind == "error":
                yield ("content", f"❌ {payload}")
            elif kind == "done":
                break
        # _drain 已结束，无需 cancel（loop 即将 close）

    agen = main()
    while True:
        try:
            yield loop.run_until_complete(agen.__anext__())
        except StopAsyncIteration:
            break


# ==================================
# 普通模式：LangChain 流式
# ==================================
async def _normal_stream(messages, model: str | None = None) -> Iterator[Tuple[str, str]]:
    model_id = _resolve_model(model)
    llm = get_llm(model_id)
    # LangChain 的 astream 是异步生成器
    async for chunk in llm.astream(messages):
        token = getattr(chunk, "content", None)
        if token:
            yield ("content", token)


# ==================================
# 深度思考：AsyncOpenAI + extra_body={"enable_thinking": True}
#          （与 tomatocat-agent 完全一致的策略）
# ==================================
async def _deep_think_non_stream(messages, model: str | None = None) -> Tuple[str, str]:
    import openai

    model_id = _resolve_model(model)
    client = _make_async_openai()
    resp = await client.chat.completions.create(
        model=model_id,
        messages=messages,
        temperature=0.1,
        max_tokens=4096,
        extra_body={"enable_thinking": True},
    )
    msg = resp.choices[0].message
    content = msg.content or ""
    thinking = ""
    reasoning = getattr(msg, "reasoning_content", None)
    if reasoning:
        thinking = str(reasoning)
    return content, thinking


async def _deep_think_stream(messages, model: str | None = None):
    """
    流式深度思考（参考 tomatocat LLMProvider._chat_streaming）。
    - chunk.delta.reasoning_content → thinking token
    - chunk.delta.content           → content token
    """
    model_id = _resolve_model(model)
    client = _make_async_openai()

    stream = await client.chat.completions.create(
        model=model_id,
        messages=messages,
        stream=True,
        temperature=0.1,
        max_tokens=4096,
        extra_body={"enable_thinking": True},
        stream_options={"include_usage": False},
    )

    async for chunk in stream:
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        if delta is None:
            continue

        reasoning = getattr(delta, "reasoning_content", None)
        if isinstance(reasoning, str) and reasoning:
            yield ("thinking", reasoning)

        content = getattr(delta, "content", None)
        if isinstance(content, str) and content:
            yield ("content", content)


def _make_async_openai():
    """构造 AsyncOpenAI client（国内 API 直连，绕代理）—— 与 tomatocat 一致。"""
    import httpx
    from openai import AsyncOpenAI

    kwargs = dict(
        api_key=API_KEY or "placeholder",
        timeout=120.0,
        max_retries=2,
    )
    if BASE_URL:
        kwargs["base_url"] = BASE_URL

    if _bypass_proxy(BASE_URL):
        # 国内 API 直连，显式 proxy=None 绕过环境变量代理
        kwargs["http_client"] = httpx.AsyncClient(
            proxy=None,
            timeout=httpx.Timeout(120.0, connect=30.0),
        )
        logger.info(f"[llm] {BASE_URL} 为国内 API，已绕过代理直连")

    return AsyncOpenAI(**kwargs)
