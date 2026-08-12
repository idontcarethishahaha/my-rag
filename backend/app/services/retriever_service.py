"""
检索服务 —— 参考 RAG-Pro 的 retrieve 流程：
  1. （可选）Query 改写 / 理解
  2. 向量检索 → 大召回（Top-K=20，阈值 0 不过滤）
  3. （可选）重排序 Rerank —— 暂不引入本地 CrossEncoder 以免依赖太重，先按相似度截断到 6
  4. 返回 DocumentChunk 列表（含来源、分数、完整 metadata）
"""
from __future__ import annotations
import uuid
from typing import Optional

from langchain_core.documents import Document

from ..config import RAG_TOP_K, RAG_RERANK_TOP_N
from ..store.vector_store import similarity_search_with_score
from ..models.schemas import DocumentChunk


# ==================================
# 1. Query 改写（目前直接返回，留钩子给后续 HyDE / 查询扩展）
# ==================================
def rewrite_query(question: str) -> str:
    return question.strip()


# ==================================
# 2. 检索主流程（严格参考 RAG-Pro：先大召回 → 再截断到 Top-N）
# ==================================
def retrieve(
    question: str,
    top_k: Optional[int] = None,
    threshold: Optional[float] = None,
    enable_rerank: Optional[bool] = None,
) -> list[DocumentChunk]:
    """
    对外主入口：
      - 先用较大的 k 召回（默认 20，阈值 0 不过滤，保证召回率）
      - 然后按分数排序，截断到 6（RAG-RRO 默认 RERANK_TOP_N=5/6）
    """
    # --- 第一步：大召回 ---
    recall_k = max(20, top_k or RAG_TOP_K)  # 至少 20，避免漏
    # 阈值：显式传了就用；默认 0 不过滤（最大化召回，避免"明明有资料却检索不到"）
    th = 0.0 if threshold is None else threshold

    query = rewrite_query(question)
    docs_scores = similarity_search_with_score(query, k=recall_k, score_threshold=th)
    if not docs_scores:
        return []

    # --- 第二步：按相似度从高到低取前 N（默认 6） ---
    top_n = min(len(docs_scores), (6 if top_k is None else top_k))
    docs_scores.sort(key=lambda x: x[1], reverse=True)
    docs_scores = docs_scores[:top_n]

    # --- 第三步：统一格式 DocumentChunk ---
    chunks: list[DocumentChunk] = []
    for doc, sim in docs_scores:
        meta = doc.metadata or {}
        chunks.append(DocumentChunk(
            chunk_id=meta.get("chunk_id") or meta.get("id") or uuid.uuid4().hex,
            content=doc.page_content,
            # 来源优先级：source > file_name > 未知来源
            source_file=meta.get("source") or meta.get("file_name") or meta.get("filename") or "未知来源",
            page=meta.get("page") or meta.get("page_number"),
            score=round(float(sim), 4),
            # 把所有 metadata 透传（方便 Prompt 拼 section_title、parent_content 等）
            metadata={k: v for k, v in meta.items()},
        ))
    return chunks


# ==================================
# 3. 重排序接口（占位，按分数截断，后续可接 BGE-Reranker-v2-m3）
# ==================================
def _rerank(
    query: str,
    docs_scores: list[tuple[Document, float]],
    top_n: int,
) -> list[tuple[Document, float]]:
    """TODO: 接 BGE-Reranker-v2-m3（本地 CrossEncoder）或 LLM-as-Judge"""
    return docs_scores[:top_n]
