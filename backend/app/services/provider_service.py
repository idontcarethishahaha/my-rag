"""
LLM Provider 配置管理服务。

设计参考 RAG-Pro 的 LLMConfig，但用 JSON 文件持久化（比数据库轻量）：
  - 首次启动时，从 .env 的 API_KEY / BASE_URL / MODEL_ID 自动种子一个默认 Provider
  - 之后所有 LLM 调用都从 providers.json 读取配置
  - 支持多 Provider（不同厂商 / 不同 key / 不同 base_url）
  - 每个 Provider 可以独立开关、设默认
"""
from __future__ import annotations

import json
import uuid
import logging
import threading
from pathlib import Path
from datetime import datetime

from ..config import API_KEY, BASE_URL, MODEL_ID

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_PROVIDERS_FILE = _DATA_DIR / "providers.json"
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _seed_from_env() -> list[dict]:
    """从 .env 配置创建初始 Provider"""
    return [{
        "id": _new_id(),
        "name": "智谱 GLM-4.5-Flash",
        "model_id": MODEL_ID or "glm-4.5-flash",
        "api_key": API_KEY or "",
        "base_url": BASE_URL or "https://open.bigmodel.cn/api/paas/v4",
        "provider": "智谱",
        "is_default": True,
        "active": True,
        "supports_deep_think": True,
        "temperature": 0.1,
        "max_tokens": 4096,
        "created_at": _now(),
        "updated_at": _now(),
    }]


def _load() -> list[dict]:
    if not _PROVIDERS_FILE.exists():
        providers = _seed_from_env()
        _save(providers)
        logger.info(f"[providers] 首次启动，从 .env 种子化 {len(providers)} 个 Provider")
        return providers
    try:
        text = _PROVIDERS_FILE.read_text(encoding="utf-8-sig")
        data = json.loads(text)
        if isinstance(data, list) and data:
            return data
    except Exception as e:
        logger.warning(f"[providers] 读取失败，重新种子：{e}")
    providers = _seed_from_env()
    _save(providers)
    return providers


def _save(providers: list[dict]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    text = json.dumps(providers, ensure_ascii=False, indent=2)
    _PROVIDERS_FILE.write_text(text, encoding="utf-8")


def list_providers(mask_key: bool = True) -> list[dict]:
    """返回所有 Provider。mask_key=True 时 api_key 脱敏。"""
    providers = _load()
    if not mask_key:
        return providers
    result = []
    for p in providers:
        item = dict(p)
        key = item.get("api_key", "")
        if key and len(key) > 12:
            item["api_key"] = key[:8] + "****" + key[-4:]
        elif key:
            item["api_key"] = "****"
        else:
            item["api_key"] = ""
        result.append(item)
    return result


def get_provider(provider_id: str) -> dict | None:
    for p in _load():
        if p["id"] == provider_id:
            return p
    return None


def get_default_provider() -> dict | None:
    providers = _load()
    for p in providers:
        if p.get("is_default") and p.get("active", True):
            return p
    for p in providers:
        if p.get("active", True):
            return p
    return providers[0] if providers else None


def get_provider_by_model(model_id: str) -> dict | None:
    """根据 model_id 查找 provider（用于 generator_service）"""
    providers = _load()
    for p in providers:
        if p.get("model_id") == model_id and p.get("active", True):
            return p
    for p in providers:
        if model_id in p.get("name", "").lower() and p.get("active", True):
            return p
    return get_default_provider()


def create_provider(data: dict) -> dict:
    with _LOCK:
        providers = _load()
        item = {
            "id": _new_id(),
            "name": data.get("name", "未命名 Provider"),
            "model_id": data.get("model_id", ""),
            "api_key": data.get("api_key", ""),
            "base_url": data.get("base_url", ""),
            "provider": data.get("provider", ""),
            "is_default": data.get("is_default", False),
            "active": data.get("active", True),
            "supports_deep_think": data.get("supports_deep_think", False),
            "temperature": data.get("temperature", 0.1),
            "max_tokens": data.get("max_tokens", 4096),
            "created_at": _now(),
            "updated_at": _now(),
        }
        if item["is_default"]:
            for p in providers:
                p["is_default"] = False
        providers.append(item)
        _save(providers)
        logger.info(f"[providers] 新建 Provider：{item['name']} ({item['model_id']})")
        return item


def update_provider(provider_id: str, data: dict) -> dict | None:
    with _LOCK:
        providers = _load()
        for p in providers:
            if p["id"] == provider_id:
                if "name" in data:
                    p["name"] = data["name"]
                if "model_id" in data:
                    p["model_id"] = data["model_id"]
                if "api_key" in data and data["api_key"]:
                    p["api_key"] = data["api_key"]
                if "base_url" in data:
                    p["base_url"] = data["base_url"]
                if "provider" in data:
                    p["provider"] = data["provider"]
                if "supports_deep_think" in data:
                    p["supports_deep_think"] = data["supports_deep_think"]
                if "temperature" in data:
                    p["temperature"] = data["temperature"]
                if "max_tokens" in data:
                    p["max_tokens"] = data["max_tokens"]
                if "active" in data:
                    p["active"] = data["active"]
                if "is_default" in data and data["is_default"]:
                    for other in providers:
                        if other["id"] != provider_id:
                            other["is_default"] = False
                    p["is_default"] = True
                p["updated_at"] = _now()
                _save(providers)
                logger.info(f"[providers] 更新 Provider：{p['name']}")
                return p
        return None


def delete_provider(provider_id: str) -> bool:
    with _LOCK:
        providers = _load()
        before = len(providers)
        providers = [p for p in providers if p["id"] != provider_id]
        if len(providers) == before:
            return False
        if providers and not any(p.get("is_default") for p in providers):
            providers[0]["is_default"] = True
        _save(providers)
        logger.info(f"[providers] 删除 Provider：{provider_id}")
        return True


def set_default(provider_id: str) -> dict | None:
    with _LOCK:
        providers = _load()
        for p in providers:
            p["is_default"] = (p["id"] == provider_id)
        _save(providers)
        for p in providers:
            if p["id"] == provider_id:
                logger.info(f"[providers] 设为默认：{p['name']}")
                return p
        return None


def test_provider(provider_id: str) -> dict:
    """测试 Provider 连接"""
    import httpx

    provider = get_provider(provider_id)
    if not provider:
        return {"ok": False, "error": "Provider 不存在"}

    base_url = provider.get("base_url", "").rstrip("/")
    api_key = provider.get("api_key", "")
    model_id = provider.get("model_id", "")

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                f"{base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model_id,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 5,
                    "stream": False,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                reply = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                return {"ok": True, "reply": reply[:50]}
            else:
                return {"ok": False, "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
