"""
检索服务 —— 参考 RAG-Pro 的 retrieve 流程：
  1. （可选）Query 改写 / 理解
  2. 向量检索 → 大召回（Top-K=20，阈值 0 不过滤，保证召回率）
  3. 重排序 Rerank（双模式，按 RERANK_PROVIDER 选择）：
       - local  : FlagReranker + bge-reranker-v2-m3，优先复用 RAG-Pro 已有本地模型
       - remote : SiliconFlow /v1/rerank（Cohere 兼容），默认复用 EMBEDDING key/URL
       - auto    : 优先 local（省成本），本地失败/无模型再 remote，都失败则降级
     - 启用条件：RERANK_ENABLE=true 且 enable_rerank != False 且命中数 > 1
     - 失败自动降级：按原向量相似度分数截断
  4. 返回 DocumentChunk 列表（含来源、分数、完整 metadata）
"""
from __future__ import annotations
import logging
import uuid
from pathlib import Path
from typing import Optional

from langchain_core.documents import Document

from ..config import (
    RAG_TOP_K,
    RERANK_ENABLE,
    RERANK_PROVIDER,
    RERANK_MODEL,
    RERANK_DEVICE,
    RERANK_BASE_URL,
    RERANK_API_KEY,
    RERANK_TIMEOUT,
    RERANK_TOP_N,
)
from ..store.vector_store import similarity_search_with_score
from ..models.schemas import DocumentChunk

logger = logging.getLogger(__name__)

# 本地 FlagReranker 模块级单例（懒加载）
_local_reranker = None
_local_reranker_checked = False  # 避免每次都重试模型加载
_last_rerank_backend: Optional[str] = None  # 最近一次成功的 rerank 后端："local" | "remote" | None


# ==================================
# 0. 工具：查找本地 bge-reranker-v2-m3 模型路径
# ==================================
def _has_local_reranker_model() -> Optional[Path]:
    """检查 RAG-Pro 同级目录有没有现成的 bge-reranker-v2-m3 模型（省得重复下）"""
    candidates = [
        Path(__file__).resolve().parent.parent.parent.parent / "RAG-Pro" / "backend" / "models" / "BAAI" / "bge-reranker-v2-m3",
        Path(__file__).resolve().parent.parent / "models" / "BAAI" / "bge-reranker-v2-m3",
    ]
    for p in candidates:
        if not p.exists():
            continue
        has_bin = (p / "pytorch_model.bin").exists()
        has_safe = (p / "model.safetensors").exists()
        if has_bin or has_safe:
            return p
    return None


# ==================================
# 1. Query 改写（目前直接返回，留钩子给后续 HyDE / 查询扩展）
# ==================================
def rewrite_query(question: str) -> str:
    return question.strip()


