"""
检索服务 —— Multi-Query + HyDE + Hybrid Search 完整链路：
  1. 查询重构：Multi-Query 多查询分解（主查询关键词+同义词 + N-1 子查询）+ HyDE 假设文档（可选）
  2. 多路 Hybrid Search：每路 dense + BM25 sparse → 全部 RRF 融合
  3. 重排序 Rerank（auto/local/remote 三模式，失败自动降级）
  4. 置信度评分（0.6*top + 0.4*avg）
  5. 返回 DocumentChunk 列表（含来源、分数、完整 metadata）
"""
from __future__ import annotations
import json
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
    API_KEY,
    BASE_URL,
    MULTI_QUERY_ENABLE,
    MULTI_QUERY_COUNT,
    HYDE_ENABLE,
    HYDE_DOC_LEN,
)
from ..store.vector_store import similarity_search_with_score, hybrid_search, hybrid_search_multi
from ..models.schemas import DocumentChunk
from ..utils.prompt_templates import compute_confidence, confidence_label

logger = logging.getLogger(__name__)

_local_reranker = None
_local_reranker_checked = False
_last_rerank_backend: Optional[str] = None


# ==================================
# 0. 工具：查找本地 bge-reranker-v2-m3 模型路径
# ==================================
def _has_local_reranker_model() -> Optional[Path]:
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
# 1. 查询重构：Multi-Query 多查询分解 + HyDE 假设文档
# ==================================
_MULTI_QUERY_SYSTEM = """你是一个专业的查询分解器，用于知识库检索前的查询重构。

任务：把用户的问题改写并分解为 {n} 个**互相差异最大化**的检索查询，返回 JSON。

## 关键规则（必须严格遵守）
1. 【主查询】第 1 个：把原问题压缩为空格分隔的关键词组合，并补充同义词
   - 去掉口语和疑问词，只保留核心关键词
   - 包含原问题中所有重要实体/概念
2. 【子查询】第 2~{n} 个：**每个子查询必须聚焦原问题中一个独立的子话题/子领域**
   - ❌ 禁止：在相同内容上换后缀（如"玩法介绍"/"系统特点"/"内容说明"——这是无效的）
   - ✅ 必须：每个子查询对应原问题中一个**不同的子话题**，关键词交集应 < 50%
3. 如果原问题的独立子话题数 > {n}-1（子查询槽位不足），按以下规则合并：
   - **合并最相关的子话题**（同领域/同模块的子话题合并到同一个子查询）
   - 被合并的子话题在该子查询中权重相等，不要刻意偏向某一个
4. 不要编造原问题中不存在的实体或概念
5. 所有输出用空格分隔的关键词组合，不要句子

## 示例
原问题："我想了解明日方舟里的肉鸽模式、基建系统怎么玩，还有干员养成体系和危机合约这些长期玩法"
→ 主查询："明日方舟 肉鸽模式 基建系统 干员养成 危机合约 长期玩法"
→ 子查询2："明日方舟 肉鸽模式 Roguelike 玩法 规则"      ← 只管肉鸽
→ 子查询3："明日方舟 基建系统 制造站 贸易站 功能"        ← 只管基建
（4 路时）子查询4："明日方舟 干员养成 星级 技能 精英化"   ← 只管干员养成
（5 路时）子查询5："明日方舟 危机合约 赛季 挑战 奖励"    ← 只管危机合约

原问题（子话题数 > 槽位）："明日方舟里的干员养成、基建、家具系统、活动玩法、主线剧情" 限 3 路
→ 主查询："明日方舟 干员养成 基建 家具系统 活动玩法 主线剧情"
→ 子查询2："明日方舟 干员养成 星级 技能 精英化"          ← 只管干员养成
→ 子查询3："明日方舟 基建 家具系统 装饰 建造"            ← 合并基建+家具（同领域）
→ 子查询4 不够，活动玩法+主线剧情合并为一个子查询

返回格式（严格 JSON，不要 markdown 代码块）：
{{"queries": ["主查询", "子查询2", "子查询3"]}}

只输出 JSON，不要任何解释。"""

_HYDE_SYSTEM = """你是一个知识库假设文档生成器（HyDE 技术）。

任务：根据用户问题，"编"一段假设性的答案文档。这段文档：
1. 用陈述句写成，就像知识库文档中真实存在的一段内容
2. 包含问题的核心实体、概念、关键词（用文档作者会使用的术语）
3. 长度约 {n} 字左右
4. 不需要事实完全正确——目的是用它的语义去检索真实文档
5. 不要写"根据知识库""如上所述"这类元话语，直接写内容本身

例如问题"蜂医有什么功效"，假设文档可以写：
"蜂医是一种传统疗法，主要功效包括缓解咽喉肿痛、辅助治疗呼吸道疾病。其作用机制源于蜂毒中的活性成分……"

只输出假设文档内容，不要任何解释或前缀。"""


