"""
统一配置管理 —— 用 dotenv + os.getenv 直接读 .env 文件。

设计原则：
  - 跟其他项目一致：load_dotenv() + os.getenv()
  - 不强制写 provider：默认走 OpenAI 兼容协议（智谱/通义/SiliconFlow 都是）
  - 嵌入模型独立配置，未设置则回退到主模型配置
  - 自动去除 URL 上意外的反引号
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# 加载 .env（从项目根目录或 backend 目录查找）
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)


def _clean_url(v: str | None) -> str | None:
    """去除 URL 上意外的反引号 / 前后空格"""
    if not v:
        return v
    return v.strip().strip("`").strip()


# ==================================
# 服务端口
# ==================================
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))

# ==================================
# 主模型（LLM）配置
# ==================================
API_KEY = os.getenv("OPENAI_API_KEY", "")
BASE_URL = _clean_url(os.getenv("OPENAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"))
MODEL_ID = os.getenv("MODEL_NAME", "glm-4.5-flash")

# ==================================
# Embedding 模型（独立配置，未设置则回退到主模型）
# ==================================
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", API_KEY)
EMBEDDING_BASE_URL = _clean_url(os.getenv("EMBEDDING_BASE_URL", BASE_URL))
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
EMBEDDING_DIMENSION = int(os.getenv("EMBEDDING_DIMENSION", "0"))

# ==================================
# 向量数据库
# ==================================
VECTOR_DB_TYPE = os.getenv("VECTOR_DB_TYPE", "chroma")
VECTOR_DB_PATH = os.getenv("VECTOR_DB_PATH", "./vector_db")
VECTOR_DB_COLLECTION = os.getenv("VECTOR_DB_COLLECTION", "my_rag_knowledge")
VECTOR_DB_HOST = os.getenv("VECTOR_DB_HOST", "localhost")
VECTOR_DB_PORT = int(os.getenv("VECTOR_DB_PORT", "6333"))

# ==================================
# RAG 超参数
# ==================================
RAG_CHUNK_SIZE = int(os.getenv("RAG_CHUNK_SIZE", "500"))
RAG_CHUNK_OVERLAP = int(os.getenv("RAG_CHUNK_OVERLAP", "80"))
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "6"))
RAG_SIMILARITY_THRESHOLD = float(os.getenv("RAG_SIMILARITY_THRESHOLD", "0.3"))
RAG_ENABLE_RERANK = os.getenv("RAG_ENABLE_RERANK", "false").lower() == "true"
RAG_RERANK_TOP_N = int(os.getenv("RAG_RERANK_TOP_N", "3"))
RAG_MAX_TOKENS_LIMIT = int(os.getenv("RAG_MAX_TOKENS_LIMIT", "8000"))
RAG_SUMMARY_THRESHOLD = int(os.getenv("RAG_SUMMARY_THRESHOLD", "4000"))

# ==================================
# 路径
# ==================================
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "./data/uploads")

# ==================================
# LangSmith（可选）
# ==================================
LANGCHAIN_TRACING_V2 = os.getenv("LANGCHAIN_TRACING_V2", "false").lower() == "true"
LANGSMITH_API_KEY = os.getenv("LANGSMITH_API_KEY", "")
LANGSMITH_ENDPOINT = _clean_url(os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"))
LANGSMITH_PROJECT = os.getenv("LANGSMITH_PROJECT", "my-rag")

# ==================================
# Tavily（可选）
# ==================================
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")


def reload():
    """重新加载环境变量（修改 .env 后调用）"""
    load_dotenv(_env_path, override=True)
    global API_KEY, BASE_URL, MODEL_ID
    global EMBEDDING_API_KEY, EMBEDDING_BASE_URL, EMBEDDING_MODEL, EMBEDDING_DIMENSION
    API_KEY = os.getenv("OPENAI_API_KEY", "")
    BASE_URL = _clean_url(os.getenv("OPENAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"))
    MODEL_ID = os.getenv("MODEL_NAME", "glm-4.5-flash")
    EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", API_KEY)
    EMBEDDING_BASE_URL = _clean_url(os.getenv("EMBEDDING_BASE_URL", BASE_URL))
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    EMBEDDING_DIMENSION = int(os.getenv("EMBEDDING_DIMENSION", "0"))
