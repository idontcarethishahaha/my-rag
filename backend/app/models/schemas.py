from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field


# ==============================
# 数据模型（Pydantic Schema）
# 接口请求 / 响应的数据结构
# ==============================

# ---------- 文件上传 / 索引 ----------
class IndexFileResponse(BaseModel):
    """文件上传入库响应"""
    file_id: str = Field(description="文件唯一ID")
    file_name: str = Field(description="原始文件名")
    chunks_count: int = Field(description="切分块数")
    status: str = Field(default="success", description="索引结果")
    message: Optional[str] = Field(default=None, description="提示信息")


class IndexStatusResponse(BaseModel):
    """索引任务状态"""
    file_id: str
    file_name: str
    status: str  # pending / indexing / done / failed
    progress: float = 0.0
    chunks_count: int = 0
    error: Optional[str] = None


class DocumentChunk(BaseModel):
    """单个检索结果（文本块 + 来源 + 分数）"""
    chunk_id: str
    content: str = Field(description="文本块内容")
    source_file: str = Field(description="来源文件名")
    page: Optional[int] = Field(default=None, description="页码（仅PDF/Word）")
    score: float = Field(description="相似度得分 0-1，越大越相关")
    metadata: dict[str, Any] = Field(default_factory=dict, description="扩展信息")


# ---------- 对话 / RAG 问答 ----------
class ChatRequest(BaseModel):
    """用户提问请求"""
    question: str = Field(description="用户问题")
    session_id: str = Field(description="会话ID，用于多轮对话记忆")
    stream: bool = Field(default=True, description="是否流式返回")
    top_k: Optional[int] = Field(default=None, description="覆盖默认 Top-K")


class ChatResponse(BaseModel):
    """非流式回答响应"""
    answer: str = Field(description="LLM生成的回答")
    sources: list[DocumentChunk] = Field(default_factory=list, description="引用来源")
    session_id: str
    usage: dict[str, int] = Field(default_factory=dict, description="Token消耗（可选）")


class ChatEvent(BaseModel):
    """SSE 流式事件"""
    event: str  # thinking / token / source / done / error
    data: Any = None


# ---------- 会话管理 ----------
class ConversationInfo(BaseModel):
    session_id: str
    title: str
    created_at: str
    last_message: Optional[str] = None


class MessageItem(BaseModel):
    role: str   # user / assistant
    content: str


# ---------- 通用 ----------
class PongResponse(BaseModel):
    status: str = "ok"
