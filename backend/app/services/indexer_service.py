"""
索引流水线服务（Offline / Indexing Pipeline）

参考 RAG-Pro 的两段式设计：
  上传+解析（upload_and_parse）和 分块+入库（chunk_and_store）分离
  用户上传文件后，在前端选择分块方式，再执行分块入库

对外暴露：
  upload_and_parse(file_bytes, filename)  → 上传文件 + 解析内容（不分块）
  chunk_and_store(file_id, chunk_method)  → 按指定方式分块 + 嵌入 + 入库
  get_chunk_methods()                     → 获取可用分块方式列表
  delete_file_index(file_id)              → 删除索引
  get_status(file_id)                     → 查询状态
"""
from __future__ import annotations
import logging
import os
import re
import uuid
from dataclasses import dataclass

from langchain_core.documents import Document as LCDocument

from ..config import (
    UPLOAD_DIR,
    RAG_CHUNK_SIZE,
    RAG_CHUNK_OVERLAP,
    VECTOR_DB_TYPE,
    VECTOR_DB_PATH,
)
from ..loaders.document_loader import load_document, SUPPORTED_EXTS, get_ext
from ..splitters.text_splitter import (
    split_documents,
    get_chunker,
    _text_chunks_to_langchain,
    ChunkMethod,
)
from ..store.vector_store import get_vector_store, delete_by_file

logger = logging.getLogger(__name__)


# ==================================
# 分块方式信息（供前端展示）
# ==================================
CHUNK_METHODS_INFO = [
    {
        "value": "recursive",
        "label": "递归分块",
        "description": "按段落→换行→句末→逗号→空格递归切分，带重叠",
        "scenario": "通用文本（默认）",
    },
    {
        "value": "intelligent",
        "label": "智能分块",
        "description": "识别标题/章节边界，小段自动合并，超长段退回递归",
        "scenario": "有章节结构的文档（书籍/论文/规范）",
    },
    {
        "value": "table",
        "label": "表格分块",
        "description": "CSV 每行带表头列名，Markdown 表格整张保留",
        "scenario": "CSV / Excel / 含表格的文档",
    },
    {
        "value": "parent_child",
        "label": "父子分块",
        "description": "两层分块：大父块(1536t)→小子块(512t)，检索子块、上下文用父块",
        "scenario": "对上下文精度要求高的文档",
    },
]


@dataclass
class IndexProgress:
    """文件索引进度"""
    status: str = "pending"   # pending / parsed / indexing / done / failed
    progress: float = 0.0
    chunks_count: int = 0
    error: str | None = None
    file_name: str = ""
    file_size: int = 0        # 字节数
    file_ext: str = ""        # 扩展名（含点，如 .pdf）
    chunk_method: str | None = None  # 使用的分块方式


_progress_store: dict[str, IndexProgress] = {}  # file_id -> progress
_parsed_docs: dict[str, list[LCDocument]] = {}  # file_id -> 解析后的文档（内存缓存）


# ==================================
# 启动时从磁盘恢复已上传文件列表
# ==================================

_FILE_ID_PATTERN = re.compile(r"^([0-9a-f]{12})_(.+)$")


def _restore_from_disk() -> None:
    """从 uploads 目录扫描已上传的文件，恢复到内存列表，清理孤立 chunk"""
    if not os.path.isdir(UPLOAD_DIR):
        return
    vs = get_vector_store()
    all_data = vs.get() if vs else {}
    chunk_counts: dict[str, int] = {}
    all_vdb_file_ids: set[str] = set()
    for meta in all_data.get("metadatas", []) or []:
        fid = meta.get("file_id")
        if fid:
            chunk_counts[fid] = chunk_counts.get(fid, 0) + 1
            all_vdb_file_ids.add(fid)

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
            # 有 chunk → done；没 chunk → parsed（待分块）
            _progress_store[file_id] = IndexProgress(
                file_name=original_name,
                status="done" if actual_chunks > 0 else "parsed",
                progress=1.0 if actual_chunks > 0 else 0.0,
                file_size=file_size,
                file_ext=os.path.splitext(original_name)[1].lower(),
                chunks_count=actual_chunks,
            )

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

def get_chunk_methods() -> list[dict]:
    """返回可用分块方式列表"""
    return CHUNK_METHODS_INFO


def upload_and_parse(file_bytes: bytes, filename: str) -> dict:
    """
    第一步：上传文件 + 解析内容（不分块、不入库）
    返回 {file_id, file_name, status: "parsed", chunks_count: 0}
    """
    ext = get_ext(filename)
    if ext not in SUPPORTED_EXTS:
        raise ValueError(f"不支持的文件类型: {ext}，支持列表: {SUPPORTED_EXTS}")

    file_id = uuid.uuid4().hex[:12]
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    save_path = os.path.join(UPLOAD_DIR, f"{file_id}_{filename}")

    # 1) 保存到磁盘
    with open(save_path, "wb") as f:
        f.write(file_bytes)

    try:
        # 2) Load（解析文档）
        docs = load_document(save_path, file_name=filename)
        if not docs:
            raise RuntimeError("文件内容为空或无法解析")

        # 缓存解析结果到内存
        _parsed_docs[file_id] = docs

        # 记录进度
        _progress_store[file_id] = IndexProgress(
            file_name=filename, status="parsed",
            file_size=len(file_bytes),
            file_ext=ext,
        )

        return {
            "file_id": file_id,
            "file_name": filename,
            "status": "parsed",
            "chunks_count": 0,
            "message": "解析成功，请选择分块方式后执行分块",
        }

    except Exception as e:
        prog = _progress_store.get(file_id, IndexProgress(file_name=filename))
        prog.status = "failed"
        prog.error = str(e)
        if os.path.exists(save_path):
            try:
                os.remove(save_path)
            except Exception:
                pass
        raise


