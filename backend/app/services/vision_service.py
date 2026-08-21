"""
视觉模型（VL）服务 —— 统一入口：图片预处理 / OCR / 多模态对话（A+B+C 三个场景共用）

所有能力走 glm-4.1v-thinking-flash（在线），不装任何本地 PaddleOCR/Tesseract/CLIP。

关键经验：
 - ID 1219328: JPEG 保存前 RGBA/P/LA → RGB 强制转换，否则 "cannot write mode RGBA as JPEG"
 - ID  818026: Base URL 截止到 /v4 即可，不要加 /chat/completions
 - ID 1178834: 先把最小可用调用规范写对，再写代码；不要吞异常
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import time
from typing import Iterator, Tuple

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

# -------- OCR Prompt：严格两段式 --------
_OCR_SYSTEM = (
    "你是一个高精度的 OCR 与图片内容理解助手。\n"
    "你会收到一张图片和一段可选提示（hint，比如这是 PDF 第几页的内嵌图/扫描件）。\n"
    "请严格按下面两段格式输出，不要加多余文字：\n"
    "===== 图片文字内容 =====\n"
    "<将图片中所有可见文字原样识别出来，按自然行换行；表格请使用 | 分隔列以保留对齐感>\n"
    "===== 图片结构描述 =====\n"
    "<用 1~2 段话描述这张图片的主体内容、结构与类型：例如“这是一张发票，含发票抬头、开票日期、金额三栏；右上角有增值税普通发票红色印章”或“这是软件架构流程图，从左到右有输入、检索、LLM、输出四个矩形，中间用箭头连接”。>\n"
    "注意：\n"
    "  - 不要编造图片中看不到的内容\n"
    "  - 如果某段文字实在看不清，不要瞎猜，写 [模糊] 即可\n"
    "  - 如果是手写体、印章、竖排文字，也要尽力识别并在描述里标注"
)

# -------- 多模态对话 System Prompt（纯视觉回答，不引用知识库）--------
VL_CHAT_SYSTEM = (
    "你是一个有看图能力的智能助手，名字叫小橘。\n"
    "请根据用户上传的图片和文字问题，基于图片内容进行准确、自然的回答。\n"
    "如果用户没写问题只有图片，就用 2~3 句话概括图片主体内容。\n"
    "如果图片里找不到相关依据，明确告诉用户“这张图里没有相关信息”。\n"
    "回答语气亲切可爱，偶尔使用 (｡•ᴗ-｡)♡ 这类猫表情，不要做长篇大论的无关展开。"
)

# ======================================================================
# 图片预处理
# ======================================================================
def preprocess_image(image_bytes: bytes, max_edge: int | None = None) -> bytes:
    """
    预处理：RGBA→RGB + 最长边压缩 + JPEG 编码。
    坏图抛 ValueError（不要吞错），返回压缩后的 JPEG bytes。
    """
    max_edge = max_edge if max_edge is not None else VL_IMAGE_MAX_EDGE
    if not image_bytes:
        raise ValueError("图片为空")

    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError("Pillow 未安装，请先 pip install Pillow") from e

    try:
        src_len = len(image_bytes)
        img = Image.open(io.BytesIO(image_bytes))
        # 必须先 load 一下，防止 lazy loading 坏图在 convert 阶段才炸
        img.load()

        # 经验 1219328：存 JPEG 前强制转 RGB，覆盖 RGBA/P/LA/CMYK/YCbCr 等
        if img.mode != "RGB":
            img = img.convert("RGB")

        # 最长边压缩（max_edge<=0 表示不压缩）
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
            f"[vision] 图片预处理完成：原始 {src_len} 字节 → 处理后 {len(out_bytes)} 字节 "
            f"（原 {src_len//1024}KB → {len(out_bytes)//1024}KB，"
            f"尺寸={img.size[0]}x{img.size[1]}）"
        )
        return out_bytes
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[vision] 图片预处理失败: {e}")
        raise ValueError(f"图片预处理失败: {e}") from e


def image_to_base64_dataurl(image_bytes: bytes, max_edge: int | None = None) -> str:
    """预处理 + 返回 'data:image/jpeg;base64,/9j/...'"""
    jpg_bytes = preprocess_image(image_bytes, max_edge=max_edge)
    b64 = base64.b64encode(jpg_bytes).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


# ======================================================================
# OCR：图片 → 结构化文本（两段式）
# ======================================================================
def _check_enabled():
    if not VL_ENABLE:
        raise RuntimeError("视觉能力已关闭，请设置 VL_ENABLE=true")
    if not VL_API_KEY:
        raise RuntimeError("VL_API_KEY 未配置")


def ocr_image(image_bytes: bytes, hint: str = "") -> str:
    """
    A/B 场景：对一张图片做 OCR + 内容理解。
    hint: 可选，比如 "这是 PDF 第 3 页的扫描件"。
    返回严格两段式文本；失败抛出异常（调用方自己降级）。
    """
    _check_enabled()
    data_url = image_to_base64_dataurl(image_bytes)

    user_text = "请对下面的图片做 OCR 与内容描述。"
    if hint:
        user_text += f"\n附加提示：{hint}"

    messages = [
        {"role": "system", "content": _OCR_SYSTEM},
        {"role": "user", "content": [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": data_url}},
        ]},
    ]

    start = time.time()
    try:
        content, _thinking = _vl_call_sync(messages, use_deep_think=False)
        # 基本校验：至少包含一个"====="分隔符或有一定字数，否则认为失败
        if not content or len(content.strip()) < 2:
            raise RuntimeError("视觉模型返回空内容")
        logger.info(
            f"[vision] OCR OK，耗时 {time.time() - start:.2f}s，"
            f"输出 {len(content)} 字符"
        )
        return content.strip()
    except Exception as e:
        logger.exception(f"[vision] OCR 失败: {e}")
        raise


def ocr_image_batch(image_bytes_list: list[bytes], hints: list[str] | None = None) -> list[str]:
    """
    B 场景批次 OCR。串行一张一张来，避免多图混在一个请求里质量下降。
    单张失败则该位置填 '[OCR失败: xxx]'，不抛出，调用方可以看到哪些图出问题。
    """
    if hints is None:
        hints = [""] * len(image_bytes_list)
    if len(hints) != len(image_bytes_list):
        raise ValueError("hints 长度必须和图片列表一致")

    results: list[str] = []
    for i, (img, hint) in enumerate(zip(image_bytes_list, hints)):
        try:
            results.append(ocr_image(img, hint=hint))
        except Exception as e:
            logger.warning(f"[vision] 第 {i + 1}/{len(image_bytes_list)} 张图 OCR 失败，打占位: {e}")
            results.append(f"----- 图片识别内容（失败占位）-----\n[OCR 失败: {e}]")
    return results


# ======================================================================
# 多模态对话（C 场景）
# ======================================================================
def chat_with_images(
    messages: list[dict],
    stream: bool = False,
    enable_deep_think: bool = False,
    model: str | None = None,
):
    """
    messages 必须已经拼好 image_url 多模态格式。
    stream=False → (content: str, thinking: str)
    stream=True  → Iterator[(type: {'thinking'|'content'}, token: str)]
    """
    _check_enabled()
    if stream:
        return _vl_call_stream(messages, enable_deep_think=enable_deep_think, model=model)
    return _vl_call_sync(messages, enable_deep_think=enable_deep_think, model=model)


# ======================================================================
# 内部：构造直连 AsyncOpenAI（与 generator_service._make_async_openai 保持一致）
# ======================================================================
def _resolve_model(model: str | None) -> str:
    return model or VL_MODEL


def _bypass_proxy(base_url: str) -> bool:
    from urllib.parse import urlparse
    _DOMESTIC_HOSTS = {"open.bigmodel.cn", "api.minimax.chat", "dashscope.aliyuncs.com"}
    try:
        host = urlparse(base_url or "").hostname or ""
        return any(h in host for h in _DOMESTIC_HOSTS)
    except Exception:
        return False


def _make_async_openai_for_vl():
    import httpx
    from openai import AsyncOpenAI
    api_key = VL_API_KEY or "placeholder"
    base_url = VL_BASE_URL
    kwargs = dict(
        api_key=api_key,
        timeout=180.0,   # 视觉任务比纯文本更慢，给 3 分钟
        max_retries=2,
    )
    if base_url:
        kwargs["base_url"] = base_url
    if _bypass_proxy(base_url):
        kwargs["http_client"] = httpx.AsyncClient(
            proxy=None,
            timeout=httpx.Timeout(180.0, connect=30.0),
        )
        logger.info(f"[vision] {base_url} 为国内 API，已绕过代理直连")
    return AsyncOpenAI(**kwargs)


def _is_bigmodel_cn() -> bool:
    """VL_BASE_URL 是否指向智谱原生（open.bigmodel.cn）。
    glm-4.1v-thinking-flash 是智谱原生的免费视觉模型（已实测可用），
    接受 OpenAI 标准多模态 content 数组（image_url 在前在后均可）。
    返回 True 时对历史消息做「数组→字符串」归一，仅为兼容智谱更严格的 schema 校验。
    """
    return "bigmodel.cn" in (VL_BASE_URL or "")


def _normalize_messages_for_schema(messages: list[dict]) -> list[dict]:
    """
    对消息体做 schema 归一化：
    1) 将任何消息里的 "image_file" 老字段统一转为 "image_url"；
    2) 智谱原生（bigmodel.cn）场景下，做一次"严格兼容"：
       - 除了最后一条 user message（含图片的那一条）保留数组结构以外，
         其它历史消息全部把数组式 content 拼接成纯字符串；
       - 这样即使智谱原生只接受"最后一条 user 是数组"的严格写法，也能过。
    """
    out: list[dict] = []
    last_idx = len(messages) - 1
    for i, m in enumerate(messages):
        role = m.get("role", "user")
        content = m.get("content")
        # 统一：image_file -> image_url.url
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
            # 智谱原生严格模式：最后一条 user 以外的消息，数组 content 强行合成字符串
            if _is_bigmodel_cn() and not (i == last_idx and role == "user"):
                texts = []
                for it in norm_items:
                    if it.get("type") == "text":
                        texts.append(str(it.get("text") or ""))
                out.append({"role": role, "content": "".join(texts) or " "})
            else:
                out.append({"role": role, "content": norm_items})
        else:
            out.append({"role": role, "content": content if isinstance(content, str) else str(content or "")})
    return out


def _translate_400_hint(e: Exception) -> str | None:
    """
    经验 1108527 / 682032：
    当 API 返回 code=1210 或 "unknown variant image_url, expected text" 时，
    本质是：请求里的 model 是一个纯文本模型（如 glm-4.5-flash），服务端按文本模型
    校验 content.type，自然只允许 ['text']。glm-4.1v-thinking-flash 本身在智谱原生
    是合法视觉模型（已实测），消息格式（image_url 在前在后均可）也都合法。
    排查顺序：
      1) 看本文件上方日志「流式调用开始/同步调用开始：model=xxx」——发出去的到底是不是 VL_MODEL；
      2) 若不是：调用方透传了前端文本模型（chat_router 必须传 model=None）；
      3) 若是：检查 .env 的 VL_MODEL 是否被改成了文本模型名；改完必须 Ctrl+C 全量重启 uvicorn。
    返回：友好中文提示字符串；命中不到就返回 None 走原异常。
    """
    s = str(e)
    hits = [
        ("content.type 参数非法" in s),
        ("取值范围 ['text']" in s),
        ("unknown variant `image_url'" in s),
        ("expected `text'" in s and "image_url" in s),
        ("\"code\": \"1210\"" in s),
        ("code=1210" in s),
    ]
    if not any(hits):
        return None
    return (
        f"\n\n⚠️ 【发错模型了】这次请求的模型是「纯文本模型」，不接受图片 content。\n"
        f"glm-4.1v-thinking-flash 在智谱原生是合法视觉模型（已实测），不用换 base_url 也不用换模型。\n"
        f"排查两步：\n"
        f"  1) 在日志里搜「流式调用开始：model=」，确认发出去的是 {VL_MODEL}；\n"
        f"     若是 glm-4.5-flash 之类文本模型 → 调用方透传了前端模型，chat_router 必须传 model=None；\n"
        f"  2) 若模型名正确还报 1210 → 改过 backend/.env 后必须 Ctrl+C 全量重启 uvicorn（改配置不会热生效）。\n"
    )


def _vl_call_sync(messages: list[dict], enable_deep_think: bool = False, model: str | None = None) -> Tuple[str, str]:
    start = time.time()
    model_id = _resolve_model(model)
    norm_messages = _normalize_messages_for_schema(messages)
    logger.info(f"[vision] 同步调用开始：model={model_id}，message.role={norm_messages[-1].get('role')}，bigmodel_native={_is_bigmodel_cn()}")
    client = _make_async_openai_for_vl()
    extra_body = {}
    if enable_deep_think:
        extra_body["enable_thinking"] = True
    try:
        resp = asyncio.run(client.chat.completions.create(
            model=model_id,
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
        err_msg = str(e)[:500]
        hint = _translate_400_hint(e)
        logger.exception(f"[vision] 同步调用失败（耗时 {time.time() - start:.2f}s）：{err_msg}")
        if hint:
            raise RuntimeError(hint) from e
        raise


def _vl_call_stream(
    messages: list[dict],
    enable_deep_think: bool = False,
    model: str | None = None,
) -> Iterator[Tuple[str, str]]:
    """和 generator_service.chat_stream 同结构：封装 async for → 同步生成器"""
    model_id = _resolve_model(model)
    use_deep_think = enable_deep_think  # glm-4.1v-thinking-flash 支持
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        norm_messages = _normalize_messages_for_schema(messages)
        async_gen = _vl_stream_async(norm_messages, model=model_id, enable_deep_think=use_deep_think)
        for item in _syncify_stream(async_gen, loop):
            yield item
    finally:
        loop.close()


async def _vl_stream_async(messages, model: str, enable_deep_think: bool):
    start = time.time()
    client = _make_async_openai_for_vl()
    logger.info(f"[vision] 流式调用开始：model={model}，bigmodel_native={_is_bigmodel_cn()}")
    extra_body = {}
    if enable_deep_think:
        extra_body["enable_thinking"] = True
    try:
        stream = await client.chat.completions.create(
            model=model,
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
        hint = _translate_400_hint(e)
        elapsed = time.time() - start
        logger.exception(f"[vision] 流式调用失败（耗时 {elapsed:.2f}s）：{e}")
        if hint:
            yield ("content", hint)
        else:
            yield ("content", f"\n\n❌ 视觉模型调用失败：{e}")


def _syncify_stream(async_gen, loop):
    """和 generator_service._syncify 一样的思路：Queue 中转"""
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
        task.done()  # 抑制未完成警告（已经 drain 完）

    agen = main()
    while True:
        try:
            yield loop.run_until_complete(agen.__anext__())
        except StopAsyncIteration:
            break