# ==================================
# 2. 检索主流程：大召回 → 可选 rerank → 截断
# ==================================
def retrieve(
    question: str,
    top_k: Optional[int] = None,
    threshold: Optional[float] = None,
    enable_rerank: Optional[bool] = None,
) -> tuple[list[DocumentChunk], dict]:
    """
    对外主入口，返回 (chunks, debug_info)。

    debug_info 结构：
    {
      "raw_hits": int,
      "kept": int,
      "rerank_enabled": bool,
      "rerank_backend": "local" | "remote" | "downgraded" | "none",
      "top_relevance": float | None,
      "chunks_debug": [
        {"chunk_id": str, "source_file": str,
         "vec_rank": int, "final_rank": int, "change": "up"|"down"|"stay"}
      ]
    }
    """
    # --- 第一步：大召回 ---
    recall_k = max(20, top_k or RAG_TOP_K)
    th = 0.0 if threshold is None else threshold

    query = rewrite_query(question)
    docs_scores = similarity_search_with_score(query, k=recall_k, score_threshold=th)
    raw_hits = len(docs_scores)
    logger.info(f"[retrieve] query='{query[:50]}', recall_k={recall_k}, threshold={th}, raw_hits={raw_hits}")

    def _empty_debug() -> dict:
        return {
            "raw_hits": 0, "kept": 0,
            "rerank_enabled": False, "rerank_backend": "none",
            "top_relevance": None, "chunks_debug": [],
        }

    if not docs_scores:
        return [], _empty_debug()

    # --- 先计算向量排名 vec_rank（按相似度降序，1-based）---
    docs_sorted_by_vec = sorted(docs_scores, key=lambda x: x[1], reverse=True)
    doc_id_to_vec_rank: dict[int, int] = {}
    for pos, (doc, _sim) in enumerate(docs_sorted_by_vec, start=1):
        doc_id_to_vec_rank[id(doc)] = pos

    # --- 第二步：决定是否 rerank（传参 > 配置 > 默认）---
    use_rerank = enable_rerank if enable_rerank is not None else RERANK_ENABLE
    use_rerank = bool(use_rerank and raw_hits > 1)

    keep_n = min(
        raw_hits,
        (top_k if top_k is not None else (RERANK_TOP_N if use_rerank else RAG_TOP_K)),
    )

    rerank_backend: str = "none"
    top_relevance: Optional[float] = None
    global _last_rerank_backend
    _last_rerank_backend = None

    if use_rerank:
        reranked = _rerank(query, docs_scores, top_n=keep_n)
        if reranked is not None:
            docs_scores_final = reranked
            rerank_backend = _last_rerank_backend or "remote"
            top_relevance = round(float(docs_scores_final[0][1]), 4)
            top_score = top_relevance
            src3 = [d.metadata.get("source", "?")[:20] for d, _ in docs_scores_final[:3]]
            logger.info(
                f"[retrieve] rerank OK, keep {len(docs_scores_final)}/{raw_hits}, "
                f"top_relevance={top_score:.4f}, top_sources={src3}, backend={rerank_backend}"
            )
        else:
            docs_scores.sort(key=lambda x: x[1], reverse=True)
            docs_scores_final = docs_scores[:keep_n]
            rerank_backend = "downgraded"
            if docs_scores_final and top_relevance is None:
                top_relevance = round(float(docs_scores_final[0][1]), 4)
            logger.info(f"[retrieve] rerank 降级（失败/超时/无可用后端），按向量分数截断 top_{keep_n}")
    else:
        docs_scores.sort(key=lambda x: x[1], reverse=True)
        docs_scores_final = docs_scores[:keep_n]
        if enable_rerank and not RERANK_API_KEY and not _has_local_reranker_model():
            logger.warning("[retrieve] enable_rerank=True 但 RERANK_API_KEY 未配置且无本地模型，跳过 rerank")
        # 未启用 rerank 时，用向量检索的最高分作为 top_relevance
        if docs_scores_final and top_relevance is None:
            top_relevance = round(float(docs_scores_final[0][1]), 4)

    # --- 第三步：统一格式 DocumentChunk + 计算排名变化 ---
    chunks: list[DocumentChunk] = []
    chunks_debug: list[dict] = []

    for final_pos, (doc, sim) in enumerate(docs_scores_final, start=1):
        meta = dict(doc.metadata or {})
        vec_rank = doc_id_to_vec_rank.get(id(doc), final_pos)
        final_rank = final_pos
        # 排名：数字越小越好（第1名最好），所以 vec_rank > final_rank → 上升
        if vec_rank < final_rank:
            change = "down"
        elif vec_rank > final_rank:
            change = "up"
        else:
            change = "stay"

        meta["__vec_rank"] = vec_rank
        meta["__final_rank"] = final_rank
        meta["__change"] = change

        # parent_child 或相邻 3 块模式都可能带 parent_content；
        # 这里只记录到 metadata，prompt 构建时统一优先取 parent_content
        chunk_method = meta.get("chunk_method") or "legacy_parent_adjacent"

        chunk = DocumentChunk(
            chunk_id=meta.get("chunk_id") or meta.get("id") or uuid.uuid4().hex,
            content=doc.page_content,
            source_file=meta.get("source") or meta.get("file_name") or meta.get("filename") or "未知来源",
            page=meta.get("page") or meta.get("page_number"),
            score=round(float(sim), 4),
            metadata=meta,
        )
        chunks.append(chunk)
        chunks_debug.append({
            "chunk_id": chunk.chunk_id,
            "source_file": chunk.source_file,
            "chunk_method": chunk_method,
            "has_parent_content": bool(meta.get("parent_content")),
            "vec_rank": vec_rank,
            "final_rank": final_rank,
            "change": change,
        })

    debug_info = {
        "raw_hits": raw_hits,
        "kept": len(chunks),
        "rerank_enabled": bool(use_rerank),
        "rerank_backend": rerank_backend,
        "top_relevance": top_relevance,
        "chunks_debug": chunks_debug,
    }

    if chunks:
        logger.info(
            f"[retrieve] 返回 {len(chunks)} 个块, "
            f"top_score={chunks[0].score:.4f}, "
            f"sources={[c.source_file for c in chunks]}"
        )
    return chunks, debug_info


