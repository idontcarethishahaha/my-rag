"""
文件索引路由（知识库管理）—— 两段式设计
  POST   /api/index/upload              上传文件 + 解析（不分块）
  POST   /api/index/{file_id}/chunk     按指定方式分块 + 入库
  GET    /api/index/methods             获取可用分块方式列表
  GET    /api/index/status              列出所有文件状态
  GET    /api/index/status/{file_id}    单个文件状态
  DELETE /api/index/{file_id}           删除一个文件的索引
"""
from __future__ import annotations
import threading
import traceback
from fastapi import APIRouter, UploadFile, File, HTTPException
from pydantic import BaseModel

from ..services import indexer_service
from ..models.schemas import (
    IndexFileResponse, IndexStatusResponse,
    ChunkResponse, ChunkMethodItem,
)

router = APIRouter(prefix="/api/index", tags=["知识库索引"])


class ChunkBody(BaseModel):
    """分块请求体"""
    chunk_method: str = "recursive"


@router.post("/upload", response_model=IndexFileResponse)
async def upload_and_parse(file: UploadFile = File(..., description="支持 PDF/Word/Excel/MD/TXT/HTML/CSV")):
    """第一步：上传文件 + 解析内容（不分块、不入库）"""
    try:
        content = await file.read()
        result = indexer_service.upload_and_parse(content, file.filename)
        return IndexFileResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"上传解析失败: {e}")


@router.post("/{file_id}/chunk")
def chunk_and_store(file_id: str, body: ChunkBody):
    """第二步：按指定方式分块 + 嵌入 + 入库（后台线程执行，立即返回）"""
    # 先校验是否可分块
    st = indexer_service.get_status(file_id)
    if not st:
        raise HTTPException(status_code=404, detail=f"未找到 file_id={file_id}")
    if st["status"] == "done":
        raise HTTPException(status_code=400, detail="文件已分块入库，如需重新分块请先删除后重新上传")
    if st["status"] == "indexing":
        raise HTTPException(status_code=400, detail="文件正在分块中，请勿重复提交")

    # 后台线程执行分块
    def _run():
        try:
            indexer_service.chunk_and_store(file_id, body.chunk_method)
        except Exception as e:
            traceback.print_exc()

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    return {
        "file_id": file_id,
        "file_name": st["file_name"],
        "status": "indexing",
        "message": "分块任务已启动，请轮询进度",
    }


@router.get("/methods", response_model=list[ChunkMethodItem])
def get_chunk_methods():
    """获取可用分块方式列表"""
    return [ChunkMethodItem(**m) for m in indexer_service.get_chunk_methods()]


@router.get("/status", response_model=list[IndexStatusResponse])
def list_indexed_files():
    return [IndexStatusResponse(**item) for item in indexer_service.list_all_files()]


@router.get("/status/{file_id}", response_model=IndexStatusResponse)
def get_file_status(file_id: str):
    st = indexer_service.get_status(file_id)
    if not st:
        raise HTTPException(status_code=404, detail=f"未找到 file_id={file_id}")
    return IndexStatusResponse(**st)


@router.delete("/{file_id}")
def delete_file_index(file_id: str):
    try:
        indexer_service.delete_file_index(file_id)
        return {"status": "ok", "message": "已删除索引"}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"删除失败: {e}")
