"""
嵌入模型封装（Embed 阶段）
默认走 OpenAI 兼容协议，不强制 provider。
如需本地 Ollama / HuggingFace BGE-M3，改 EMBEDDING_PROVIDER 环境变量即可。
"""
from __future__ import annotations

from langchain_core.embeddings import Embeddings

from ..config import EMBEDDING_API_KEY, EMBEDDING_BASE_URL, EMBEDDING_MODEL, EMBEDDING_DIMENSION

# 模块级单例
_instance: Embeddings | None = None


def get_embeddings() -> Embeddings:
    """单例：返回 LangChain Embeddings 对象"""
    global _instance
    if _instance is not None:
        return _instance

    provider = __import__("os").getenv("EMBEDDING_PROVIDER", "openai").lower()

    # ---- Ollama 本地嵌入 ----
    if provider == "ollama":
        from langchain_ollama import OllamaEmbeddings
        _instance = OllamaEmbeddings(
            model=EMBEDDING_MODEL,
            base_url=EMBEDDING_BASE_URL,
        )
        return _instance

    # ---- 本地 HuggingFace BGE-M3（纯离线）----
    if provider == "local":
        from langchain_community.embeddings import HuggingFaceBgeEmbeddings
        _instance = HuggingFaceBgeEmbeddings(
            model_name=EMBEDDING_MODEL,
            model_kwargs={"device": "cpu"},
            encode_kwargs={"normalize_embeddings": True},
        )
        return _instance

    # ---- 默认：OpenAI 兼容协议（智谱/通义/SiliconFlow/DashScope 通用）----
    from langchain_openai import OpenAIEmbeddings
    kwargs = dict(
        model=EMBEDDING_MODEL,
        api_key=EMBEDDING_API_KEY,
        base_url=EMBEDDING_BASE_URL,
    )
    # 注意：SiliconFlow / 部分兼容服务不支持 dimensions 参数，
    # 仅当显式设置且值非默认时才传递
    if EMBEDDING_DIMENSION and EMBEDDING_DIMENSION != 1024:
        kwargs["dimensions"] = EMBEDDING_DIMENSION
    _instance = OpenAIEmbeddings(**kwargs)
    return _instance