def chunk_and_store(file_id: str, chunk_method: str = "recursive") -> dict:
    """
    第二步：按指定方式分块 + 嵌入 + 入库
    返回 {file_id, file_name, chunks_count, chunk_method, status: "success"}
    """
    if file_id not in _progress_store:
        raise ValueError(f"未找到 file_id={file_id}，请先上传文件")

    prog = _progress_store[file_id]
    if prog.status == "done":
        raise ValueError(f"文件已分块入库，如需重新分块请先删除后重新上传")

    # 校验分块方法
    valid_methods = [m["value"] for m in CHUNK_METHODS_INFO]
    if chunk_method not in valid_methods:
        raise ValueError(f"不支持的分块方式: {chunk_method}，可选: {valid_methods}")

    # 更新状态
    prog.status = "indexing"
    prog.error = None

    try:
        # 获取解析后的文档：优先从内存取，没有就从磁盘重新解析
        docs = _parsed_docs.get(file_id)
        if not docs:
            # 从磁盘找文件并重新解析
            save_path = None
            for fn in os.listdir(UPLOAD_DIR):
                if fn.startswith(f"{file_id}_"):
                    save_path = os.path.join(UPLOAD_DIR, fn)
                    break
            if not save_path or not os.path.exists(save_path):
                raise FileNotFoundError(f"磁盘上未找到 file_id={file_id} 的文件")
            docs = load_document(save_path, file_name=prog.file_name)
            _parsed_docs[file_id] = docs

        if not docs:
            raise RuntimeError("文件内容为空")

        method: ChunkMethod = chunk_method  # type: ignore[assignment]
        token_size = max(256, RAG_CHUNK_SIZE // 2)
        token_overlap = max(32, RAG_CHUNK_OVERLAP // 2)

        # parent_child 走专门路径
        if method == "parent_child":
            pages = [{
                "text": d.page_content or "",
                "page_number": d.metadata.get("page"),
                "section_title": d.metadata.get("section_title"),
                "metadata": dict(d.metadata),
            } for d in docs]
            chunker = get_chunker("parent_child", token_size, token_overlap)
            child_chunks, _parent_chunks = chunker.chunk_pages(pages)  # type: ignore[misc]
            chunks = _text_chunks_to_langchain(child_chunks)
        else:
            chunks = split_documents(
                docs,
                chunk_size=RAG_CHUNK_SIZE,
                chunk_overlap=RAG_CHUNK_OVERLAP,
                method=method,
            )

        if not chunks:
            raise RuntimeError("分块结果为空")

        # 打元数据
        for ch in chunks:
            ch.metadata["file_id"] = file_id
            ch.metadata.setdefault("source", prog.file_name)
            ch.metadata["chunk_method"] = method

        # 非 parent_child：相邻 3 块拼接注入 parent_content
        if method != "parent_child":
            for i, ch in enumerate(chunks):
                parts = []
                if i > 0:
                    parts.append(chunks[i - 1].page_content)
                parts.append(ch.page_content)
                if i < len(chunks) - 1:
                    parts.append(chunks[i + 1].page_content)
                ch.metadata["parent_content"] = "\n\n".join(parts)

        # Embed + Store
        store = get_vector_store()
        store.add_documents(chunks)

        if VECTOR_DB_TYPE == "faiss":
            save_path_faiss = f"{VECTOR_DB_PATH}/faiss_index"
            store.save_local(save_path_faiss)

        # 更新进度
        prog.status = "done"
        prog.progress = 1.0
        prog.chunks_count = len(chunks)
        prog.chunk_method = method

        # 清理内存缓存
        _parsed_docs.pop(file_id, None)

        return {
            "file_id": file_id,
            "file_name": prog.file_name,
            "chunks_count": len(chunks),
            "chunk_method": method,
            "status": "success",
        }

    except Exception as e:
        prog = _progress_store.get(file_id, IndexProgress())
        prog.status = "failed"
        prog.error = str(e)
        raise


def delete_file_index(file_id: str) -> None:
    """删除一个文件的所有索引块（向量库 + 磁盘 + 内存）"""
    deleted_count = delete_by_file(file_id)
    logger.info(f"[delete] file_id={file_id}: 从向量库删除了 {deleted_count} 个 chunk")
    _progress_store.pop(file_id, None)
    _parsed_docs.pop(file_id, None)
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
        "chunk_method": prog.chunk_method,
    }


def list_all_files() -> list[dict]:
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
        "chunk_method": p.chunk_method,
    }