def rewrite_query(question: str) -> str:
    """单查询改写（保留做降级兼容）：直接返回原文。多查询模式用 generate_multi_queries()。"""
    return question.strip()


def _call_fast_llm(system: str, user: str, max_tokens: int = 384) -> str:
    """调 glm-4-flash 做一次非流式调用"""
    from langchain_openai import ChatOpenAI
    llm = ChatOpenAI(
        model="glm-4-flash",
        api_key=API_KEY or "placeholder",
        base_url=BASE_URL,
        temperature=0.0,
        max_tokens=max_tokens,
        streaming=False,
    )
    resp = llm.invoke([
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ])
    return (resp.content or "").strip()


def generate_multi_queries(question: str) -> tuple[list[str], Optional[str]]:
    """
    多查询分解 + HyDE 生成。

    返回 (queries, hyde_doc)：
    - queries: 主查询 + N-1 个子查询（MULTI_QUERY_ENABLE=false 时只返回 [question]）
    - hyde_doc: HyDE 假设文档（HYDE_ENABLE=true 时生成，否则 None）
    任何 LLM 失败都降级为 [question]，不影响主流程。
    """
    question = question.strip()
    queries: list[str] = [question]
    hyde_doc: Optional[str] = None

    # --- Multi-Query 分解 ---
    if MULTI_QUERY_ENABLE and len(question) >= 6:
        try:
            n = max(2, MULTI_QUERY_COUNT)
            system = _MULTI_QUERY_SYSTEM.format(n=n)
            raw = _call_fast_llm(system, question, max_tokens=384)
            # 解析 JSON（容忍 markdown 代码块）
            raw = raw.strip()
            if raw.startswith("```"):
                lines = raw.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                raw = "\n".join(lines).strip()
            data = json.loads(raw)
            qlist = [str(q).strip() for q in data.get("queries", []) if str(q).strip()]
            if qlist:
                queries = qlist[:n]
                logger.info(f"[multi_query] 分解为 {len(queries)} 个查询: {queries}")
        except Exception as e:
            logger.warning(f"[multi_query] 分解失败，降级为原问题: {e}")

    # --- HyDE 假设文档 ---
    if HYDE_ENABLE:
        try:
            system = _HYDE_SYSTEM.format(n=HYDE_DOC_LEN)
            doc = _call_fast_llm(system, question, max_tokens=512)
            if doc and len(doc) >= 20:
                hyde_doc = doc
                logger.info(f"[hyde] 假设文档已生成({len(doc)}字): {doc[:60]}...")
            else:
                logger.warning("[hyde] 假设文档过短，跳过")
        except Exception as e:
            logger.warning(f"[hyde] 生成失败，跳过: {e}")

    return queries, hyde_doc


