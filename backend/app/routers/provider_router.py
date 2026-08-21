"""
LLM Provider 配置路由
  GET    /api/providers              列表（api_key 脱敏）
  GET    /api/providers/{id}         详情（api_key 脱敏）
  POST   /api/providers              新建
  PUT    /api/providers/{id}         更新
  DELETE /api/providers/{id}         删除
  PUT    /api/providers/{id}/default 设为默认
  POST   /api/providers/{id}/test    测试连接
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services import provider_service

router = APIRouter(prefix="/api/providers", tags=["LLM Provider 配置"])


class ProviderCreate(BaseModel):
    name: str = Field(description="显示名称")
    model_id: str = Field(description="模型 ID")
    api_key: str = Field(default="", description="API Key")
    base_url: str = Field(description="API Base URL")
    provider: str = Field(default="", description="厂商名")
    is_default: bool = Field(default=False)
    active: bool = Field(default=True)
    supports_deep_think: bool = Field(default=False)
    temperature: float = Field(default=0.1)
    max_tokens: int = Field(default=4096)


class ProviderUpdate(BaseModel):
    name: str | None = None
    model_id: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    provider: str | None = None
    is_default: bool | None = None
    active: bool | None = None
    supports_deep_think: bool | None = None
    temperature: float | None = None
    max_tokens: int | None = None


@router.get("")
def list_providers():
    return provider_service.list_providers(mask_key=True)


@router.get("/{provider_id}")
def get_provider(provider_id: str):
    p = provider_service.get_provider(provider_id)
    if not p:
        raise HTTPException(status_code=404, detail="Provider 不存在")
    key = p.get("api_key", "")
    if key and len(key) > 12:
        p["api_key"] = key[:8] + "****" + key[-4:]
    elif key:
        p["api_key"] = "****"
    return p


@router.post("")
def create_provider(req: ProviderCreate):
    return provider_service.create_provider(req.model_dump())


@router.put("/{provider_id}")
def update_provider(provider_id: str, req: ProviderUpdate):
    updated = provider_service.update_provider(provider_id, req.model_dump(exclude_none=True))
    if not updated:
        raise HTTPException(status_code=404, detail="Provider 不存在")
    return updated


@router.delete("/{provider_id}")
def delete_provider(provider_id: str):
    ok = provider_service.delete_provider(provider_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Provider 不存在")
    return {"status": "ok"}


@router.put("/{provider_id}/default")
def set_default(provider_id: str):
    p = provider_service.set_default(provider_id)
    if not p:
        raise HTTPException(status_code=404, detail="Provider 不存在")
    return p


@router.post("/{provider_id}/test")
def test_provider(provider_id: str):
    return provider_service.test_provider(provider_id)
