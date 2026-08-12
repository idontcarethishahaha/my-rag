"""
文件索引路由（知识库管理）
  POST   /api/index/upload     上传文件入库
  GET    /api/index/status     列出所有文件状态
  GET    /api/index/status/{file_id}  单个文件状态
  DELETE /api/index/{file_id}  删除一个文件的索引
"""
from __future__ import annotations
import traceback
from fastapi import APIRouter, UploadFile, File, HTTPException

from ..services import indexer_service
from ..models.schemas import IndexFileResponse, IndexStatusResponse

router = APIRouter(prefix="/api/index", tags=["知识库索引"])


@router.post("/upload", response_model=IndexFileResponse)
async def upload_and_index(file: UploadFile = File(..., description="支持 PDF/Word/Excel/MD/TXT/HTML")):
    try:
        content = await file.read()
        result = indexer_service.index_uploaded_file(content, file.filename)
        return IndexFileResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"索引失败: {e}")


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
    indexer_service.delete_file_index(file_id)
    return {"status": "ok", "message": "已删除索引"}