# ==================================
# 2. 检索主流程：Hybrid Search → 可选 rerank → 截断 → 置信度
# ==================================
def retrieve(
    question: str,
    top_k: Optional[int] = None,
    threshold: Optional[float] = None,
    enable_rerank: Optional[bool] = None,
    enable_hybrid: bool = True,
) -> tuple[list[DocumentChunk], dict]:
    """
    对外主入口，返回 (chunks, debug_info)。
    """
    # --- 第一步：查询重构（Multi-Query 分解 + HyDE）---
    queries, hyde_doc = generate_multi_queries(question)
    # rerank 用主查询（queries[0]）作为 query-passage 匹配的 query
    query = queries[0]

    # --- 第二步：多路大召回（每路 dense + sparse，RRF 融合）---
    recall_k = max(20, top_k or RAG_TOP_K)
    th = 0.0 if threshold is None else threshold

    if enable_hybrid:
        docs_scores = hybrid_search_multi(queries, hyde_doc=hyde_doc, k=recall_k, filter=None)
        search_mode = "hybrid_multi"
    else:
        # 纯 dense：多查询各搜一路，按首次出现顺序合并
        seen: dict[str, tuple[Document, float]] = {}
        for q in queries:
            for doc, score in similarity_search_with_score(q, k=recall_k, score_threshold=th):
                if doc.page_content not in seen:
                    seen[doc.page_content] = (doc, score)
        docs_scores = sorted(seen.values(), key=lambda x: x[1], reverse=True)
        search_mode = "dense_multi"

    raw_hits = len(docs_scores)
    logger.info(
        f"[retrieve] queries={len(queries)}, hyde={'on' if hyde_doc else 'off'}, "
        f"mode={search_mode}, recall_k={recall_k}, raw_hits={raw_hits}"
    )

    def _empty_debug() -> dict:
        return {
            "raw_hits": 0, "kept": 0,
            "rerank_enabled": False, "rerank_backend": "none",
            "top_relevance": None,
            "confidence": 0.0, "confidence_label": "very_low",
            "search_mode": search_mode,
            "search_queries": queries,
            "hyde_doc": hyde_doc,
            "chunks_debug": [],
        }

    if not docs_scores:
        return [], _empty_debug()

    # --- 向量排名 vec_rank ---
    docs_sorted_by_vec = sorted(docs_scores, key=lambda x: x[1], reverse=True)
    doc_id_to_vec_rank: dict[int, int] = {}
    for pos, (doc, _sim) in enumerate(docs_sorted_by_vec, start=1):
        doc_id_to_vec_rank[id(doc)] = pos

    # --- 第三步：决定是否 rerank ---
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
            logger.info(
                f"[retrieve] rerank OK, keep {len(docs_scores_final)}/{raw_hits}, "
                f"top_relevance={top_relevance:.4f}, backend={rerank_backend}"
            )
        else:
            docs_scores.sort(key=lambda x: x[1], reverse=True)
            docs_scores_final = docs_scores[:keep_n]
            rerank_backend = "downgraded"
            if docs_scores_final and top_relevance is None:
                top_relevance = round(float(docs_scores_final[0][1]), 4)
            logger.info(f"[retrieve] rerank 降级，按分数截断 top_{keep_n}")
    else:
        docs_scores.sort(key=lambda x: x[1], reverse=True)
        docs_scores_final = docs_scores[:keep_n]
        if docs_scores_final and top_relevance is None:
            top_relevance = round(float(docs_scores_final[0][1]), 4)

    # --- 第四步：置信度评分 ---
    all_scores = [float(sim) for _, sim in docs_scores_final]
    confidence = compute_confidence(all_scores)
    conf_label = confidence_label(confidence)

    # --- 第五步：统一格式 DocumentChunk + 排名变化 ---
    chunks: list[DocumentChunk] = []
    chunks_debug: list[dict] = []

    for final_pos, (doc, sim) in enumerate(docs_scores_final, start=1):
        meta = dict(doc.metadata or {})
        vec_rank = doc_id_to_vec_rank.get(id(doc), final_pos)
        final_rank = final_pos
        if vec_rank < final_rank:
            change = "down"
        elif vec_rank > final_rank:
            change = "up"
        else:
            change = "stay"

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
            "has_keywords": bool(meta.get("keywords")),
            "has_questions": bool(meta.get("questions")),
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
        "confidence": round(confidence, 4),
        "confidence_label": conf_label,
        "search_mode": search_mode,
        "search_queries": queries,
        "hyde_doc": hyde_doc,
        "chunks_debug": chunks_debug,
    }

    if chunks:
        logger.info(
            f"[retrieve] 返回 {len(chunks)} 块, "
            f"top={chunks[0].score:.4f}, conf={confidence:.2f}({conf_label}), "
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
    if not docs_scores:
        return None

    provider = RERANK_PROVIDER or "auto"
    n = len(docs_scores)
    keep = min(max(top_n, 1), n)

    passages: list[str] = []
    for doc, _ in docs_scores:
        text = (doc.page_content or "").strip()
        cap = 4000 if provider == "local" else 2000
        if len(text) > cap:
            text = text[:cap]
        passages.append(text or " ")

    if provider == "local":
        return _rerank_local(query, passages, docs_scores, top_n=keep)

    if provider == "remote":
        return _rerank_remote(query, passages, docs_scores, top_n=keep)

    # auto
    local_path = _has_local_reranker_model()
    if local_path is not None:
        res = _rerank_local(query, passages, docs_scores, top_n=keep)
        if res is not None:
            return res
        logger.warning("[rerank] local 后端失败，auto 模式下回退到 remote")
    else:
        logger.info("[rerank] 未检测到本地 rerank 模型，auto 模式走 remote")

    return _rerank_remote(query, passages, docs_scores, top_n=keep)


def _rerank_local(
    query: str,
    passages: list[str],
    docs_scores: list[tuple[Document, float]],
    top_n: int,
) -> Optional[list[tuple[Document, float]]]:
    global _local_reranker, _local_reranker_checked

    try:
        if _local_reranker is None and not _local_reranker_checked:
            _local_reranker_checked = True
            local_path = _has_local_reranker_model()
            if local_path is None:
                logger.info("[rerank] 未找到本地 bge-reranker-v2-m3 模型，跳过 local")
                return None
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


def _rerank_remote(
    query: str,
    passages: list[str],
    docs_scores: list[tuple[Document, float]],
    top_n: int,
) -> Optional[list[tuple[Document, float]]]:
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
                logger.warning(f"[rerank] remote HTTP {resp.status_code} 失败：{err_body}，降级")
                return None
            data = resp.json()
    except httpx.TimeoutException:
        logger.warning(f"[rerank] remote 超时（>{RERANK_TIMEOUT}s），降级")
        return None
    except Exception as e:
        logger.warning(f"[rerank] remote 请求异常：{e}，降级")
        return None

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