# ==================================
# 3. Rerank 重排序（auto / local / remote 三模式）
# ==================================
def _rerank(
    query: str,
    docs_scores: list[tuple[Document, float]],
    top_n: int,
) -> Optional[list[tuple[Document, float]]]:
    """
    按 RERANK_PROVIDER 选择后端做 rerank。

    - auto   : 优先 local，本地失败/无模型 → remote，remote 再失败 → None（降级）
    - local  : 只试本地 FlagReranker，失败 → None
    - remote : 只试 SiliconFlow HTTP API，失败 → None

    返回：
      - 成功：重新排序后的 list[tuple[Document, float]]（float = 0~1 的 relevance_score）
      - 失败/超时/无可用后端：None，调用方降级走向量分数截断
    """
    if not docs_scores:
        return None

    provider = RERANK_PROVIDER or "auto"
    n = len(docs_scores)
    keep = min(max(top_n, 1), n)

    # --- 抽取 passages（统一准备，两边都要用）---
    passages: list[str] = []
    for doc, _ in docs_scores:
        text = (doc.page_content or "").strip()
        # 单条不要太长：远端 2000，本地 4000（本地有更多 RAM）
        cap = 4000 if provider == "local" else 2000
        if len(text) > cap:
            text = text[:cap]
        passages.append(text or " ")

    # ------- 模式 1：只走 local -------
    if provider == "local":
        return _rerank_local(query, passages, docs_scores, top_n=keep)

    # ------- 模式 2：只走 remote -------
    if provider == "remote":
        return _rerank_remote(query, passages, docs_scores, top_n=keep)

    # ------- 模式 3（默认 auto）：优先 local，失败再 remote -------
    local_path = _has_local_reranker_model()
    if local_path is not None:
        res = _rerank_local(query, passages, docs_scores, top_n=keep)
        if res is not None:
            return res
        logger.warning("[rerank] local 后端失败，auto 模式下回退到 remote")
    else:
        logger.info("[rerank] 未检测到本地 rerank 模型，auto 模式走 remote")

    return _rerank_remote(query, passages, docs_scores, top_n=keep)


