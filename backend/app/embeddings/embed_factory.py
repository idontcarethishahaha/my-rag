"""
嵌入模型封装 —— 参考 RAG-Pro 的 BGEM3Embedder。
优先级：
  1. EMBEDDING_PROVIDER=local → 本地 FlagEmbedding BGE-M3（最稳，推荐）
  2. EMBEDDING_PROVIDER=ollama → 本地 Ollama
  3. 默认 → OpenAI 兼容协议（智谱 / SiliconFlow 等远端 API）

注意：SiliconFlow / 智谱 embedding-3 等远端服务，**不支持 dimensions 参数**，传了会 400，必须严格不传。
"""
from __future__ import annotations
import logging
import os
from pathlib import Path

from langchain_core.embeddings import Embeddings

from ..config import (
    EMBEDDING_API_KEY,
    EMBEDDING_BASE_URL,
    EMBEDDING_MODEL,
    EMBEDDING_DIMENSION,
)

logger = logging.getLogger(__name__)

# 模块级单例
_instance: Embeddings | None = None


def _has_local_bgem3() -> Path | None:
    """检查 RAG-Pro 同级目录有没有现成的 BGE-M3 模型（省得重复下）"""
    candidates = [
        Path(__file__).resolve().parent.parent.parent.parent / "RAG-Pro" / "backend" / "models" / "BAAI" / "bge-m3",
        Path(__file__).resolve().parent.parent / "models" / "BAAI" / "bge-m3",
    ]
    for p in candidates:
        if p.exists() and (p / "pytorch_model.bin").exists():
            return p
    return None


# ==================================
# 本地 BGE-M3（FlagEmbedding）—— 与 RAG-Pro 完全一致
# ==================================
class _LocalBGEM3(Embeddings):
    """用 FlagEmbedding 本地加载 BGE-M3，返回 dense 向量。"""

    def __init__(self, model_path: Path, device: str = "cpu", batch_size: int = 32):
        from FlagEmbedding import BGEM3FlagModel
        logger.info(f"[embed] 加载本地 BGE-M3: {model_path} (device={device})")
        self._model = BGEM3FlagModel(str(model_path), use_fp16=(device != "cpu"))
        self._batch = batch_size

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out = self._model.encode(
            texts,
            batch_size=self._batch,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
        return out["dense_vecs"].tolist()

    def embed_query(self, text: str) -> list[float]:
        out = self._model.encode(
            [text],
            batch_size=1,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
        return out["dense_vecs"][0].tolist()


# ==================================
# 工厂入口
# ==================================
def get_embeddings() -> Embeddings:
    """单例：返回 LangChain Embeddings 对象"""
    global _instance
    if _instance is not None:
        return _instance

    provider = os.getenv("EMBEDDING_PROVIDER", "").strip().lower()

    # ---- 1) 本地 FlagEmbedding BGE-M3（优先，稳定，不会 400）----
    local_path = _has_local_bgem3()
    if provider == "local" or (not provider and local_path is not None):
        try:
            if local_path is None:
                raise RuntimeError("未找到本地 BGE-M3 模型目录")
            _instance = _LocalBGEM3(local_path)
            logger.info("[embed] 使用本地 BGE-M3（FlagEmbedding）")
            return _instance
        except Exception as e:
            logger.warning(f"[embed] 本地 BGE-M3 加载失败，回退到远端 API：{e}")

    # ---- 2) Ollama 本地嵌入 ----
    if provider == "ollama":
        from langchain_ollama import OllamaEmbeddings
        _instance = OllamaEmbeddings(
            model=EMBEDDING_MODEL,
            base_url=EMBEDDING_BASE_URL,
        )
        logger.info(f"[embed] 使用 Ollama: {EMBEDDING_MODEL}")
        return _instance

    # ---- 3) 默认：OpenAI 兼容协议（SiliconFlow / 智谱 等远端 API）----
    from langchain_openai import OpenAIEmbeddings

    kwargs = dict(
        model=EMBEDDING_MODEL,
        api_key=EMBEDDING_API_KEY,
        base_url=EMBEDDING_BASE_URL,
    )
    # ⚠️ 关键修复：dimensions 参数只有在显式设置且非 0 时才传。
    # 不传就不会触发 SiliconFlow / 智谱 embedding 的 400 "parameter invalid" 报错
    if EMBEDDING_DIMENSION and EMBEDDING_DIMENSION > 0:
        kwargs["dimensions"] = EMBEDDING_DIMENSION

    _instance = OpenAIEmbeddings(**kwargs)
    logger.info(f"[embed] 使用远端 API: {EMBEDDING_BASE_URL} model={EMBEDDING_MODEL} (dimensions 已显式控制)")
    return _instance
