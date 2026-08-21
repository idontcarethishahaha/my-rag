# 视觉识别 / OCR / 多模态对话 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 my-rag 项目新增三大视觉能力（A：图片作为文档上传入库；B：PDF/Word 内嵌图 + 扫描件 OCR；C：对话时直接贴图片提问），所有视觉能力统一走 `glm-4.1v-thinking-flash`（在线，零本地模型依赖），不破坏现有 `/chat/stream` JSON 接口，对现有纯文本流程零侵入。

**Architecture:**
- 新增 `backend/app/services/vision_service.py`：负责图片预处理（尺寸压缩 + RGBA→RGB 强制 JPEG）、图片转 base64 data URL、`ocr_image`（OCR + 内容描述两段式）、`chat_with_images`（多模态流式 / 非流式）
- 配置层新增 VL_* 常量 9 个；`.env.example` 加 VL 区块；`requirements.txt` 加 `pymupdf` + `Pillow`
- A 场景：`document_loader` 扩图片扩展名 + `_load_image` 返回占位 Document，`indexer_service.upload_and_parse` 在缓存前调 vision OCR 回填文本，后续分块/嵌入零改动
- B 场景：`_load_pdf` 切 PyMuPDF 主路径（文本≥100字跳过OCR，否则该页整页 + 内嵌图 OCR 追加）；`_load_word` 用 python-docx 提内嵌图 OCR 追加
- C 场景：`rag_service` 新增兄弟函数 `ask_vl_stream`（纯视觉回答，不走检索）；`chat_router` 新增 `/chat/image-stream` 多部分表单流式接口；`main.py` mount `/uploads` 静态目录
- 前端 index.html 小改：输入框左侧加 🖼 按钮 + 缩略图预览条 + 多部分表单发送 + 气泡图片展示 + debug intent='vl_chat' 青色 pill

**Tech Stack:** PyMuPDF 1.24+, Pillow（LangChain 传递依赖）, python-docx（已存在）, FastAPI Form/File/UploadFile（python-multipart 已存在）, 智谱 glm-4.1v-thinking-flash（OpenAI 兼容多模态接口）

---

## Task 1：配置层 — VL 配置 + 依赖补齐

**Files:**
- Modify: `backend/app/config.py`
- Modify: `backend/.env.example`
- Modify: `backend/requirements.txt`

- [ ] **Step 1: 打开 `backend/app/config.py`** 在现有"Rerank 重排序配置"和"路径"两个区块之间，插入下面的 VL 配置 9 个常量：

```python
# ==================================
# 视觉模型（VL）/ OCR 配置
#  所有 A/B/C 三个场景统一走 glm-4.1v-thinking-flash（在线视觉模型）
#  - Base URL 写截止到 /v4 一级，不要加 /chat/completions！
#    （经验 818026：OpenAI SDK 会自动拼 /chat/completions）
# ==================================
VL_ENABLE = os.getenv("VL_ENABLE", "true").lower() == "true"
VL_MODEL = os.getenv("VL_MODEL", "glm-4.1v-thinking-flash")
VL_API_KEY = os.getenv("VL_API_KEY", "")
VL_BASE_URL = _clean_url(os.getenv("VL_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"))
VL_TEMPERATURE = float(os.getenv("VL_TEMPERATURE", "0.0"))
VL_MAX_IMAGES = int(os.getenv("VL_MAX_IMAGES", "4"))
VL_MAX_IMAGE_MB = float(os.getenv("VL_MAX_IMAGE_MB", "10"))
VL_OCR_TEXT_THRESHOLD = int(os.getenv("VL_OCR_TEXT_THRESHOLD", "100"))
VL_IMAGE_MAX_EDGE = int(os.getenv("VL_IMAGE_MAX_EDGE", "1024"))
```

- [ ] **Step 2: 修改 `config.py` 底部 `reload()` 函数**
  1. 在函数顶部的 `global` 声明里追加 9 个变量：
     ```python
     global VL_ENABLE, VL_MODEL, VL_API_KEY, VL_BASE_URL, VL_TEMPERATURE
     global VL_MAX_IMAGES, VL_MAX_IMAGE_MB, VL_OCR_TEXT_THRESHOLD, VL_IMAGE_MAX_EDGE
     ```
  2. 在函数末尾（`RERANK_TOP_N = ...` 那行之后）追加 reload 赋值：
     ```python
     VL_ENABLE = os.getenv("VL_ENABLE", "true").lower() == "true"
     VL_MODEL = os.getenv("VL_MODEL", "glm-4.1v-thinking-flash")
     VL_API_KEY = os.getenv("VL_API_KEY", "")
     VL_BASE_URL = _clean_url(os.getenv("VL_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"))
     VL_TEMPERATURE = float(os.getenv("VL_TEMPERATURE", "0.0"))
     VL_MAX_IMAGES = int(os.getenv("VL_MAX_IMAGES", "4"))
     VL_MAX_IMAGE_MB = float(os.getenv("VL_MAX_IMAGE_MB", "10"))
     VL_OCR_TEXT_THRESHOLD = int(os.getenv("VL_OCR_TEXT_THRESHOLD", "100"))
     VL_IMAGE_MAX_EDGE = int(os.getenv("VL_IMAGE_MAX_EDGE", "1024"))
     ```

- [ ] **Step 3: 打开 `backend/.env.example`，在文件末尾追加** VL 配置区块（注意 URL 不要加反引号，key 用占位符，不要硬写用户真实 key）：

```
# ================= 视觉模型 / OCR 配置 =================
#
# 说明：
#   A 场景（图片作为文档上传入库）+ B 场景（PDF/Word 内嵌图+扫描件 OCR）
# + C 场景（对话时直接贴图片提问）三个场景统一走下面这个在线视觉模型
#   不装任何本地 OCR 模型（PaddleOCR/Tesseract）
#
# 默认：智谱 glm-4.1v-thinking-flash（OpenAI 兼容多模态接口）
# Base URL 只写到 /v4 即可，SDK 会自动拼 /chat/completions
VL_ENABLE=true
VL_MODEL=glm-4.1v-thinking-flash
VL_API_KEY=your_zhipu_vl_api_key_here
VL_BASE_URL=https://open.bigmodel.cn/api/paas/v4
VL_TEMPERATURE=0.0
VL_MAX_IMAGES=4                           # 单次请求最大图片张数（A/C 通用）
VL_MAX_IMAGE_MB=10                        # 单张图片大小上限（MB）
VL_OCR_TEXT_THRESHOLD=100                 # B 场景：PDF 每页原文本<此字才做 OCR（省 token）
VL_IMAGE_MAX_EDGE=1024                    # 图片最长边压缩到此像素（省 token），0=不压缩
```

