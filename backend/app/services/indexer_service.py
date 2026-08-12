"""
索引流水线服务（Offline / Indexing Pipeline）
把 4 个阶段串起来：
    Load（读取文件）→ Split（切块）→ Embed（转向量）→ Store（存入向量库）

对外暴露：
  index_file(file_bytes, filename)  → 上传一个文件入库
  delete_file(file_id)              → 删除索引
  get_file_status(file_id)          → 查询状态
"""
from __future__ import annotations
import os
import uuid
import shutil
from dataclasses import dataclass, field

from ..config import UPLOAD_DIR, RAG_CHUNK_SIZE, RAG_CHUNK_OVERLAP, VECTOR_DB_TYPE, VECTOR_DB_PATH
from ..loaders.document_loader import load_document, SUPPORTED_EXTS, get_ext
from ..splitters.text_splitter import split_documents
from ..store.vector_store import get_vector_store, delete_by_file


@dataclass
class IndexProgress:
    """内存里记录文件索引进度（生产可换 Redis/DB）"""
    status: str = "pending"   # pending / indexing / done / failed
    progress: float = 0.0
    chunks_count: int = 0
    error: str | None = None
    file_name: str = ""


_progress_store: dict[str, IndexProgress] = {}  # file_id -> progress


# ==================================
# 公共 API
# ==================================

def index_uploaded_file(file_bytes: bytes, filename: str) -> dict:
    """
    接收前端上传的文件内容 + 文件名
    1. 保存到 uploads 目录
    2. 同步执行 Load → Split → Embed → Store
    3. 返回 {file_id, chunks_count, status}
    """
    save_dir = UPLOAD_DIR
    ext = get_ext(filename)
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"不支持的文件类型: {ext}，支持列表: {SUPPORTED_EXTS}")

    file_id = uuid.uuid4().hex[:12]
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, f"{file_id}_{filename}")

    # 1) 保存到磁盘
    with open(save_path, "wb") as f:
        f.write(file_bytes)

    # 记录进度
    _progress_store[file_id] = IndexProgress(file_name=filename, status="indexing")

    try:
        # 2) Load
        docs = load_document(save_path, file_name=filename)

        # 3) Split
        chunks = split_documents(
            docs,
            chunk_size=RAG_CHUNK_SIZE,
            chunk_overlap=RAG_CHUNK_OVERLAP,
        )

        if not chunks:
            raise RuntimeError("文件内容为空或无法解析")

        # 给每个块打元数据（file_id / source）
        for ch in chunks:
            ch.metadata["file_id"] = file_id
            ch.metadata.setdefault("source", filename)

        # 4) Embed + Store
        store = get_vector_store()
        store.add_documents(chunks)

        # 若用 FAISS，记得持久化
        if VECTOR_DB_TYPE == "faiss":
            save_path_faiss = f"{VECTOR_DB_PATH}/faiss_index"
            store.save_local(save_path_faiss)

        # 更新进度
        prog = _progress_store[file_id]
        prog.status = "done"
        prog.progress = 1.0
        prog.chunks_count = len(chunks)

        return {
            "file_id": file_id,
            "file_name": filename,
            "chunks_count": len(chunks),
            "status": "success",
        }

    except Exception as e:
        prog = _progress_store.get(file_id, IndexProgress(file_name=filename))
        prog.status = "failed"
        prog.error = str(e)
        # 失败清理：删除已存磁盘文件
        if os.path.exists(save_path):
            try:
                os.remove(save_path)
            except Exception:
                pass
        raise


def delete_file_index(file_id: str) -> None:
    """删除一个文件的所有索引块"""
    delete_by_file(file_id)
    _progress_store.pop(file_id, None)
    # 顺便清理磁盘上传文件
    for fn in os.listdir(UPLOAD_DIR):
        if fn.startswith(f"{file_id}_"):
            try:
                os.remove(os.path.join(UPLOAD_DIR, fn))
            except Exception:
                pass


def get_status(file_id: str) -> dict | None:
    prog = _progress_store.get(file_id)
    if not prog:
        return None
    return {
        "file_id": file_id,
        "file_name": prog.file_name,
        "status": prog.status,
        "progress": prog.progress,
        "chunks_count": prog.chunks_count,
        "error": prog.error,
    }


def list_all_files() -> list[dict]:
    """列出所有索引过的文件状态"""
    return [
        {"file_id": fid, **_to_dict(p)}
        for fid, p in _progress_store.items()
    ]


def _to_dict(p: IndexProgress) -> dict:
    return {
        "file_name": p.file_name,
        "status": p.status,
        "progress": p.progress,
        "chunks_count": p.chunks_count,
        "error": p.error,
    }
