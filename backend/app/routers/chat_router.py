"""
对话 / RAG 问答路由
  POST   /api/chat              非流式回答（简单调试用）
  POST   /api/chat/stream       SSE 流式回答（推荐）
  GET    /api/conversations     会话列表
  DELETE /api/conversations/{id}  清空单个会话
"""
from __future__ import annotations
import uuid
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..services import rag_service, memory_service
from ..models.schemas import ChatRequest, ChatResponse, ConversationInfo, PongResponse

router = APIRouter(prefix="/api", tags=["对话 / RAG 问答"])


# -------- 健康检查 --------
@router.get("/ping", response_model=PongResponse)
def ping():
    return PongResponse()


# -------- 非流式（调试用）--------
@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    try:
        answer, sources = rag_service.ask_rag(
            question=req.question,
            session_id=req.session_id,
            top_k=req.top_k,
        )
        return ChatResponse(
            answer=answer,
            sources=sources,
            session_id=req.session_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------- 流式（前端默认走这个）--------
@router.post("/chat/stream")
def chat_stream(req: ChatRequest):
    """
    Server-Sent Events (SSE) 流式输出。
    前端用 EventSource 或 fetch + ReadableStream 消费。
    """
    generator = rag_service.ask_rag_stream(
        question=req.question,
        session_id=req.session_id,
        top_k=req.top_k,
    )

    def sse_wrap():
        for event in generator:
            import json
            # SSE 格式：event: xxx\ndata: {...}\n\n
            yield f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"

    return StreamingResponse(
        sse_wrap(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 关闭 Nginx 缓冲
        },
    )


# -------- 会话管理 --------
@router.get("/conversations", response_model=list[ConversationInfo])
def list_conversations():
    mm = memory_service.get_memory_manager()
    from datetime import datetime
    return [
        ConversationInfo(
            session_id=item["session_id"],
            title=item["title"],
            created_at=datetime.now().isoformat(timespec="seconds"),
            last_message=item.get("last_message"),
        )
        for item in mm.list_sessions()
    ]


@router.delete("/conversations/{session_id}")
def clear_conversation(session_id: str):
    mm = memory_service.get_memory_manager()
    mm.clear(session_id)
    return {"status": "ok"}


# -------- 工具：创建新会话 ID --------
@router.post("/conversations/new")
def new_conversation():
    session_id = uuid.uuid4().hex
    return {"session_id": session_id, "title": "新对话"}