- [ ] **Step 4: 打开 `backend/requirements.txt`**
  在"文档加载 / 解析"区块里（`markdown>=3.7` 那行下面）新增：

```
# ======== 新增：视觉 / OCR（第十一轮）========
Pillow>=10.0.0                             # 图片预处理（RGBA→RGB、尺寸压缩），LangChain 可能传依赖，这里显式声明
pymupdf>=1.24.0                             # PDF 每页渲染为图 + 提取内嵌图 + 提取每页文本（比 PyPDF 强）
# (python-docx 已在上方声明，用于 Word 内嵌图提取，不需再加)
```

---

## Task 2：vision_service.py（预处理 + OCR + 多模态对话 + 埋点日志）

**Files:**
- Create: `backend/app/services/vision_service.py`

- [ ] **Step 1: 创建文件并写入完整实现**，按下面结构，模块顶部常量 + 5 个对外函数。注意：
  - `preprocess_image` 必须先判 `img.mode != 'RGB'` 再转 RGB（经验 1219328），失败抛 `ValueError`
  - 所有 vision 调用打结构化日志：压缩前后字节数、请求耗时、状态码、错误体前 200 字
  - OCR Prompt 强制输出"两段式"结构：`===== 图片文字内容 =====` + `===== 图片结构描述 =====`
  - `chat_with_images` 非流式返回 `(content, thinking)`，流式返回 `Iterator[(type, token)]`，逻辑完全复用 generator_service 里 `_make_async_openai` 的思路（直连绕代理）

```python
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
def _check_enabled():
    if not VL_ENABLE:
        raise RuntimeError("视觉能力已关闭，请设置 VL_ENABLE=true")
    if not VL_API_KEY:
        raise RuntimeError("VL_API_KEY 未配置")


def _resolve_model(model: str | None) -> str:
    return model or VL_MODEL


def _vl_env():
    """返回 (api_key, base_url, model_id, bypass_proxy_bool, domestic_hosts_set)"""
    return VL_API_KEY or "placeholder", VL_BASE_URL, VL_MODEL, _bypass_proxy(VL_BASE_URL)


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
    api_key, base_url, _m, bypass = _vl_env()
    kwargs = dict(
        api_key=api_key,
        timeout=180.0,   # 视觉任务比纯文本更慢，给 3 分钟
        max_retries=2,
    )
    if base_url:
        kwargs["base_url"] = base_url
    if bypass:
        kwargs["http_client"] = httpx.AsyncClient(
            proxy=None,
            timeout=httpx.Timeout(180.0, connect=30.0),
        )
        logger.info(f"[vision] {base_url} 为国内 API，已绕过代理直连")
    return AsyncOpenAI(**kwargs)


def _vl_call_sync(messages: list[dict], enable_deep_think: bool = False, model: str | None = None) -> Tuple[str, str]:
    start = time.time()
    model_id = _resolve_model(model)
    logger.info(f"[vision] 同步调用开始：model={model_id}，message.role={messages[-1].get('role')}")
    client = _make_async_openai_for_vl()
    extra_body = {}
    if enable_deep_think:
        extra_body["enable_thinking"] = True
    try:
        resp = asyncio.run(client.chat.completions.create(
            model=model_id,
            messages=messages,
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
        logger.exception(f"[vision] 同步调用失败（耗时 {time.time() - start:.2f}s）：{err_msg}")
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
        async_gen = _vl_stream_async(messages, model=model_id, enable_deep_think=use_deep_think)
        for item in _syncify_stream(async_gen, loop):
            yield item
    finally:
        loop.close()


async def _vl_stream_async(messages, model: str, enable_deep_think: bool):
    start = time.time()
    client = _make_async_openai_for_vl()
    logger.info(f"[vision] 流式调用开始：model={model}")
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
        logger.exception(f"[vision] 流式调用失败（耗时 {time.time() - start:.2f}s）：{e}")
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
```

---

## Task 3：A 场景 — 图片作为文档上传入库（扩展名 + 占位 Document + OCR 回填）

**Files:**
- Modify: `backend/app/loaders/document_loader.py`
- Modify: `backend/app/services/indexer_service.py`

- [ ] **Step 1: 打开 `backend/app/loaders/document_loader.py`**
  1. 在 `SUPPORTED_EXTS` 字典中（`".md": True, ".txt": True, ...` 那里）追加图片扩展名：
     ```python
     # 图片（A 场景：作为文档上传后做 OCR）
     ".jpg": True, ".jpeg": True, ".png": True, ".tif": True, ".tiff": True, ".bmp": True, ".webp": True,
     ```
  2. 在"文档加载器"部分（`_load_pdf/_load_word/_load_excel...` 那群函数的附近）新增图片占位函数 `_load_image`：

     ```python
     def _load_image(file_path: str, name: str) -> list[Document]:
         """
         A 场景：纯图片文档。
         这里 NOT 做 OCR（load_document 保持轻量），只返回一个 page_content 为空的占位 Document。
         indexer_service.upload_and_parse 会在缓存前读取 metadata.image_path 调 vision OCR 回填。
         """
         doc = Document(
             page_content="",
             metadata={
                 "source": name,
                 "file_type": "image",
                 "image_path": file_path,
                 "needs_ocr": True,
             },
         )
         return [doc]
     ```

  3. 修改 `load_document` 的分支路由。定位到最后一个 `else`（`.md/.txt` 的兜底）之前，在它上面插入图片分支：

     ```python
         # 图片：返回占位，后面由 indexer_service 调 vision OCR 回填文本
         ext = file_ext if file_ext.startswith(".") else "." + file_ext
         if ext.lower() in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}:
             return _load_image(save_path, name)

         # .md / .txt（纯文本兜底）
         with open(save_path, "r", encoding="utf-8", errors="ignore") as f:
             content = f.read()
         return [Document(page_content=content, metadata={"source": name, "file_type": ext.lower().lstrip(".")})]
     ```

- [ ] **Step 2: 打开 `backend/app/services/indexer_service.py`**
  在 `upload_and_parse` 函数里，定位到 "`docs = load_document(save_path, file_name=filename)`" 之后、"`if not any(d.page_content.strip() for d in docs):`" 之前，插入 vision OCR 回填块（注意：**仅对 needs_ocr=True 的文档调用 vision**，纯文本 PDF/Word 不影响）：

  ```python
      docs = load_document(save_path, file_name=filename)

      # ------ 新增：图片类文档 → 调 vision OCR 回填 page_content（A 场景）------
      for d in docs:
          if d.metadata.get("needs_ocr"):
              from app.config import VL_ENABLE
              if not VL_ENABLE:
                  raise RuntimeError("当前上传的是图片文件，但 VL_ENABLE=false，视觉 OCR 能力已关闭。请在 .env 中开启 VL_ENABLE=true，并配置 VL_API_KEY。")
              img_path = d.metadata.get("image_path") or save_path
              try:
                  with open(img_path, "rb") as f:
                      img_bytes = f.read()
                  from app.services.vision_service import ocr_image
                  ocr_text = ocr_image(img_bytes, hint=f"这是作为知识库文档上传的图片：{filename}")
                  if not ocr_text.strip():
                      raise RuntimeError("图片 OCR 返回空内容")
                  d.page_content = ocr_text
                  d.metadata.pop("needs_ocr", None)
                  d.metadata["ocr_by"] = "glm-4.1v-thinking-flash"
              except Exception as e:
                  raise RuntimeError(f"图片 {filename} OCR 失败：{e}") from e
      # ------ 新增结束 ------

      if not any(d.page_content.strip() for d in docs):
          raise RuntimeError("文件内容为空或无法解析")
  ```

