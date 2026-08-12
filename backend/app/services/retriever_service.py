"""
检索服务（Retrieve / Re-rank 阶段）
流程：
  1. （可选）Query 改写 / 理解 —— 将口语化问题重写成检索友好的查询
  2. 向量检索 —— 调用向量库 similarity_search，召回 Top-K
  3. （可选）重排序 Rerank —— 用 CrossEncoder 或大模型精细打分，截到 Top-N
  4. 返回 DocumentChunk 列表（含来源、分数）
"""
from __future__ import annotations
import uuid
from typing import Optional

from langchain_core.documents import Document

from ..config import RAG_TOP_K, RAG_SIMILARITY_THRESHOLD, RAG_ENABLE_RERANK, RAG_RERANK_TOP_N
from ..store.vector_store import similarity_search_with_score
from ..models.schemas import DocumentChunk


# ==================================
# 1. Query 改写（可选）
# ==================================
def rewrite_query(question: str, llm=None) -> str:
    """
    把口语化的用户问题改写成适合向量检索的查询文本。
    例：用户说 "那个多少钱？"  → 结合上下文改写为具体问题。
    目前直接返回原句（骨架），后续可接入 LLM 做 HyDE / Query Expansion。
    """
    return question.strip()


# ==================================
# 2. 检索 + 重排
# ==================================
def retrieve(
    question: str,
    top_k: Optional[int] = None,
    threshold: Optional[float] = None,
    enable_rerank: Optional[bool] = None,
) -> list[DocumentChunk]:
    """
    对外主入口：检索 -> （重排）-> 返回规范化的 DocumentChunk 列表。
    """
    k = top_k or RAG_TOP_K
    th = threshold if threshold is not None else RAG_SIMILARITY_THRESHOLD
    rerank = enable_rerank if enable_rerank is not None else RAG_ENABLE_RERANK

    # 1) 查询改写（留钩子，目前不变）
    query = rewrite_query(question)

    # 2) 向量检索
    docs_scores = similarity_search_with_score(query, k=k, score_threshold=th)
    if not docs_scores:
        return []

    # 3) 重排（如果启用 + 有重排器）
    if rerank:
        docs_scores = _rerank(query, docs_scores, RAG_RERANK_TOP_N)

    # 4) 转为统一格式 DocumentChunk
    chunks: list[DocumentChunk] = []
    for doc, sim in docs_scores:
        chunks.append(DocumentChunk(
            chunk_id=doc.metadata.get("chunk_id") or uuid.uuid4().hex,
            content=doc.page_content,
            source_file=doc.metadata.get("source") or doc.metadata.get("file_name") or "未知来源",
            page=doc.metadata.get("page"),
            score=round(sim, 4),
            metadata={k: v for k, v in doc.metadata.items() if k not in {"source", "page", "chunk_id"}},
        ))
    return chunks


# ==================================
# 3. 重排（可选）
# ==================================
def _rerank(
    query: str,
    docs_scores: list[tuple[Document, float]],
    top_n: int,
) -> list[tuple[Document, float]]:
    """
    重排序骨架。
    两种方案：
      A. 本地 BGE-Reranker（CrossEncoder）—— 免费，需 sentence-transformers，吃 CPU
      B. LLM-as-Judge 打分 —— 贵，但效果好，适合小 K
    这里先写一个 LLM 方案的接口占位，后续可根据配置替换。
    """
    try:
        # 方案 A: BGE Reranker（如果装了 FlagEmbedding）
        # from FlagEmbedding import FlagReranker
        # reranker = FlagReranker("BAAI/bge-reranker-v2-m3", use_fp16=True)
        # pairs = [[query, d.page_content] for d, _ in docs_scores]
        # scores = reranker.compute_score(pairs, normalize=True)
        # combined = list(zip([d for d,_ in docs_scores], scores))
        # combined.sort(key=lambda x: x[1], reverse=True)
        # return combined[:top_n]
        pass
    except Exception:
        pass

    # 兜底：不重排，直接截断前 top_n
    return docs_scores[:top_n]
