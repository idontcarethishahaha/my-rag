"""
对话 / RAG 问答路由
  POST   /api/chat              非流式回答（简单调试用）
  POST   /api/chat/stream       SSE 流式回答（推荐）
  GET    /api/models            可用模型列表
  GET    /api/conversations     会话列表
  DELETE /api/conversations/{id}  清空单个会话
"""
from __future__ import annotations
import uuid
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..services import rag_service, memory_service, generator_service
from ..models.schemas import ChatRequest, ChatResponse, ConversationInfo, PongResponse, MessageItem, ChatModel

router = APIRouter(prefix="/api", tags=["对话 / RAG 问答"])


# -------- 健康检查 --------
@router.get("/ping", response_model=PongResponse)
def ping():
    return PongResponse()


# -------- 可用模型列表（从 provider_service 动态读取）--------
@router.get("/models", response_model=list[ChatModel])
def list_models():
    models = generator_service.get_available_models()
    return [ChatModel(**m) for m in models]


# -------- 非流式（调试用）--------
@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    try:
        answer, thinking_text, sources, _debug_info = rag_service.ask_rag(
            question=req.question,
            session_id=req.session_id,
            top_k=req.top_k,
            enable_deep_think=req.enable_deep_think,
            model=req.model,
        )
        return ChatResponse(
            answer=answer,
            sources=sources,
            session_id=req.session_id,
            usage={"thinking_chars": len(thinking_text or "")},
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# -------- 流式（前端默认走这个）--------
@router.post("/chat/stream")
def chat_stream(req: ChatRequest):
    """
    Server-Sent Events (SSE) 流式输出。
    前端用 EventSource 或 fetch + ReadableStream 消费。

    事件（event 字段）：
      - source         引用来源 chunks
      - thinking       进入思考阶段（前端展示 loading 点点点）
      - thinking_token 思考过程文本增量（可折叠块里的内容）
      - thinking_done  思考阶段结束
      - token          正式回答文本增量
      - done           全部结束
      - error          异常信息
    """
    generator = rag_service.ask_rag_stream(
        question=req.question,
        session_id=req.session_id,
        top_k=req.top_k,
        enable_deep_think=req.enable_deep_think,
        model=req.model,
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
    items = mm.list_sessions()
    return [
        ConversationInfo(
            session_id=item["session_id"],
            title=item["title"],
            created_at=item["created_at"],
            last_message=item.get("last_message"),
        )
        for item in items
    ]


@router.delete("/conversations/{session_id}")
def clear_conversation(session_id: str):
    mm = memory_service.get_memory_manager()
    mm.clear(session_id)
    return {"status": "ok"}


# -------- 获取单个会话的消息历史 --------
@router.get("/conversations/{session_id}/messages", response_model=list[MessageItem])
def get_conversation_messages(session_id: str):
    mm = memory_service.get_memory_manager()
    msgs = mm.get_messages(session_id)
    # role: assistant -> 前端用的是 "ai"
    return [
        MessageItem(
            role="ai" if m.role == "assistant" else m.role,
            content=m.content,
            metadata=m.metadata,
        )
        for m in msgs
    ]


# -------- 工具：创建新会话 ID --------
@router.post("/conversations/new")
def new_conversation():
    mm = memory_service.get_memory_manager()
    return mm.create_session(title="新对话")