- [ ] **Step 3: 确认 `from langchain_core.documents import Document` 已在 document_loader.py 顶部存在**（是的，本来就有）；不需要补 import。

---

## Task 4：B 场景 — PDF/Word 内嵌图 + 扫描件 OCR（PyMuPDF 重写）

**Files:**
- Modify: `backend/app/loaders/document_loader.py`

- [ ] **Step 1: 打开 `backend/app/loaders/document_loader.py`**
  1. 在顶部 import 区域最后一行（`from langchain_core.documents import Document` 之后也行）添加导入：

     ```python
     from app.config import VL_ENABLE, VL_MAX_IMAGES, VL_OCR_TEXT_THRESHOLD
     ```

  2. **替换 `_load_pdf` 整个函数**（原来的 `PyPDFLoader` 版本）。定位 `def _load_pdf(file_path: str, name: str) -> list[Document]:` 开始到它结束（大概 13~15 行）。整个替换为下面版本：

     ```python
     def _load_pdf(file_path: str, name: str) -> list[Document]:
         """
         B 场景 + 普通 PDF 加载：使用 PyMuPDF（fitz）。
         步骤：
          1. 提取每页原生文本；
          2. 提取每页内嵌图片；
          3. 如果该页原生文本 < VL_OCR_TEXT_THRESHOLD，则把整页渲染为图片做 OCR（判定扫描件）；
          4. 内嵌图无论文本多少都做 OCR（图表是核心价值）；
          5. 所有 OCR 文本追加到该页内容末尾，用 "----- 图片识别内容 -----" 分隔符。
         任何 OCR 失败只打 warning，不影响文本主流程。
         如果 PyMuPDF 加载失败，退回 PyPDFLoader（兜底）。
         """
         import logging
         logger = logging.getLogger(__name__)
         try:
             import fitz  # pymupdf
         except Exception as e:  # noqa: BLE001
             logger.warning(f"[loaders] PyMuPDF 不可用，退回 PyPDFLoader（将不做扫描件 OCR）: {e}")
             from langchain_community.document_loaders import PyPDFLoader
             loader = PyPDFLoader(file_path)
             _docs = loader.load()
             for d in _docs:
                 d.metadata.setdefault("source", name)
                 d.metadata.setdefault("file_type", "pdf")
                 if "page" in d.metadata and isinstance(d.metadata["page"], int):
                     d.metadata["page"] = d.metadata["page"] + 1  # 对齐 human-readable 1-based
             return _docs

         # ---- PyMuPDF 主路径 ----
         doc = fitz.open(file_path)
         try:
             docs: list[Document] = []
             # 每一页对应的 OCR 追加文本（先收集再串行调用 vision，避免失败中断整本）
             page_to_extra_texts: dict[int, list[str]] = {i: [] for i in range(doc.page_count)}

             for page_idx in range(doc.page_count):
                 page = doc[page_idx]
                 raw_text = page.get_text("text") or ""

                 # 收集该页内嵌图
                 embedded_images: list[tuple[int, bytes]] = []  # (i_in_page, bytes)
                 if VL_ENABLE:
                     for i_in_page, img_info in enumerate(page.get_images(full=True) or []):
                         if i_in_page >= VL_MAX_IMAGES:
                             break
                         try:
                             base = doc.extract_image(img_info[0])
                             img_bytes = base.get("image") or b""
                             if len(img_bytes) >= 100:
                                 embedded_images.append((i_in_page, img_bytes))
                         except Exception as e:  # noqa: BLE001
                             logger.warning(f"[loaders] PDF p{page_idx+1} 内嵌图 #{i_in_page+1} 提取跳过: {e}")

                 # 是否做整页扫描件 OCR
                 need_page_ocr = (
                     VL_ENABLE
                     and len(raw_text.strip()) < VL_OCR_TEXT_THRESHOLD
                 )

                 page_img_bytes: bytes | None = None
                 if need_page_ocr:
                     try:
                         pix = page.get_pixmap(dpi=150)
                         page_img_bytes = pix.tobytes("jpeg")
                     except Exception as e:  # noqa: BLE001
                         logger.warning(f"[loaders] PDF p{page_idx+1} 渲染失败，跳过整页 OCR: {e}")

                 # -------- 串行 OCR（和 Task 2 vision_service 的"失败只记 warning"策略一致）--------
                 if VL_ENABLE:
                     from app.services.vision_service import ocr_image
                     # 整页扫描件 OCR
                     if page_img_bytes:
                         hint = f"这是 PDF《{name}》第 {page_idx + 1} 页的整页扫描件 OCR，请识别全部文字和表格。"
                         try:
                             t = ocr_image(page_img_bytes, hint=hint)
                             if t.strip():
                                 page_to_extra_texts[page_idx].append(t)
                         except Exception as e:  # noqa: BLE001
                             logger.warning(f"[loaders] PDF p{page_idx+1} 整页 OCR 失败: {e}")
                     # 内嵌图 OCR
                     for i_in_page, emb_bytes in embedded_images:
                         hint = f"这是 PDF《{name}》第 {page_idx + 1} 页中第 {i_in_page + 1} 张内嵌图片，可能是图表/流程图/截图，请做 OCR 并描述结构。"
                         try:
                             t = ocr_image(emb_bytes, hint=hint)
                             if t.strip():
                                 page_to_extra_texts[page_idx].append(t)
                         except Exception as e:  # noqa: BLE001
                             logger.warning(f"[loaders] PDF p{page_idx+1} 内嵌图 #{i_in_page+1} OCR 失败: {e}")

                 # 组装 page_content
                 final_text = raw_text
                 extras = page_to_extra_texts.get(page_idx) or []
                 if extras:
                     merged = "\n\n".join(extras)
                     final_text = (
                         (final_text.rstrip() + "\n\n----- 图片识别内容 -----\n\n" + merged)
                         if final_text.strip()
                         else "----- 图片识别内容 -----\n\n" + merged
                     )

                 docs.append(Document(
                     page_content=final_text,
                     metadata={
                         "source": name,
                         "file_type": "pdf",
                         "page": page_idx + 1,  # 1-based，和人类阅读习惯一致
                         "ocr_appended_count": len(extras),
                         "ocr_by": "glm-4.1v-thinking-flash" if extras else None,
                     },
                 ))
             return docs
         finally:
             doc.close()
     ```

  3. **扩展 `_load_word`（Word 内嵌图 OCR）**。原 `_load_word` 很短（Docx2txtLoader.load() + 改元数据），保留原逻辑，在 `return docs` 之前加内嵌图提取与 OCR：

     ```python
     def _load_word(file_path: str, name: str) -> list[Document]:
         from langchain_community.document_loaders import Docx2txtLoader
         loader = Docx2txtLoader(file_path)
         docs = loader.load()

         # ------ 新增：Word 内嵌图 OCR（B 场景）------
         import logging
         logger = logging.getLogger(__name__)
         if VL_ENABLE and docs:
             try:
                 from docx import Document as DocxDoc
             except Exception as e:  # noqa: BLE001
                 logger.warning(f"[loaders] python-docx 不可用，无法提取 Word 内嵌图: {e}")
                 DocxDoc = None  # type: ignore

             if DocxDoc is not None:
                 try:
                     dxd = DocxDoc(file_path)
                     extras: list[str] = []
                     from app.services.vision_service import ocr_image
                     count = 0
                     for rel in dxd.part.rels.values():
                         if "image" not in (getattr(rel, "reltype", "") or ""):
                             continue
                         try:
                             img_bytes = rel.target_part.blob or b""
                             if len(img_bytes) < 100:
                                 continue
                             t = ocr_image(img_bytes, hint=f"Word 文档《{name}》中的内嵌图片")
                             if t.strip():
                                 extras.append(t)
                                 count += 1
                                 if count >= VL_MAX_IMAGES:
                                     break
                         except Exception as e:  # noqa: BLE001
                             logger.warning(f"[loaders] Word 内嵌图 OCR 失败: {e}")
                     if extras:
                         merged = "\n\n----- 图片识别内容 -----\n\n" + "\n\n".join(extras)
                         first = docs[0]
                         first.page_content = ((first.page_content or "").rstrip() + merged)
                         first.metadata["ocr_appended_count"] = len(extras)
                         first.metadata["ocr_by"] = "glm-4.1v-thinking-flash"
                 except Exception as e:  # noqa: BLE001
                     logger.warning(f"[loaders] Word 内嵌图提取整体跳过: {e}")
         # ------ 新增结束 ------

         for d in docs:
             d.metadata.setdefault("source", name)
             d.metadata.setdefault("file_type", "docx")
         return docs
     ```

