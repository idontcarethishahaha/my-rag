"""
对话 / RAG 问答路由
  POST   /api/chat              非流式回答（简单调试用）
  POST   /api/chat/stream       SSE 流式回答（推荐，纯文本）
  POST   /api/chat/image-stream SSE 流式回答（图片 + 文字，视觉模型）
  GET    /api/models            可用模型列表
  GET    /api/conversations     会话列表
  DELETE /api/conversations/{id}  清空单个会话
  GET    /api/conversations/{id}/messages  获取会话消息历史
"""
from __future__ import annotations
import json
import logging
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from ..config import UPLOAD_DIR, VL_MAX_IMAGE_MB, VL_MAX_IMAGES, VL_MODEL, VL_ENABLE
from ..services import rag_service, memory_service, generator_service
from ..services.vision_service import (
    build_vl_messages,
    chat_with_images,
    image_to_base64_dataurl,
    preprocess_image,
)
from ..models.schemas import ChatRequest, ChatResponse, ConversationInfo, PongResponse, MessageItem, ChatModel

logger = logging.getLogger(__name__)

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


@router.get("/vl-config")
def get_vl_config():
    """返回视觉模型（VL）配置，供前端显示图片模式下的模型信息"""
    return {
        "enabled": VL_ENABLE,
        "model": VL_MODEL,
        "display_name": f"视觉模型 · {VL_MODEL}",
    }


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


