"""
LLM 生成服务（参考 tomatocat-agent + RAG-Pro 的 LLMManager）

核心改动（多 Provider 支持）：
1. AVAILABLE_MODELS 不再硬编码，从 provider_service 动态读取
2. get_llm() 根据 model_id 从 provider_service 查找对应 Provider 配置
3. 每个 Provider 可以有独立的 api_key / base_url / temperature / max_tokens
4. 普通模式 → LangChain ChatOpenAI
5. 深度思考模式 → AsyncOpenAI + extra_body={"enable_thinking": True}
6. 智谱等国内 API → 绕过 HTTP_PROXY 直连
7. 流式解析 delta.reasoning_content / delta.content
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterator, Tuple
from urllib.parse import urlparse

from .provider_service import get_provider_by_model, get_default_provider, list_providers

logger = logging.getLogger(__name__)

# 智谱等国内域名，绕过代理直连
_DOMESTIC_HOSTS = {"open.bigmodel.cn", "api.minimax.chat", "dashscope.aliyuncs.com"}

_llm_instances = {}  # {cache_key: ChatOpenAI 实例}


def _bypass_proxy(base_url: str) -> bool:
    try:
        host = urlparse(base_url or "").hostname or ""
        return any(h in host for h in _DOMESTIC_HOSTS)
    except Exception:
        return False


# ==================================
# 从 provider_service 动态获取配置
# ==================================
def _get_provider_config(model: str | None = None) -> dict:
    """根据 model_id 查找 Provider 配置，找不到就回退到默认"""
    if model:
        p = get_provider_by_model(model)
        if p:
            return p
    p = get_default_provider()
    if p:
        return p
    # 最终兜底（providers.json 为空时）
    from ..config import API_KEY, BASE_URL, MODEL_ID
    return {
        "model_id": MODEL_ID or "glm-4.5-flash",
        "api_key": API_KEY or "",
        "base_url": BASE_URL or "https://open.bigmodel.cn/api/paas/v4",
        "provider": "智谱",
        "supports_deep_think": True,
        "temperature": 0.1,
        "max_tokens": 4096,
    }


def _resolve_model(model: str | None = None) -> str:
    """解析模型 ID：传入则用传入的，否则用默认 Provider 的 model_id"""
    if model:
        return model
    return _get_provider_config().get("model_id", "glm-4.5-flash")


def get_llm(model: str | None = None):
    """普通模式：LangChain ChatOpenAI（不启用思考）"""
    cfg = _get_provider_config(model)
    model_id = cfg["model_id"]
    cache_key = f"{cfg.get('base_url', '')}:{model_id}"

    if cache_key in _llm_instances:
        return _llm_instances[cache_key]

    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model=model_id,
        api_key=cfg.get("api_key") or "placeholder",
        base_url=cfg.get("base_url"),
        temperature=cfg.get("temperature", 0.1),
        max_tokens=cfg.get("max_tokens", 4096),
        streaming=True,
    )
    _llm_instances[cache_key] = llm
    logger.info(f"[llm] 普通模式初始化：{model_id} @ {cfg.get('base_url')}")
    return llm


def model_supports_deep_think(model: str | None = None) -> bool:
    """判断某个模型是否支持深度思考（从 provider 配置读取）"""
    cfg = _get_provider_config(model)
    return cfg.get("supports_deep_think", False)


# ==================================
# 兼容旧接口：动态生成可用模型列表
# ==================================
def get_available_models() -> list[dict]:
    """从 provider_service 动态生成模型列表（兼容 /api/models 端点）"""
    providers = list_providers(mask_key=False)
    models = []
    for p in providers:
        if not p.get("active", True):
            continue
        models.append({
            "id": p.get("model_id", ""),
            "name": p.get("name", p.get("model_id", "")),
            "provider": p.get("provider", ""),
            "default": p.get("is_default", False),
            "supports_deep_think": p.get("supports_deep_think", False),
        })
    if not models:
        models.append({
            "id": "glm-4.5-flash",
            "name": "GLM-4.5-Flash",
            "provider": "智谱",
            "default": True,
            "supports_deep_think": True,
        })
    return models


# 向后兼容：AVAILABLE_MODELS 改为动态函数调用
# chat_router.py 中 [ChatModel(**m) for m in generator_service.AVAILABLE_MODELS]
# 需要遍历，所以用个 proxy 对象
class _AvailableModelsProxy:
    def __iter__(self):
        return iter(get_available_models())
    def __getitem__(self, idx):
        return get_available_models()[idx]
    def __len__(self):
        return len(get_available_models())
    def __repr__(self):
        return repr(get_available_models())
    def __bool__(self):
        return bool(get_available_models())


AVAILABLE_MODELS = _AvailableModelsProxy()


# ==================================
# 同步入口
# ==================================
def chat(messages: list[dict], enable_deep_think: bool = False, model: str | None = None) -> Tuple[str, str]:
    """同步调用。返回 (answer, thinking_text)。"""
    return asyncio.run(_chat_async(messages, enable_deep_think=enable_deep_think, model=model))


async def _chat_async(messages: list[dict], enable_deep_think: bool = False, model: str | None = None) -> Tuple[str, str]:
    model_id = _resolve_model(model)
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
    llm = get_llm(model)
    async for chunk in llm.astream(messages):
        token = getattr(chunk, "content", None)
        if token:
            yield ("content", token)


# ==================================
# 深度思考：AsyncOpenAI + extra_body={"enable_thinking": True}
# ==================================
async def _deep_think_non_stream(messages, model: str | None = None) -> Tuple[str, str]:
    model_id = _resolve_model(model)
    cfg = _get_provider_config(model_id)
    client = _make_async_openai(cfg)
    resp = await client.chat.completions.create(
        model=model_id,
        messages=messages,
        temperature=cfg.get("temperature", 0.1),
        max_tokens=cfg.get("max_tokens", 4096),
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
    """流式深度思考 - chunk.delta.reasoning_content → thinking, delta.content → content"""
    model_id = _resolve_model(model)
    cfg = _get_provider_config(model_id)
    client = _make_async_openai(cfg)

    stream = await client.chat.completions.create(
        model=model_id,
        messages=messages,
        stream=True,
        temperature=cfg.get("temperature", 0.1),
        max_tokens=cfg.get("max_tokens", 4096),
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


def _make_async_openai(cfg: dict | None = None):
    """构造 AsyncOpenAI client（国内 API 直连，绕代理）"""
    import httpx
    from openai import AsyncOpenAI

    if cfg is None:
        cfg = _get_provider_config()

    base_url = cfg.get("base_url", "")
    api_key = cfg.get("api_key", "")

    kwargs = dict(
        api_key=api_key or "placeholder",
        timeout=120.0,
        max_retries=2,
    )
    if base_url:
        kwargs["base_url"] = base_url

    if _bypass_proxy(base_url):
        kwargs["http_client"] = httpx.AsyncClient(
            proxy=None,
            timeout=httpx.Timeout(120.0, connect=30.0),
        )
        logger.info(f"[llm] {base_url} 为国内 API，已绕过代理直连")

    return AsyncOpenAI(**kwargs)