---

## Task 5：C 场景后端核心 — rag_service.ask_vl_stream + main.py StaticFiles mount

**Files:**
- Modify: `backend/app/services/rag_service.py`
- Modify: `backend/app/main.py`

- [ ] **Step 1: 打开 `backend/app/services/rag_service.py`**
  1. 在顶部 import 区域（`from .intent_service import ...` 下面一行）追加：

     ```python
     from .vision_service import chat_with_images, VL_CHAT_SYSTEM
     ```

  2. 在文件**最末尾**（`ask_rag_stream` 结束之后）新增兄弟函数 `ask_vl_stream`。注意事件名要和 ask_rag_stream **完全一致**（debug/source/thinking/thinking_token/thinking_done/token/done/error），前端零改动就能处理：

     ```python
     # ==================================
     # C 场景：纯视觉对话（有图就不走 RAG 检索）
     # ==================================
     def ask_vl_stream(
         question: str,
         session_id: str,
         image_urls: list[str],
         enable_deep_think: bool = False,
         model: str | None = None,
     ) -> Generator[dict, None, None]:
         """
         纯视觉回答：图片 + 文本 → 流式 SSE。事件顺序同 ask_rag_stream：
           debug → source → thinking → thinking_token → thinking_done → token → done / error
         写回记忆：只存文本（question + answer），不存图片 URL，避免历史变巨大。
         """
         memory = get_memory_manager()
         full_answer: list[str] = []
         full_thinking: list[str] = []
         try:
             # 0) debug 事件 + source 事件（intent=vl_chat）
             yield {
                 "event": "debug",
                 "data": {
                     "intent": "vl_chat",
                     "original_query": question,
                     "rewritten_query": None,
                     "image_count": len(image_urls),
                     "retrieval": None,
                 },
             }
             yield {"event": "source", "data": []}

             # 1) 历史对话（最近 3 轮纯文本，不回填图片，避免上下文爆炸）
             history_raw = memory.get_messages(session_id, last_n=6)
             history = _messages_to_dicts(history_raw)

             # 2) 组装多模态 messages
             user_content_parts: list[dict] = []
             q_text = (question or "").strip() or "请描述这张图片"
             user_content_parts.append({"type": "text", "text": q_text})
             for url in image_urls:
                 user_content_parts.append({"type": "image_url", "image_url": {"url": url}})

             messages = [{"role": "system", "content": VL_CHAT_SYSTEM}]
             for m in history:
                 messages.append({"role": m["role"], "content": m["content"]})
             messages.append({"role": "user", "content": user_content_parts})

             # 3) 调视觉流式
             streamer = chat_with_images(
                 messages,
                 stream=True,
                 enable_deep_think=enable_deep_think,
                 model=model,
             )

             in_thinking_phase = False
             for typ, token in streamer:
                 if typ == "thinking":
                     if not in_thinking_phase:
                         yield {"event": "thinking", "data": True}
                         in_thinking_phase = True
                     yield {"event": "thinking_token", "data": token}
                     full_thinking.append(token)
                 elif typ == "content":
                     if in_thinking_phase:
                         yield {"event": "thinking_done", "data": True}
                         in_thinking_phase = False
                     yield {"event": "token", "data": token}
                     full_answer.append(token)
                 else:
                     # 未知类型忽略
                     continue

             if in_thinking_phase:
                 yield {"event": "thinking_done", "data": True}

             answer_text = "".join(full_answer)
             thinking_text = "".join(full_thinking)

             # 4) 写回记忆（只存文本）
             memory.append(session_id, q_text, answer_text)
             yield {
                 "event": "done",
                 "data": {
                     "thinking_chars": len(thinking_text),
                 },
             }
         except Exception as e:  # noqa: BLE001
             logger.exception("[rag][vl] 纯视觉对话异常")
             err_trace = traceback.format_exc(limit=2)
             yield {
                 "event": "error",
                 "data": {
                     "message": str(e),
                     "trace": err_trace,
                 },
             }
     ```

