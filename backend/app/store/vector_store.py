"""
向量数据库封装（Store / Retrieve 阶段）
默认 ChromaDB（本地文件，零配置）。
"""
from __future__ import annotations
from typing import Optional

from langchain_core.documents import Document
from langchain_core.vectorstores import VectorStore

from ..config import VECTOR_DB_TYPE, VECTOR_DB_PATH, VECTOR_DB_COLLECTION, VECTOR_DB_HOST, VECTOR_DB_PORT
from ..embeddings.embed_factory import get_embeddings

# 模块级单例
_instance: VectorStore | None = None


def get_vector_store() -> VectorStore:
    """单例：根据配置创建并返回 VectorStore 实例"""
    global _instance
    if _instance is not None:
        return _instance

    db_type = VECTOR_DB_TYPE.lower()
    embeddings = get_embeddings()

    # ---------- ChromaDB（本地文件，零配置）----------
    if db_type == "chroma":
        from langchain_chroma import Chroma
        import chromadb

        client_settings = chromadb.Settings(
            anonymized_telemetry=False,
            is_persistent=True,
            persist_directory=VECTOR_DB_PATH,
        )
        _instance = Chroma(
            collection_name=VECTOR_DB_COLLECTION,
            embedding_function=embeddings,
            client_settings=client_settings,
            persist_directory=VECTOR_DB_PATH,
        )
        return _instance

    # ---------- Qdrant（Docker 启动）----------
    if db_type == "qdrant":
        from langchain_qdrant import QdrantVectorStore
        from qdrant_client import QdrantClient

        client = QdrantClient(host=VECTOR_DB_HOST, port=VECTOR_DB_PORT)
        _instance = QdrantVectorStore(
            client=client,
            collection_name=VECTOR_DB_COLLECTION,
            embedding=embeddings,
        )
        return _instance

    # ---------- FAISS（纯内存）----------
    if db_type == "faiss":
        from langchain_community.vectorstores import FAISS
        import os
        save_path = f"{VECTOR_DB_PATH}/faiss_index"

        if os.path.exists(save_path + ".faiss"):
            _instance = FAISS.load_local(save_path, embeddings, allow_dangerous_deserialization=True)
        else:
            _instance = FAISS.from_texts(["__init__"], embeddings)
            _instance.save_local(save_path)
        return _instance

    raise ValueError(f"未知向量库类型: {db_type}")


def similarity_search_with_score(
    query: str,
    k: int = 6,
    score_threshold: float = 0.0,
    filter: Optional[dict] = None,
) -> list[tuple[Document, float]]:
    """
    相似度检索 + 自动按阈值过滤。
    ChromaDB: L2 距离 → 转换为 1/(1+d) 的相似度。
    阈值默认 0.0（不过滤，最大化召回率）。
    """
    store = get_vector_store()
    results = store.similarity_search_with_score(query, k=k, filter=filter)

    db_type = VECTOR_DB_TYPE.lower()
    normalized: list[tuple[Document, float]] = []
    for doc, raw_score in results:
        if db_type in {"chroma", "faiss"}:
            sim = 1.0 / (1.0 + raw_score)
        else:
            sim = float(raw_score)
        # score_threshold <= 0 表示不过滤
        if score_threshold <= 0 or sim >= score_threshold:
            normalized.append((doc, sim))

    normalized.sort(key=lambda x: x[1], reverse=True)
    return normalized


def get_all_documents() -> list[tuple[str, Document]]:
    """
    获取向量库中所有文档（用于 BM25 索引构建）
    返回 [(doc_id, Document), ...]
    """
    store = get_vector_store()
    try:
        data = store.get()
        ids = data.get("ids", [])
        docs_data = data.get("documents", [])
        metadatas = data.get("metadatas", [])
        result = []
        for i, doc_id in enumerate(ids):
            text = docs_data[i] if i < len(docs_data) else ""
            meta = metadatas[i] if i < len(metadatas) else {}
            result.append((doc_id, Document(page_content=text, metadata=meta)))
        return result
    except Exception as e:
        import logging as _logging
        _logging.getLogger(__name__).error(f"[vector_store] get_all_documents 失败: {e}")
        return []