# -------- 图片 + 文字 多模态流式对话 --------
@router.post("/chat/image-stream")
async def chat_image_stream(
    session_id: str = Form(..., min_length=1, max_length=128),
    question: str = Form(""),
    enable_deep_think: bool = Form(False),
    files: list[UploadFile] = File(..., description="图片附件，支持 jpg/png/webp/tiff/bmp"),
):
    """
    多模态对话：图片 + 文字 → 视觉模型流式回答。
    - 纯图片：question 留空，发送图片即可
    - 图片 + 文字：question 写问题，同时发送图片
    - 返回 SSE 事件流，与 /chat/stream 格式一致
    """
    logger.info(
        "[image-stream] === 收到请求 === session=%s, question=%r, deep_think=%s, files=%d",
        session_id, question, enable_deep_think, len(files) if files else 0,
    )

    if not files:
        logger.warning("[image-stream] 没有上传文件，返回 400")
        raise HTTPException(status_code=400, detail="请至少上传 1 张图片")
    if len(files) > VL_MAX_IMAGES:
        raise HTTPException(status_code=400, detail=f"单次最多上传 {VL_MAX_IMAGES} 张图片")

    # 图片格式白名单
    _EXT_WHITELIST = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
    _MIME_WHITELIST = {
        "image/jpeg", "image/jpg", "image/png",
        "image/tiff", "image/tif", "image/bmp", "image/webp",
    }

    # 逐个校验 + 预处理
    image_dataurls: list[str] = []
    image_server_urls: list[str] = []

    for idx, uf in enumerate(files):
        raw_bytes = await uf.read()
        if not raw_bytes:
            raise HTTPException(status_code=400, detail=f"第 {idx + 1} 张图片是空文件")

        if len(raw_bytes) > VL_MAX_IMAGE_MB * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"图片大小超过 {VL_MAX_IMAGE_MB}MB 限制")

        # 格式校验
        ext = Path(uf.filename or "").suffix.lower()
        mime = (uf.content_type or "").lower()
        if ext not in _EXT_WHITELIST and mime not in _MIME_WHITELIST:
            logger.warning(
                "[image-stream] 图片 #%s 格式不在白名单：filename=%r ext=%r mime=%r，尝试 Pillow 解码",
                idx + 1, uf.filename, ext, mime,
            )

        # 预处理为 base64 data URL（关键：不依赖 localhost 路径）
        try:
            data_url = image_to_base64_dataurl(raw_bytes)
        except ModuleNotFoundError as e:
            raise HTTPException(
                status_code=503,
                detail=f"缺少图片处理依赖：{e}。请执行 pip install Pillow"
            )
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"图片 '{uf.filename or ''}' 预处理失败（格式不支持或损坏）：{e}"
            )

        # 落盘保存（供前端气泡预览）
        try:
            abs_upload = Path(UPLOAD_DIR).resolve()
            save_dir = abs_upload / "_chat_images" / (session_id or "default")
            save_dir.mkdir(parents=True, exist_ok=True)
            saved_name = f"{uuid.uuid4().hex}.jpg"
            saved_path = save_dir / saved_name
            jpg_bytes = preprocess_image(raw_bytes)
            with open(saved_path, "wb") as f:
                f.write(jpg_bytes)
            image_server_urls.append(f"/uploads/_chat_images/{session_id}/{saved_name}")
        except Exception as e:
            logger.warning("[image-stream] 图片落盘失败（不影响视觉回答）：%s", e)

        image_dataurls.append(data_url)

    logger.info(
        "[image-stream] 图片校验通过：n=%s, session=%s, question=%r",
        len(image_dataurls), session_id, question,
    )

    # 获取历史对话（用于多轮上下文）
    memory = memory_service.get_memory_manager()
    history_raw = memory.get_messages(session_id, last_n=4)
    history = []
    for m in history_raw:
        if m.role in ("user", "assistant"):
            content = m.content
            if m.metadata and m.metadata.get("image_urls"):
                content = m.metadata.get("text", content)
            history.append({"role": m.role, "content": content})

    # 构建视觉模型 messages
    vl_messages = build_vl_messages(question, image_dataurls, history=history)

    # 记录用户消息到记忆（只写 user；assistant 在流式结束后写入，避免空消息）
    user_metadata = {
        "image_urls": image_server_urls,
        "text": question,
    }
    memory.append_user_message(session_id, question or "", user_metadata=user_metadata)

    async def sse_wrap_vl():
        full_answer = []
        full_thinking = []
        thinking_finished = False

        logger.info("[image-stream] 开始 SSE 流，推送 image_events 和 thinking")
        # 先推送一个 image_events 事件，包含图片静态 URL（前端气泡渲染用）
        yield (
            f"event: image_events\ndata: "
            f"{json.dumps({'image_urls': image_server_urls}, ensure_ascii=False)}\n\n"
        )
        yield f"event: thinking\ndata: {json.dumps(None)}\n\n"

        try:
            from app.services.vision_service import _vl_stream_async, _normalize_messages, _check_enabled
            logger.info("[image-stream] 检查视觉模型配置")
            _check_enabled()
            norm_messages = _normalize_messages(vl_messages)
            logger.info("[image-stream] 开始流式调用视觉模型，messages count=%d", len(norm_messages))
            
            token_count = 0
            async for ttype, token in _vl_stream_async(norm_messages, enable_deep_think=enable_deep_think):
                token_count += 1
                if token_count <= 5 or token_count % 50 == 0:
                    logger.info("[image-stream] token #%d: type=%s, content=%s", token_count, ttype, str(token)[:50])
                if ttype == "thinking":
                    full_thinking.append(token)
                    yield f"event: thinking_token\ndata: {json.dumps(token, ensure_ascii=False)}\n\n"
                elif ttype == "content":
                    if not thinking_finished:
                        thinking_finished = True
                        yield f"event: thinking_done\ndata: {json.dumps(None)}\n\n"
                    full_answer.append(token)
                    yield f"event: token\ndata: {json.dumps(token, ensure_ascii=False)}\n\n"

            if not thinking_finished and full_thinking:
                logger.info("[image-stream] 补充发送 thinking_done 事件")
                yield f"event: thinking_done\ndata: {json.dumps(None)}\n\n"

            # 写回助手回答到记忆（只写 assistant，不再产生空 user 消息）
            answer = "".join(full_answer)
            logger.info("[image-stream] 流式完成，thinking=%d tokens, answer=%d chars", len(full_thinking), len(answer))
            if answer:
                memory.append_assistant_message(session_id, answer)

            logger.info("[image-stream] 发送 done 事件")
            yield f"event: done\ndata: {json.dumps(None)}\n\n"
        except Exception as e:
            logger.exception("[image-stream] 流式异常")
            yield f"event: error\ndata: {json.dumps(str(e), ensure_ascii=False)}\n\n"

    return StreamingResponse(
        sse_wrap_vl(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
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
            images=m.image_urls if m.image_urls else None,
        )
        for m in msgs
    ]


# -------- 工具：创建新会话 ID --------
@router.post("/conversations/new")
def new_conversation():
    mm = memory_service.get_memory_manager()
    return mm.create_session(title="新对话")