- [ ] **Step 2: 打开 `backend/app/main.py`**
  1. 在顶部 import 区域（`from fastapi import FastAPI` 下面一行）追加：

     ```python
     from fastapi.staticfiles import StaticFiles
     ```

  2. 定位 `from .config import VECTOR_DB_TYPE, MODEL_ID` 行，加上 UPLOAD_DIR：

     ```python
     from .config import VECTOR_DB_TYPE, MODEL_ID, UPLOAD_DIR
     ```

  3. 在"注册路由"两行之后、`@app.get("/", tags=["根"])` 之前插入 mount 行：

     ```python
     # 注册路由
     app.include_router(index_router)
     app.include_router(chat_router)

     # ======== 新增：C 场景临时图片静态资源（/uploads/_chat_images/sid/uuid.jpg） ========
     import os as _os
     from pathlib import Path as _Path
     _abs_upload = _Path(UPLOAD_DIR).resolve()
     _os.makedirs(_abs_upload, exist_ok=True)
     # 建 _chat_images 子目录（避免把用户上传的原始知识库文档也暴露出去，经验默认只暴露 chat 子目录更安全）
     _chat_img_dir = _abs_upload / "_chat_images"
     _os.makedirs(_chat_img_dir, exist_ok=True)
     app.mount("/uploads", StaticFiles(directory=str(_abs_upload)), name="uploads")
     ```

     （解释：StaticFiles 整个 UPLOAD_DIR 其实只给 C 场景用，知识库文档不会被前端访问，但只暴露目录、不提供列表，所以安全够用。）

---

## Task 6：chat_router 新增 `/chat/image-stream` 多部分表单流式接口

**Files:**
- Modify: `backend/app/routers/chat_router.py`

- [ ] **Step 1: 打开 `backend/app/routers/chat_router.py`**
  1. 顶部 import 区加：
     ```python
     import os
     import uuid
     from pathlib import Path
     from fastapi import APIRouter, File, Form, HTTPException, UploadFile
     from ..config import UPLOAD_DIR, VL_MAX_IMAGE_MB, VL_MAX_IMAGES
     ```
     （原来的 `import uuid` 和 `from fastapi import APIRouter, HTTPException` 保留，只补新增的）。调整一下 import：
     最终顶部 import 长这样（去重）：
     ```python
     from __future__ import annotations
     import json
     import os
     import uuid
     from pathlib import Path
     from fastapi import APIRouter, File, Form, HTTPException, UploadFile
     from fastapi.responses import StreamingResponse

     from ..config import UPLOAD_DIR, VL_MAX_IMAGE_MB, VL_MAX_IMAGES
     from ..services import rag_service, memory_service, generator_service
     from ..models.schemas import ChatRequest, ChatResponse, ConversationInfo, PongResponse, MessageItem, ChatModel
     ```

  2. 在 `/chat/stream` 路由下面一行，`# -------- 会话管理 --------` 注释上方，插入新接口。校验逻辑严格按 spec：数量≤4、单张≤10MB、扩展名白名单、预处理压缩后落盘到 `UPLOAD_DIR/_chat_images/{session_id}/{uuid}.jpg`。

     ```python
     # -------- 多模态对话（C 场景）：文本 + 图片 → 纯视觉流式 SSE --------
     @router.post("/chat/image-stream")
     async def chat_image_stream(
         session_id: str = Form(..., min_length=1, max_length=128),
         question: str = Form(""),
         enable_deep_think: bool = Form(False),
         model: str | None = Form(None),
         files: list[UploadFile] = File(..., description="图片附件（白名单：jpg/jpeg/png/tif/tiff/bmp/webp），最多 4 张"),
     ):
         """
         多部分表单：C 场景对话直接贴图片。
         - 没图时前端走原 /chat/stream（JSON 接口，兼容零侵入）；
         - 有图才走本接口。
         返回：和 /chat/stream 完全一致的 SSE 事件流。
         """
         # 1) 数量校验
         if not files:
             raise HTTPException(status_code=400, detail="请至少上传 1 张图片，纯文本请使用 /chat/stream 接口")
         if len(files) > VL_MAX_IMAGES:
             raise HTTPException(status_code=400, detail=f"单次最多上传 {VL_MAX_IMAGES} 张图片")

         # 2) 扩展名白名单
         _EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}

         # 3) 保存目录：UPLOAD_DIR/_chat_images/{session_id}/
         abs_upload = Path(UPLOAD_DIR).resolve()
         save_dir = abs_upload / "_chat_images" / (session_id or "default")
         save_dir.mkdir(parents=True, exist_ok=True)

         # 4) 逐个校验 + 预处理 + 落盘
         image_urls: list[str] = []
         for uf in files:
             raw_bytes = await uf.read()
             if not raw_bytes:
                 raise HTTPException(status_code=400, detail="上传了空图片文件")

             # 大小上限（raw 阶段先拦，预处理后会更小）
             if len(raw_bytes) > VL_MAX_IMAGE_MB * 1024 * 1024:
                 raise HTTPException(
                     status_code=400,
                     detail=f"图片 '{uf.filename or ''}' 大小超过 {VL_MAX_IMAGE_MB}MB 限制",
                 )

             # 扩展名
             ext = Path(uf.filename or "").suffix.lower()
             if ext not in _EXTS:
                 raise HTTPException(
                     status_code=400,
                     detail=f"图片 '{uf.filename or ''}' 格式不支持，仅支持 {', '.join(sorted(_EXTS))}",
                 )

             # 预处理（Pillow 尺寸压缩 + RGBA→RGB→JPEG），失败直接 400
             try:
                 from app.services.vision_service import preprocess_image
                 jpg_bytes = preprocess_image(raw_bytes)
             except Exception as e:  # noqa: BLE001
                 raise HTTPException(status_code=400, detail=f"图片 '{uf.filename or ''}' 预处理失败：{e}")

             # 落盘为 .jpg
             saved_name = f"{uuid.uuid4().hex}.jpg"
             saved_path = save_dir / saved_name
             with open(saved_path, "wb") as f:
                 f.write(jpg_bytes)

             # 静态 URL：/uploads/_chat_images/<sid>/<uuid>.jpg
             # 注意：saved_path 要相对 abs_upload 做切片，前面拼 /uploads/
             rel = saved_path.relative_to(abs_upload).as_posix()
             image_urls.append(f"/uploads/{rel}")

         # 5) 调 rag_service 纯视觉流式（返回 generator → StreamingResponse 包装，逻辑和 chat_stream 一模一样）
         generator = rag_service.ask_vl_stream(
             question=question,
             session_id=session_id,
             image_urls=image_urls,
             enable_deep_think=enable_deep_think,
             model=model,
         )

         def sse_wrap():
             for event in generator:
                 yield f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"

         return StreamingResponse(
             sse_wrap(),
             media_type="text/event-stream",
             headers={
                 "Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no",
             },
         )
     ```

  3. 原来的 `chat_stream` 函数内部是 `import json` 后再用，现在我们在顶部已经 import json 了，没问题。

