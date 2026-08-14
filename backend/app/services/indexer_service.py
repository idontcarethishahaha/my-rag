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
import logging
import os
import re
import uuid
from dataclasses import dataclass

from ..config import UPLOAD_DIR, RAG_CHUNK_SIZE, RAG_CHUNK_OVERLAP, VECTOR_DB_TYPE, VECTOR_DB_PATH
from ..loaders.document_loader import load_document, SUPPORTED_EXTS, get_ext
from ..splitters.text_splitter import split_documents
from ..store.vector_store import get_vector_store, delete_by_file

logger = logging.getLogger(__name__)


@dataclass
class IndexProgress:
    """文件索引进度"""
    status: str = "pending"   # pending / indexing / done / failed
    progress: float = 0.0
    chunks_count: int = 0
    error: str | None = None
    file_name: str = ""
    file_size: int = 0        # 字节数
    file_ext: str = ""        # 扩展名（含点，如 .pdf）


_progress_store: dict[str, IndexProgress] = {}  # file_id -> progress


# ==================================
# 启动时从磁盘恢复已上传文件列表
# ==================================

# 文件命名规则: {file_id}_{filename}，file_id 是 12 位 hex
_FILE_ID_PATTERN = re.compile(r"^([0-9a-f]{12})_(.+)$")


def _restore_from_disk() -> None:
    """从 uploads 目录扫描已上传的文件，恢复到内存列表，清理孤立 chunk"""
    if not os.path.isdir(UPLOAD_DIR):
        return
    vs = get_vector_store()
    # 取向量库中所有 chunk 的 metadata
    all_data = vs.get() if vs else {}
    chunk_counts: dict[str, int] = {}
    all_vdb_file_ids: set[str] = set()
    for meta in all_data.get("metadatas", []) or []:
        fid = meta.get("file_id")
        if fid:
            chunk_counts[fid] = chunk_counts.get(fid, 0) + 1
            all_vdb_file_ids.add(fid)

    # 磁盘上存在的 file_id 集合
    disk_file_ids: set[str] = set()
    for fn in os.listdir(UPLOAD_DIR):
        m = _FILE_ID_PATTERN.match(fn)
        if not m:
            continue
        file_id = m.group(1)
        disk_file_ids.add(file_id)
        original_name = m.group(2)
        if file_id not in _progress_store:
            file_path = os.path.join(UPLOAD_DIR, fn)
            file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
            actual_chunks = chunk_counts.get(file_id, 0)
            _progress_store[file_id] = IndexProgress(
                file_name=original_name,
                status="done",
                progress=1.0,
                file_size=file_size,
                file_ext=os.path.splitext(original_name)[1].lower(),
                chunks_count=actual_chunks,
            )

    # 清理孤立 chunk：向量库中有，但磁盘上文件已不存在
    orphan_ids = all_vdb_file_ids - disk_file_ids
    for fid in orphan_ids:
        delete_by_file(fid)
        logger.info(f"[restore] 清理孤立 chunk: file_id={fid}")


def get_file_list() -> list[dict]:
    """获取当前所有文件的真实列表（含 chunk 数），供 AI 回答时参考"""
    result = []
    for fid, p in _progress_store.items():
        if p.status == "done":
            result.append({
                "file_id": fid,
                "file_name": p.file_name,
                "chunks_count": p.chunks_count,
                "file_size": p.file_size,
            })
    return result


# 模块加载时自动恢复
_restore_from_disk()


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
    _progress_store[file_id] = IndexProgress(
        file_name=filename, status="indexing",
        file_size=len(file_bytes),
        file_ext=ext,
    )

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
    """删除一个文件的所有索引块（向量库 + 磁盘 + 内存）"""
    # 1. 删除向量库中的 chunk
    deleted_count = delete_by_file(file_id)
    logger.info(f"[delete] file_id={file_id}: 从向量库删除了 {deleted_count} 个 chunk")
    
    # 2. 从内存中移除
    _progress_store.pop(file_id, None)
    
    # 3. 清理磁盘上传文件
    if os.path.isdir(UPLOAD_DIR):
        for fn in os.listdir(UPLOAD_DIR):
            if fn.startswith(f"{file_id}_"):
                try:
                    os.remove(os.path.join(UPLOAD_DIR, fn))
                    logger.info(f"[delete] file_id={file_id}: 删除磁盘文件 {fn}")
                except Exception as e:
                    logger.warning(f"[delete] file_id={file_id}: 删除磁盘文件失败 {fn}: {e}")


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
        "file_size": p.file_size,
        "file_ext": p.file_ext,
    }
