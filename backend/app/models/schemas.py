from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field


# ==============================
# 数据模型（Pydantic Schema）
# 接口请求 / 响应的数据结构
# ==============================

# ---------- 文件上传 / 索引 ----------
class IndexFileResponse(BaseModel):
    """文件上传响应（仅解析，未分块）"""
    file_id: str = Field(description="文件唯一ID")
    file_name: str = Field(description="原始文件名")
    chunks_count: int = Field(default=0, description="切分块数（上传时为0）")
    status: str = Field(default="parsed", description="状态: parsed/success")
    message: Optional[str] = Field(default=None, description="提示信息")


class ChunkRequest(BaseModel):
    """分块请求"""
    chunk_method: str = Field(default="recursive", description="分块方式: recursive/intelligent/table/parent_child")


class ChunkResponse(BaseModel):
    """分块入库响应"""
    file_id: str = Field(description="文件唯一ID")
    file_name: str = Field(description="原始文件名")
    chunks_count: int = Field(description="切分块数")
    chunk_method: str = Field(description="使用的分块方式")
    status: str = Field(default="success", description="分块结果")


class ChunkMethodItem(BaseModel):
    """分块方式信息"""
    value: str = Field(description="方式标识")
    label: str = Field(description="显示名称")
    description: str = Field(description="方式说明")
    scenario: str = Field(description="适用场景")


class IndexStatusResponse(BaseModel):
    """索引任务状态"""
    file_id: str
    file_name: str
    status: str  # pending / parsed / indexing / done / failed
    progress: float = 0.0
    chunks_count: int = 0
    error: Optional[str] = None
    chunk_method: Optional[str] = None
    file_size: int = 0
    file_ext: str = ""


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
    enable_deep_think: bool = Field(default=False, description="是否启用深度思考模式")
    model: Optional[str] = Field(default=None, description="指定模型名（如 glm-4-flash、glm-4.5-flash），不传则用默认模型")


class ChatModel(BaseModel):
    """可用模型列表项"""
    id: str = Field(description="模型 ID，用于请求体 model 字段")
    name: str = Field(description="显示名称")
    provider: str = Field(description="提供商")
    default: bool = Field(default=False, description="是否默认模型")
    supports_deep_think: bool = Field(default=False, description="是否支持深度思考（reasoning_content）")


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
    metadata: Optional[dict[str, Any]] = None


# ---------- 通用 ----------
class PongResponse(BaseModel):
    status: str = "ok"