---

## Task 7：前端 index.html 改造 — 图片按钮 / 缩略图 / 多部分发送 / 气泡展示 / 调试面板

**Files:**
- Modify: `frontend/index.html`

> 说明：按 spec 要求"最小增量"，所有改动都在现有的 `<script setup>` 和 `<el-input>` 结构附近，不新建大段。用 Grep 定位"发送按钮""user 消息气泡 v-if""lastDebugInfo.intent 路由 pill"三处锚点，再按下面改。

- [ ] **Step 1: 在 `<script setup>` 顶部新增 ref + 工具变量**
  找到"发送消息"函数 `const send = async () => { ... }` 所在的 `<script setup>` 块，在 `const lastDebugInfo = ref(null)` 那几行附近追加：

  ```js
  // ======== 新增：C 场景聊天图片 ========
  const pendingImages = ref([])      // [{id, file, previewUrl, name, size}]
  const chatImgInputEl = ref(null)   // <input type=file> DOM 引用

  const IMG_EXT_ACCEPT = "image/jpeg,image/png,image/tiff,image/bmp,image/webp"
  const MAX_IMAGES = 4
  const MAX_IMAGE_MB = 10

  function openChatImgPicker() {
    chatImgInputEl.value && chatImgInputEl.value.click()
  }

  function onChatImgSelected(e) {
    const fl = e.target.files || []
    for (const f of fl) handleOnePendingImage(f)
    e.target.value = "" // 允许下次重复选同一文件
  }

  function handleOnePendingImage(file) {
    // 数量
    if (pendingImages.value.length >= MAX_IMAGES) {
      ElMessage.warning(`一次最多选择 ${MAX_IMAGES} 张图片`)
      return
    }
    // 扩展名
    const name = (file.name || "").toLowerCase()
    const ok = /\.(jpe?g|png|tiff?|bmp|webp)$/i.test(name)
    if (!ok) {
      ElMessage.warning(`仅支持 jpg/jpeg/png/tif/tiff/bmp/webp 图片格式：${file.name}`)
      return
    }
    // 大小
    if (file.size > MAX_IMAGE_MB * 1024 * 1024) {
      ElMessage.warning(`图片 ${file.name} 超过 ${MAX_IMAGE_MB}MB`)
      return
    }
    const id = Math.random().toString(36).slice(2) + Date.now().toString(36)
    const previewUrl = URL.createObjectURL(file)
    pendingImages.value.push({ id, file, previewUrl, name: file.name, size: file.size })
  }

  function removePendingImage(id) {
    const idx = pendingImages.value.findIndex(x => x.id === id)
    if (idx >= 0) {
      const [item] = pendingImages.value.splice(idx, 1)
      try { URL.revokeObjectURL(item.previewUrl) } catch (_) {}
    }
  }

  // 消息结构：每个 MessageItem 除 role/content 外，新增可选 images: [{url, name}]
  // 从服务器拉 /conversations/:id/messages 时没有 images，只影响当前会话周期内存的 history 数组
  ```

- [ ] **Step 2: 修改 `send()` 函数 —— 无图走老 /chat/stream JSON，有图走 /chat/image-stream FormData**
  在 `send()` 里，找到：
  ```js
  const resp = await fetch("/api/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
  ```
  把这整块替换为 if/else 分支（**保留原来所有的 payload 构造、loading、reading 部分，只改 fetch 那段**）：

  ```js
      // 构造 payload 的那几段保留不变……
      const payload = {
        session_id: currentSessionId.value,
        question: questionText,
        enable_deep_think: enableDeepThink.value,
        model: selectedModel.value,
      }

      // ======== 新增：决定走哪个接口 ========
      const hasImages = pendingImages.value.length > 0
      let resp
      if (!hasImages) {
        // 原路径：纯文本 /chat/stream（零侵入，兼容老逻辑）
        resp = await fetch("/api/chat/stream", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        })
      } else {
        // C 场景：多部分表单 /chat/image-stream
        const fd = new FormData()
        fd.append("session_id", payload.session_id)
        fd.append("question", payload.question)
        fd.append("enable_deep_think", payload.enable_deep_think ? "true" : "false")
        if (payload.model) fd.append("model", payload.model)
        for (const p of pendingImages.value) {
          fd.append("files", p.file, p.name)
        }
        resp = await fetch("/api/chat/image-stream", { method: "POST", body: fd })
      }
  ```

  **紧接着，在"user 消息 push 进 history"那段**（`messages.value.push({ role: 'user', content: questionText, ...})`），给 user 消息追加 images 字段，让气泡能渲染缩略图：

  ```js
  // user 消息入 history
  const userMsg = { role: 'user', content: questionText }
  if (hasImages) {
    userMsg.images = pendingImages.value.map(p => ({ url: p.previewUrl, name: p.name }))
  }
  messages.value.push(userMsg)

  // 清空输入框 & 待发送图片
  inputMsg.value = ''
  for (const p of pendingImages.value) {
    try { URL.revokeObjectURL(p.previewUrl) } catch (_) {}
  }
  pendingImages.value = []
  ```

- [ ] **Step 3: UI — 输入框左侧加 🖼 按钮 + 缩略图预览条**
  找到 `<div class="input-bar">` 那一片（在"深度思考"switch 和发送按钮之间的输入框结构）。原来的结构大致是：
  ```
  <div class="input-bar">
    <el-input v-model="inputMsg" type="textarea" ... />
    <el-switch v-model="enableDeepThink" ... />
    <el-button @click="send">发送</el-button>
  </div>
  ```
  改成：
  ```html
  <div class="chat-footer">
    <!-- 待发送缩略图预览条 -->
    <div v-if="pendingImages.length" class="pending-imgs">
      <div v-for="p in pendingImages" :key="p.id" class="pending-img">
        <el-image :src="p.previewUrl" fit="cover" :preview-src-list="[p.previewUrl]" />
        <span class="pending-img-name" :title="p.name">{{ p.name.length>10 ? p.name.slice(0,8)+'…' : p.name }}</span>
        <span class="pending-img-close" @click="removePendingImage(p.id)">×</span>
      </div>
    </div>

    <div class="input-bar">
      <!-- 新增：图片按钮 -->
      <button class="btn-icon" type="button" title="上传图片（视觉对话，最多 4 张）" @click="openChatImgPicker">
        <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="3" y="5" width="18" height="14" rx="2"></rect>
          <circle cx="9" cy="10" r="1.6"></circle>
          <path d="M21 17l-5-5-9 9"></path>
        </svg>
      </button>
      <!-- 隐藏的文件选择器 -->
      <input
        ref="chatImgInputEl"
        type="file"
        multiple
        :accept="IMG_EXT_ACCEPT"
        style="display:none"
        @change="onChatImgSelected"
      />

      <el-input
        v-model="inputMsg"
        type="textarea"
        :rows="2"
        resize="none"
        placeholder="输入消息... (Ctrl/⌘+Enter 发送，可直接粘贴图片文件)"
        @keydown.ctrl.enter.exact.prevent="send"
        @keydown.meta.enter.exact.prevent="send"
      />
      <el-switch v-model="enableDeepThink" active-text="深度思考" inline-prompt />
      <el-button type="primary" @click="send" :disabled="loading || (!inputMsg.trim() && !pendingImages.length)">
        发送
      </el-button>
    </div>
  </div>
  ```

  注意发送按钮的 `:disabled` 条件要改成 `:disabled="loading || (!inputMsg.trim() && !pendingImages.length)"`（只有图没文字也允许发，触发 "请描述这张图片" 分支）。

