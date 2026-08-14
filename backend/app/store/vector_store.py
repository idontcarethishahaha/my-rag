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