# ==================================
# 3a. 本地 FlagReranker（FlagEmbedding，和 RAG-Pro 一致）
# ==================================
def _rerank_local(
    query: str,
    passages: list[str],
    docs_scores: list[tuple[Document, float]],
    top_n: int,
) -> Optional[list[tuple[Document, float]]]:
    """本地 CrossEncoder rerank。失败/加载异常返回 None，不抛。"""
    global _local_reranker, _local_reranker_checked

    try:
        # 懒加载（只尝试一次；失败了就不再反复试以免卡顿）
        if _local_reranker is None and not _local_reranker_checked:
            _local_reranker_checked = True

            local_path = _has_local_reranker_model()
            if local_path is None:
                logger.info("[rerank] 未找到本地 bge-reranker-v2-m3 模型，跳过 local 后端")
                return None

            # 如果没装 FlagEmbedding，这里会报错 → None
            from FlagEmbedding import FlagReranker

            use_fp16 = RERANK_DEVICE not in {"cpu", ""}
            logger.info(f"[rerank] 加载本地 FlagReranker: {local_path} (device={RERANK_DEVICE}, fp16={use_fp16})")
            _local_reranker = FlagReranker(str(local_path), use_fp16=use_fp16)
    except Exception as e:
        logger.warning(f"[rerank] 本地 FlagReranker 加载失败：{e}")
        _local_reranker = None
        return None

    if _local_reranker is None:
        return None

    try:
        pairs = [[query, p] for p in passages]
        scores = _local_reranker.compute_score(pairs, normalize=True)
        # 兼容单条返回 float 的情况
        if isinstance(scores, (int, float)):
            scores = [scores]
        scores = list(scores)

        indexed = list(enumerate(scores))
        indexed.sort(key=lambda x: x[1], reverse=True)
        indexed = indexed[:top_n]

        reranked: list[tuple[Document, float]] = []
        used: set[int] = set()
        n = len(docs_scores)
        for idx, score in indexed:
            if idx in used or idx < 0 or idx >= n:
                continue
            used.add(idx)
            reranked.append((docs_scores[idx][0], float(score)))

        if not reranked:
            logger.warning("[rerank] local 返回空结果，降级")
            return None
        _last_rerank_backend = "local"
        return reranked

    except Exception as e:
        logger.warning(f"[rerank] local compute_score 失败：{e}，降级")
        return None


# ==================================
# 3b. SiliconFlow HTTP /v1/rerank（Cohere 兼容）
# ==================================
def _rerank_remote(
    query: str,
    passages: list[str],
    docs_scores: list[tuple[Document, float]],
    top_n: int,
) -> Optional[list[tuple[Document, float]]]:
    """远端 SiliconFlow Cohere 兼容 rerank。失败/超时/无配置返回 None，不抛。"""
    if not (RERANK_API_KEY and RERANK_BASE_URL and RERANK_MODEL):
        logger.warning("[rerank] 跳过 remote：RERANK_API_KEY/BASE_URL/MODEL 未配置")
        return None

    import httpx

    url = RERANK_BASE_URL.rstrip("/") + "/rerank"
    headers = {
        "Authorization": f"Bearer {RERANK_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": RERANK_MODEL,
        "query": query,
        "documents": passages,
        "top_n": top_n,
    }

    try:
        with httpx.Client(timeout=httpx.Timeout(RERANK_TIMEOUT, connect=3.0)) as client:
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                try:
                    err_body = resp.json()
                except Exception:
                    err_body = resp.text[:200]
                logger.warning(
                    f"[rerank] remote HTTP {resp.status_code} 失败：{err_body}，降级"
                )
                return None
            data = resp.json()
    except httpx.TimeoutException:
        logger.warning(f"[rerank] remote 超时（>{RERANK_TIMEOUT}s），降级")
        return None
    except Exception as e:
        logger.warning(f"[rerank] remote 请求异常：{e}，降级")
        return None

    # 解析：results = [{"index": 0, "relevance_score": 0.92, "text": "..."}, ...]
    results = data.get("results") or []
    if not results:
        logger.warning("[rerank] remote 返回 results 为空，降级")
        return None

    reranked: list[tuple[Document, float]] = []
    n = len(docs_scores)
    used: set[int] = set()
    for r in results:
        idx = r.get("index")
        if idx is None or idx < 0 or idx >= n or idx in used:
            continue
        used.add(idx)
        score = float(r.get("relevance_score") or 0.0)
        reranked.append((docs_scores[idx][0], score))

    if not reranked:
        logger.warning("[rerank] remote 解析后无有效结果，降级")
        return None

    _last_rerank_backend = "remote"
    return reranked