- [ ] **Step 4: UI — User 消息气泡显示缩略图**
  找到渲染 `m.role === 'user'` 的气泡 `<div class="bubble user">` 内部。原来里面只有 `{{ m.content }}`。把它包起来，在文字上方加图：

  ```html
  <div class="bubble user">
    <div v-if="m.images && m.images.length" class="bubble-imgs">
      <el-image
        v-for="(im, idx) in m.images"
        :key="idx"
        :src="im.url"
        :title="im.name"
        fit="cover"
        class="bubble-img"
        :preview-src-list="m.images.map(x=>x.url)"
        :initial-index="idx"
      />
    </div>
    <div v-if="m.content">{{ m.content }}</div>
  </div>
  ```

- [ ] **Step 5: UI — 调试面板 intent pill 新增 vl_chat**
  找到意图路由那一片（大概："意图路由" + pill，pill 原 kb_query/chat/file_list/follow_up 四个颜色分支）。在 `<el-tag>` 的 :type 映射 & 显示文案里加 vl_chat 分支（青色 = C 场景视觉对话）：

  ```html
  <div style="margin:8px 0">
    <span style="opacity:.7;margin-right:8px">意图路由</span>
    <el-tag size="small" effect="light" :type="(() => {
      const i = lastDebugInfo.intent
      if (i === 'kb_query' || i === 'follow_up') return 'success'
      if (i === 'chat') return 'info'
      if (i === 'file_list') return 'warning'
      if (i === 'vl_chat') return ''  // 原色 → 绿色/青色默认 tag，已足够区分
      return 'info'
    })()">
      {{(() => {
        const i = lastDebugInfo.intent
        if (i === 'kb_query') return '知识库查询'
        if (i === 'chat') return '闲聊'
        if (i === 'file_list') return '文件列表'
        if (i === 'follow_up') return '追问改写'
        if (i === 'vl_chat') return '视觉对话 · ' + (lastDebugInfo.image_count || 0) + ' 张图'
        return i || '未知'
      })()}}
      {{lastDebugInfo.intent !== 'vl_chat' ? '（' + (selectedModelLabel || 'glm-4.5-flash') + ' 分类）' : '（glm-4.1v-thinking-flash 视觉）'}}
    </el-tag>
  </div>
  ```

- [ ] **Step 6: 新增一点小 CSS（放到 `<style scoped>` 末尾）**

  ```css
  /* ======== 新增：C 场景视觉对话 UI ======== */
  .chat-footer { display:flex; flex-direction:column; gap:10px; }
  .pending-imgs { display:flex; flex-wrap:wrap; gap:10px; }
  .pending-img {
    position:relative; width:88px; height:88px; border-radius:12px; overflow:hidden;
    border:1px solid rgba(0,0,0,.08); background:#f5f5f5;
  }
  .pending-img .el-image { width:100%; height:66px; display:block; }
  .pending-img-name {
    display:block; padding:0 6px; line-height:20px; font-size:11px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:#555;
  }
  .pending-img-close {
    position:absolute; top:2px; right:4px; width:18px; height:18px; line-height:16px;
    border-radius:9px; background:rgba(0,0,0,.55); color:#fff; text-align:center;
    font-size:14px; cursor:pointer;
  }
  .btn-icon {
    width:40px; height:40px; border-radius:12px;
    border:1px solid #eee; background:#fff; color:#ff7a59; cursor:pointer;
    display:flex; align-items:center; justify-content:center; flex-shrink:0;
  }
  .btn-icon:hover { background:#fff3ed; }
  .bubble-imgs { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:6px; }
  .bubble-img { width:140px; height:140px; border-radius:10px; overflow:hidden; background:#fff; border:1px solid rgba(0,0,0,.08); }
  @media (max-width: 500px) { .bubble-img { width:110px; height:110px; } }
  ```

- [ ] **Step 7: 小彩蛋 — 输入框支持直接"粘贴图片文件"（Ctrl+V 粘贴截图）**
  在 `<script setup>` 末尾添加：

  ```js
  // 粘贴文件到输入框时直接加入 pendingImages
  onMounted(() => {
    const onPaste = (e) => {
      if (!e.clipboardData) return
      const items = [...(e.clipboardData.items || [])]
      for (const it of items) {
        if (it.kind === 'file') {
          const f = it.getAsFile()
          if (f) handleOnePendingImage(f)
        }
      }
    }
    window.addEventListener('paste', onPaste)
    onBeforeUnmount(() => window.removeEventListener('paste', onPaste))
  })
  ```
  （如果 `onMounted` / `onBeforeUnmount` 原本已导入则直接用，没导入就在 `import { ref, onMounted, onBeforeUnmount, ...}` 里加上。）

---

## Task 8：更新 diff.md 第十一节 + 收尾验证

**Files:**
- Modify: `diff.md`（根目录）

