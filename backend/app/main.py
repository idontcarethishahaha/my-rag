"""
my-rag — 完整 RAG 系统 后端入口
启动：
  cd backend
  pip install -r requirements.txt
  uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import VECTOR_DB_TYPE, MODEL_ID
from .routers.index_router import router as index_router
from .routers.chat_router import router as chat_router
from .routers.provider_router import router as provider_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动前 & 停止后的生命周期钩子"""
    # 预热嵌入模型和向量库（避免首请求卡顿）
    try:
        from .embeddings.embed_factory import get_embeddings
        from .store.vector_store import get_vector_store
        get_embeddings()
        get_vector_store()
        print("[my-rag] 嵌入模型 + 向量库 初始化完成")
    except Exception as e:
        print(f"[my-rag] 预热失败（不影响启动，首次调用会懒加载）：{e}")

    # 初始化 Provider 配置（首次启动从 .env 种子化）
    try:
        from .services.provider_service import _load
        providers = _load()
        print(f"[my-rag] LLM Provider 配置已加载：{len(providers)} 个")
    except Exception as e:
        print(f"[my-rag] Provider 配置加载失败：{e}")

    yield

    print("[my-rag] 服务已停止")


app = FastAPI(
    title="my-rag",
    description="从零搭建的完整 RAG 系统：索引流水线 + 推理流水线 + 前端界面",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Event-Stream-ID"],
)

# 注册路由
app.include_router(index_router)
app.include_router(chat_router)
app.include_router(provider_router)


@app.get("/", tags=["根"])
def root():
    return {
        "name": "my-rag",
        "version": "0.1.0",
        "docs": "/docs",
        "status": "running",
        "vector_db": VECTOR_DB_TYPE,
        "llm_model": MODEL_ID,
    }