def hybrid_search(
    query: str,
    k: int = 20,
    score_threshold: float = 0.0,
    filter: Optional[dict] = None,
) -> list[tuple[Document, float]]:
    """
    Hybrid Search（单查询版，兼容旧接口）：稠密向量 + BM25 稀疏 → RRF 融合
    """
    return hybrid_search_multi([query], k=k, filter=filter)


def hybrid_search_multi(
    queries: list[str],
    hyde_doc: Optional[str] = None,
    k: int = 20,
    filter: Optional[dict] = None,
) -> list[tuple[Document, float]]:
    """
    多路 Hybrid Search：
    - queries: 多个改写后的查询（主查询 + 子查询），每个查询各做 dense + sparse 检索
    - hyde_doc: HyDE 假设答案文档（可选），作为额外一路 dense 检索
    - BM25 索引只构建一次，多查询复用
    - 所有路的结果用 RRF 融合

    返回 [(Document, rrf_score), ...] 按 RRF 分数降序
    """
    import logging as _logging
    _logger = _logging.getLogger(__name__)

    from ..utils.bm25 import BM25Index, rrf_fuse

    queries = [q for q in queries if q and q.strip()]
    if not queries:
        return []

    # 1) 每个查询各做一次 Dense 检索（HyDE 文档也做一次 dense）
    dense_routes: list[list[tuple[Document, float]]] = []
    for q in queries:
        dense_routes.append(
            similarity_search_with_score(q, k=k, score_threshold=0.0, filter=filter)
        )
    if hyde_doc:
        dense_routes.append(
            similarity_search_with_score(hyde_doc, k=k, score_threshold=0.0, filter=filter)
        )

    if not any(dense_routes):
        return []

    # 2) 获取所有文档构建 BM25 索引（只建一次）
    all_docs = get_all_documents()
    if len(all_docs) <= 1:
        # 文档太少，退化为多路 dense 结果直接合并（按首次出现顺序）
        seen: dict[str, tuple[Document, float]] = {}
        for route in dense_routes:
            for doc, score in route:
                if doc.page_content not in seen:
                    seen[doc.page_content] = (doc, score)
        merged = sorted(seen.values(), key=lambda x: x[1], reverse=True)
        return merged[:k]

    doc_texts = [d.page_content for _, d in all_docs]
    bm25 = BM25Index(doc_texts)

    # 3) content → all_docs 索引映射
    all_content_to_idx: dict[str, int] = {}
    for i, (_, doc) in enumerate(all_docs):
        all_content_to_idx[doc.page_content] = i

    # 4) 每一路转成 all_docs 索引，RRF 融合所有路
    all_routes_as_idx: list[list[tuple[int, float]]] = []

    # dense 各路
    for route in dense_routes:
        route_idx: list[tuple[int, float]] = []
        for doc, score in route:
            all_idx = all_content_to_idx.get(doc.page_content)
            if all_idx is not None:
                route_idx.append((all_idx, score))
        if route_idx:
            all_routes_as_idx.append(route_idx)

    # BM25 sparse 各路（每个查询一路）
    for q in queries:
        sparse_hits = bm25.search(q, top_k=k)
        if sparse_hits:
            all_routes_as_idx.append(sparse_hits)

    if not all_routes_as_idx:
        return []

    # 递归 RRF 融合所有路
    fused = all_routes_as_idx[0]
    for route in all_routes_as_idx[1:]:
        fused = rrf_fuse(fused, route, k=60)

    # 5) 转回 Document + rrf_score
    result: list[tuple[Document, float]] = []
    for all_idx, rrf_score in fused[:k]:
        _, doc = all_docs[all_idx]
        result.append((doc, rrf_score))

    _logger.info(
        f"[hybrid_multi] queries={len(queries)}, hyde={'yes' if hyde_doc else 'no'}, "
        f"routes={len(all_routes_as_idx)}, fused={len(result)}"
    )

    return result


def delete_by_file(file_id: str) -> int:
    """按 file_id 批量删除一个文件的所有块，返回删除的数量"""
    store = get_vector_store()
    try:
        result = store.get(where={"file_id": file_id})
        ids_to_delete = result.get("ids", [])
        if not ids_to_delete:
            return 0
        store.delete(ids=ids_to_delete)
        return len(ids_to_delete)
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"[vector_store] 删除文件失败 file_id={file_id}: {e}")
        raise