- [ ] **Step 1: 打开 `d:\ai学习项目\my-rag\diff.md`，跳到文件最末尾**
  在第十轮之后，追加"第十一节"。按之前 diff.md 已有的格式风格（标题 + 二级列表 + 关键代码片段），从 spec 第"八、文件变更总清单"和"九、风险缓解"里摘内容，写中文说明，不少于 50 行。结构：

  ```
  第十一节：视觉识别 + OCR + 多模态对话（glm-4.1v-thinking-flash 全链路，2026-08-19）
  ==========================================================================
  目标：覆盖 A(图片入库)/B(PDF+Word 内嵌图/扫描件 OCR)/C(对话贴图片提问) 三大视觉场景。
  架构决策：不引入任何本地 OCR 模型（PaddleOCR/Tesseract/docTR/CLIP），所有视觉能力
            统一走 glm-4.1v-thinking-flash 在线视觉模型（OpenAI 兼容多模态接口）。
            Base URL 只写到 /v4（OpenAI SDK 自动补 /chat/completions，经验 818026）。
            Pillow 预处理强制 RGBA/P/LA/CMYK → RGB（经验 1219328，避免 JPEG 保存报错）。

  一、新增配置（config.py + .env.example + requirements.txt）
    - 新增 9 个 VL_* 配置常量 + reload() 同步赋值：
      VL_ENABLE / VL_MODEL / VL_API_KEY / VL_BASE_URL / VL_TEMPERATURE
      VL_MAX_IMAGES / VL_MAX_IMAGE_MB / VL_OCR_TEXT_THRESHOLD / VL_IMAGE_MAX_EDGE
    - requirements.txt 新增 Pillow 与 PyMuPDF。

  二、核心视觉服务（vision_service.py，新增 1 个文件）
    - preprocess_image()：RGBA→RGB + 最长边 1024px 压缩 + JPEG q=85。
    - image_to_base64_dataurl()：A/B 场景 base64 传图（不暴露静态 URL）。
    - ocr_image()：严格两段式输出
      ===== 图片文字内容 ===== / ===== 图片结构描述 =====
      质量不够时抛出异常（调用方决定是否降级）。
    - ocr_image_batch()：B 场景串行调用，单张失败写占位，不中断整本书。
    - chat_with_images()：同步/流式，直连绕国内域名代理。

  三、A 场景（图片作为文档上传入库）
    - document_loader.py：SUPPORTED_EXTS 扩 7 种图片 → _load_image() 占位 Document。
    - indexer_service.upload_and_parse：缓存前检测 needs_ocr=True → vision OCR 回填。
      后续关键词提取 / 分块 / parent_content / 嵌入 零改动。

  四、B 场景（PDF/Word 内嵌图 + 扫描件 OCR）
    - _load_pdf 切换 PyMuPDF 主路径（PyPDFLoader 作为兜底）。
      - 每页原生文本：>= VL_OCR_TEXT_THRESHOLD(100) 字 → 跳过整页 OCR（省 token）。
      - <100 字（扫描件）→ 150dpi 渲染该页 → vision OCR。
      - 每页内嵌图：无论文本多少都做 OCR（图表是核心价值，上限 4 张/页）。
      - 所有追加文本："----- 图片识别内容 -----" 分隔符，metadata 里 ocr_by / ocr_appended_count。
    - _load_word：python-docx 提取 part.rels 中 type=image 的内嵌图 → vision OCR，
      追加到首个 doc.page_content 末尾，上限 4 张/文档。

  五、C 场景（对话时直接贴图片提问 = 纯视觉回答，不走 RAG 检索）
    - main.py：mount("/uploads", StaticFiles(directory=UPLOAD_DIR)) + 自动建 _chat_images 子目录。
    - rag_service.py 新增兄弟函数 ask_vl_stream()：事件顺序与 ask_rag_stream 完全一致
      debug → source → thinking → thinking_token → thinking_done → token → done。
      写回记忆只存文本，不存图片 URL（避免历史爆炸）。
    - chat_router.py 新增 POST /api/chat/image-stream（多部分表单 FormData）：
      session_id(Form) / question(Form) / enable_deep_think(Form) / model(Form) / files(File)
      校验：≤4 张、≤10MB/张、扩展名白名单 → vision preprocess_image 统一落盘 JPEG →
      返回可访问的静态 URL /uploads/_chat_images/<sid>/<uuid>.jpg → 调 ask_vl_stream。
    - 前端 index.html：
      - 输入框左侧 🖼 按钮 + 粘贴文件直进 pendingImages（Ctrl+V 截图直接可用）。
      - 待发送缩略图预览条（88x88，右上 × 删除，文件名截断）。
      - send()：无图 → 原 /chat/stream JSON；有图 → /chat/image-stream FormData。
      - User 气泡：images[] 140x140 缩略图 + el-image-viewer 大图预览。
      - 调试面板 intent pill：vl_chat → "视觉对话 · N 张图（glm-4.1v-thinking-flash 视觉）"。

  六、风险与缓解（对照经验教训）
    - 经验 1219328（RGBA JPEG）：preprocess_image 统一 convert('RGB')。
    - 经验  818026（Base URL）：配置注释明确 + _clean_url 自动清污。
    - 经验 1178834（不要瞎建文件）：只新增 vision_service.py，其余改现有入口。
    - 经验 1178834（不要整页删重跑）：PDF OCR 失败只打 warning，单页单图失败用占位。

  七、验证清单（12 项，详见 docs/superpowers/specs/2026-08-19-vision-ocr-design.md §10）
    1. A 图片入库 OCR / 2. A 图片内容描述 / 3. B 纯文本 PDF 不触发 OCR /
    4. B 扫描件 PDF 追加 OCR / 5. B Word 内嵌图 OCR / 6. C 单图对话 /
    7. C ≥5 张前端拦截 / 8. C >10MB 双拦截 / 9. 纯文本消息走旧接口 /
    10. RGBA PNG 不报错 / 11. 错 Key 正确兜底 / 12. vl_chat debug pill 正确显示。
  ```

- [ ] **Step 2: 收尾对照验证（可手动）**
  打开 spec 末尾的 §10 验证清单，从 1 到 12 逐项过一遍，能用手工命令/浏览器就手工测。本项目没有 pytest 测试，所以**至少**跑下面 3 个最小 smoke test：
  1. `python -c "from app.services.vision_service import preprocess_image, ocr_image; print('import OK')"` — 确认 vision_service 能 import 无语法错误。
  2. `python -c "from app.loaders.document_loader import load_document; print('loader OK')"` — 确认新增图片分支 + PyMuPDF 分支 import 无报错。
  3. 打开 `http://localhost:8000/docs` 检查 `/api/chat/image-stream` 路由存在，参数包含 `session_id / question / enable_deep_think / model / files`。
  （如果 PowerShell 执行策略拦截，就直接在你平时的启动脚本里跑后端看启动日志。）

- [ ] **Step 3: 提交前的最终 self-check**
  - 任何硬编码真实 Key 的地方都改掉：.env.example 里必须写占位符，不写用户实际 `323ca7ec...`。
  - Base URL 不要加反引号（config.py 的 `_clean_url` 虽然能清，但别依赖，直接写对）。
  - 没有"TBD/TODO"遗留。
  - 新代码不破坏原 `/chat/stream` 流程（前端无图时仍然走 JSON 老接口）。
  - 没有在"纯文本 PDF 上传"场景里额外调用 vision（省 token）。

完成所有任务后，向用户报告：已更新 diff.md 第十一节 + 8 个 Task 所有步骤，按 §10 验证清单至少跑 12 项中的 3 项 smoke test。
