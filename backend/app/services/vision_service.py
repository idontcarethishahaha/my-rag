"""
视觉模型（VL）服务 —— 图片识别 + 多模态对话

使用 glm-4.1v-thinking-flash 视觉模型处理图片。
关键设计：
- 图片通过 base64 data URL 传给模型，不依赖 localhost 静态路径
- 图片预处理：RGBA→RGB + 最长边压缩 + JPEG 编码
- 支持纯图片问答、图片+文字多模态对话
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import time
from typing import Iterator

from ..config import (
    VL_API_KEY,
    VL_BASE_URL,
    VL_ENABLE,
    VL_IMAGE_MAX_EDGE,
    VL_MAX_IMAGE_MB,
    VL_MODEL,
    VL_TEMPERATURE,
)

logger = logging.getLogger(__name__)

# -------- 多模态对话 System Prompt --------
VL_CHAT_SYSTEM = (
    "你是一个有看图能力的智能助手。\n"
    "请根据用户上传的图片和文字问题，基于图片内容进行准确、自然的回答。\n"
    "如果用户没写问题只有图片，就用 2~3 句话概括图片主体内容。\n"
    "如果图片里找不到相关依据，明确告诉用户'这张图里没有相关信息'。\n"
    "回答要清晰、有条理。"
)


# ======================================================================
# 图片预处理
# ======================================================================
# 解压缩炸弹防护上限：解码前先读 header 检查声明尺寸（参考 picident-mcp）
# 一张小 PNG 头可以声明 20000×20000，全量解码会先耗尽内存，压缩逻辑根本跑不到
MAX_DECODED_PIXELS = 50_000_000  # 5000 万像素


def _check_declared_size(image_bytes: bytes) -> None:
    """解码前检查图片 header 声明的尺寸，超限直接拒绝（防解压缩炸弹）。
    PIL 自带上限约 1.79 亿像素（DecompressionBombError），这里收紧到 5000 万。"""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            w, h = img.size
    except Image.DecompressionBombError:
        # PIL 自己的炸弹检查（>1.79 亿像素）在 open 时就触发了
        raise ValueError("图片声明的尺寸过大（疑似解压缩炸弹），已拒绝")
    except Exception:
        return  # 格式未知/读不出 header —— 放行，让解码器决定
    if w * h > MAX_DECODED_PIXELS:
        raise ValueError(f"图片尺寸 {w}x{h} 超过 {MAX_DECODED_PIXELS // 1_000_000} 百万像素上限")


def preprocess_image(image_bytes: bytes, max_edge: int | None = None) -> bytes:
    """
    预处理：声明尺寸检查 + RGBA→RGB + 最长边压缩 + JPEG 编码。
    返回压缩后的 JPEG bytes；坏图抛 ValueError。
    """
    max_edge = max_edge if max_edge is not None else VL_IMAGE_MAX_EDGE
    if not image_bytes:
        raise ValueError("图片为空")

    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError("Pillow 未安装，请先 pip install Pillow") from e

    # 解码前的解压缩炸弹防护
    _check_declared_size(image_bytes)

    try:
        src_len = len(image_bytes)
        img = Image.open(io.BytesIO(image_bytes))
        img.load()  # 强制加载，防止 lazy loading 坏图

        # 强制转 RGB（覆盖 RGBA/P/LA/CMYK 等）
        if img.mode != "RGB":
            img = img.convert("RGB")

        # 最长边压缩
        if max_edge and max_edge > 0:
            w, h = img.size
            long_side = max(w, h)
            if long_side > max_edge:
                ratio = float(max_edge) / float(long_side)
                new_w = max(1, int(round(w * ratio)))
                new_h = max(1, int(round(h * ratio)))
                img = img.resize((new_w, new_h), Image.LANCZOS)

        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85, optimize=True)
        out_bytes = out.getvalue()
        logger.info(
            f"[vision] 图片预处理完成：原始 {src_len//1024}KB → "
            f"处理后 {len(out_bytes)//1024}KB，尺寸={img.size[0]}x{img.size[1]}"
        )
        return out_bytes
    except Exception as e:
        logger.exception(f"[vision] 图片预处理失败: {e}")
        raise ValueError(f"图片预处理失败: {e}") from e


def image_to_base64_dataurl(image_bytes: bytes, max_edge: int | None = None) -> str:
    """预处理 + 返回 'data:image/jpeg;base64,/9j/...'"""
    jpg_bytes = preprocess_image(image_bytes, max_edge=max_edge)
    b64 = base64.b64encode(jpg_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


# ======================================================================
# 内部：构造客户端
# ======================================================================
def _check_enabled():
    if not VL_ENABLE:
        raise RuntimeError("视觉能力已关闭，请设置 VL_ENABLE=true")
    if not VL_API_KEY:
        raise RuntimeError("VL_API_KEY 未配置")


def _bypass_proxy(base_url: str) -> bool:
    from urllib.parse import urlparse
    _DOMESTIC_HOSTS = {"open.bigmodel.cn", "api.minimax.chat", "dashscope.aliyuncs.com"}
    try:
        host = urlparse(base_url or "").hostname or ""
        return any(h in host for h in _DOMESTIC_HOSTS)
    except Exception:
        return False


def _make_async_client():
    import httpx
    from openai import AsyncOpenAI
    kwargs = dict(
        api_key=VL_API_KEY or "placeholder",
        timeout=180.0,
        max_retries=2,
    )
    if VL_BASE_URL:
        kwargs["base_url"] = VL_BASE_URL
    if _bypass_proxy(VL_BASE_URL):
        kwargs["http_client"] = httpx.AsyncClient(
            proxy=None,
            timeout=httpx.Timeout(180.0, connect=30.0),
        )
        logger.info(f"[vision] {VL_BASE_URL} 为国内 API，已绕过代理直连")
    return AsyncOpenAI(**kwargs)


def _is_bigmodel_cn() -> bool:
    return "bigmodel.cn" in (VL_BASE_URL or "")


def _normalize_messages(messages: list[dict]) -> list[dict]:
    """智谱原生场景下做 schema 兼容：除最后一条 user 消息外，其他历史消息数组 content 合成字符串。"""
    out: list[dict] = []
    last_idx = len(messages) - 1
    for i, m in enumerate(messages):
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, list):
            norm_items: list[dict] = []
            for item in content:
                if not isinstance(item, dict):
                    norm_items.append({"type": "text", "text": str(item)})
                    continue
                if item.get("type") == "image_file" and "file" in item:
                    f = item["file"] or {}
                    url = f.get("url") or f.get("data") or ""
                    norm_items.append({"type": "image_url", "image_url": {"url": url}})
                else:
                    norm_items.append(item)
            if _is_bigmodel_cn() and not (i == last_idx and role == "user"):
                texts = [str(it.get("text") or "") for it in norm_items if it.get("type") == "text"]
                out.append({"role": role, "content": "".join(texts) or " "})
            else:
                out.append({"role": role, "content": norm_items})
        else:
            out.append({"role": role, "content": content if isinstance(content, str) else str(content or "")})
    return out


# ======================================================================
# 同步调用
# ======================================================================
def _vl_call_sync(messages: list[dict], enable_deep_think: bool = False) -> tuple[str, str]:
    _check_enabled()
    start = time.time()
    norm_messages = _normalize_messages(messages)
    client = _make_async_client()
    extra_body = {}
    if enable_deep_think:
        extra_body["enable_thinking"] = True
    try:
        resp = asyncio.run(client.chat.completions.create(
            model=VL_MODEL,
            messages=norm_messages,
            temperature=VL_TEMPERATURE if not enable_deep_think else 0.3,
            max_tokens=4096,
            extra_body=extra_body,
        ))
        msg = resp.choices[0].message
        content = msg.content or ""
        thinking = ""
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning:
            thinking = str(reasoning)
        logger.info(
            f"[vision] 同步调用完成：耗时 {time.time() - start:.2f}s，"
            f"content={len(content)} 字，thinking={len(thinking)} 字"
        )
        return content, thinking
    except Exception as e:
        logger.exception(f"[vision] 同步调用失败（耗时 {time.time() - start:.2f}s）")
        raise


# ======================================================================
# 流式调用
# ======================================================================
def _vl_call_stream(
    messages: list[dict],
    enable_deep_think: bool = False,
) -> Iterator[tuple[str, str]]:
    _check_enabled()
    use_deep_think = enable_deep_think
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        norm_messages = _normalize_messages(messages)
        async_gen = _vl_stream_async(norm_messages, enable_deep_think=use_deep_think)
        for item in _syncify_stream(async_gen, loop):
            yield item
    finally:
        loop.close()


async def _vl_stream_async(messages, enable_deep_think: bool):
    start = time.time()
    client = _make_async_client()
    extra_body = {}
    if enable_deep_think:
        extra_body["enable_thinking"] = True
    try:
        stream = await client.chat.completions.create(
            model=VL_MODEL,
            messages=messages,
            stream=True,
            temperature=VL_TEMPERATURE if not enable_deep_think else 0.3,
            max_tokens=4096,
            extra_body=extra_body,
            stream_options={"include_usage": False},
        )
        token_count = 0
        async for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            if delta is None:
                continue
            reasoning = getattr(delta, "reasoning_content", None)
            if isinstance(reasoning, str) and reasoning:
                yield ("thinking", reasoning)
            content = getattr(delta, "content", None)
            if isinstance(content, str) and content:
                token_count += len(content)
                yield ("content", content)
        logger.info(
            f"[vision] 流式调用完成：耗时 {time.time() - start:.2f}s，输出约 {token_count} 字"
        )
    except Exception as e:
        logger.exception(f"[vision] 流式调用失败（耗时 {time.time() - start:.2f}s）")
        yield ("content", f"\n\n❌ 视觉模型调用失败：{e}")
    finally:
        try:
            await client.close()
        except Exception:
            pass


def _syncify_stream(async_gen, loop):
    from asyncio import Queue
    q: Queue = Queue()

    async def _drain():
        try:
            async for item in async_gen:
                await q.put(("item", item))
        except Exception as e:
            logger.exception("[vision] 异步流排水异常")
            await q.put(("error", e))
        finally:
            await q.put(("done", None))

    async def main():
        task = asyncio.create_task(_drain())
        while True:
            kind, payload = await q.get()
            if kind == "item":
                yield payload
            elif kind == "error":
                yield ("content", f"❌ {payload}")
            elif kind == "done":
                break
        task.done()

    agen = main()
    while True:
        try:
            yield loop.run_until_complete(agen.__anext__())
        except StopAsyncIteration:
            break


# ======================================================================
# 对外 API
# ======================================================================
def build_vl_messages(
    question: str,
    image_data_urls: list[str],
    history: list[dict] | None = None,
) -> list[dict]:
    """
    构建视觉模型的 messages。
    - 纯图片（question 为空）：user message 只放图片列表
    - 图片 + 文字：user message 放文字 + 图片列表
    - 支持多轮历史
    """
    messages = [{"role": "system", "content": VL_CHAT_SYSTEM}]

    # 历史消息
    for m in (history or []):
        role = m.get("role", "user")
        content = m.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    # 当前用户消息
    user_content = []
    if question and question.strip():
        user_content.append({"type": "text", "text": question.strip()})
    for url in image_data_urls:
        # detail: "auto" 是 OpenAI 兼容网关的标准字段（智谱会忽略，其他网关友好）
        user_content.append({"type": "image_url", "image_url": {"url": url, "detail": "auto"}})

    if not user_content:
        # 兜底
        user_content.append({"type": "text", "text": "请描述一下这些图片"})

    messages.append({"role": "user", "content": user_content})
    return messages


def chat_with_images(
    messages: list[dict],
    stream: bool = True,
    enable_deep_think: bool = False,
):
    """
    多模态对话：图片 + 文字 → 视觉模型回答。
    stream=True → 迭代器 (type, token)
    stream=False → (content, thinking)
    """
    if stream:
        return _vl_call_stream(messages, enable_deep_think=enable_deep_think)
    return _vl_call_sync(messages, enable_deep_think=enable_deep_think)
