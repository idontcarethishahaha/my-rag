# MyRag AI — 视觉 / OCR / 多模态对话 设计文档（第十一轮）

生成时间：2026-08-19

参考：`D:\ai学习项目\RAG-Pro\docs\feature-comparison.md`（OCR / 表格识别 / 图片提取改进建议）；经验 ID 1178834（不要擅自引入大量本地模型）、1219328（RGBA→JPEG 必转）。

---

## 一、目标与范围

### 1.1 解决的问题（三个场景，用户已锁定为 A+B+C）

| 代号 | 场景 | 用户故事 |
|---|---|---|
| **A** | **图片作为文档上传入库** | 我有一张发票截图 / 课程笔记拍照 / 产品海报图，上传到知识库后，能在后续提问中检索到图片里的文字、表格、内容描述 |
| **B** | **PDF/Word 内嵌图片 + 扫描件 OCR** | 我上传的 PDF 是扫描件（或带内嵌图表/流程图），纯文本解析器（PyPDF）抓不到文字，需要视觉模型识别补充 |
| **C** | **对话时直接贴图片提问** | 我不想入库，就想临时发一张图片（机票、表格、结构图）+ 一句话给 AI 直接看，让它基于图片内容回答 |

### 1.2 明确的非目标（本期不做）

- ❌ **D 场景**：图片语义检索（以图搜图 / 以图搜文）——本期不做 CLIP 图像向量，A 场景是把图片 OCR 成纯文本后用文本向量检索，后续要 D 场景再加一层 CLIP
- ❌ 本地 PaddleOCR / Tesseract / docTR / CLIP 安装——**零本地模型依赖**，所有视觉任务统一走 `glm-4.1v-thinking-flash`（用户给定）
- ❌ 聊天图片和 RAG 的混合模式（例如"图片+文本+检索"）——本期 C 场景**有图就纯视觉回答，不走检索**；后续版本可叠加
- ❌ 视频 / 多页 TIFF / GIF 动态图——只处理静态图片

### 1.3 关键约束（来自用户确认）

| 约束项 | 取值 | 说明 |
|---|---|---|
| 视觉模型 | `glm-4.1v-thinking-flash` | API Key、Base URL 用户已给，走 OpenAI 兼容接口（ChatCompletion 多模态格式） |
| B 场景扫描件判定阈值 | 该页提取纯文本 **< 100 字** 才做 OCR | 避免纯文本 PDF 每页都调 vision 浪费 token |
| C 场景回答模式 | 有图 = 纯视觉回答，不走 RAG 检索 | 降低复杂度，后续可叠加 |
| 单张图片大小上限 | **10MB** | 前端 + 后端双校验 |
| 单次最多上传图片张数 | **4 张**（A/C 场景通用） | 避免 payload 过大 |

---

## 二、总体架构

```
                            ┌──────────────────────────────────────┐
                            │        Vision Service (新增)         │
                            │  后端统一"图片→文本/理解"入口        │
                            │  ┌─ ocr_image(image) → 结构化文本    │
                            │  ├─ understand(messages) → 流式回答   │
                            │  └─ 图片预处理: 尺寸压缩 / RGBA→JPEG  │
                            └──────────────┬───────────────────────┘
                                           │ glm-4.1v-thinking-flash
                                           ▼
┌──────────────┐     ┌───────────────────────────────────────┐
│  A 场景      │────▶│  Indexer Pipeline (改现有入口)         │
│  图片上传    │     │  upload_and_parse: 图片→vision OCR    │
│  入库        │     │                    → 文本回填 Document │
└──────────────┘     │  chunk_and_store: 走原有(分块/关键词/  │
                     │                     parent_content/嵌入)│
                     └───────────────────────────────────────┘

┌──────────────┐     ┌───────────────────────────────────────┐
│  B 场景      │────▶│  document_loader 扩展                 │
│  PDF/Word    │     │  _load_pdf:  PyMuPDF 渲染每一页 →     │
│  内嵌图+扫   │     │             文本少则 OCR 该页补文本     │
│  描件        │     │  _load_word: python-docx 提内嵌图      │
│              │     │             → OCR → 追加到 doc 末尾    │
└──────────────┘     └───────────────────────────────────────┘

┌──────────────┐     ┌───────────────────────────────────────┐
│  C 场景      │────▶│  Chat Router + Frontend (新增/改造)    │
│  对话贴图片  │     │  后端: 新增 /chat/image-stream 表单    │
│              │     │        → 静态 /uploads/_chat_images   │
│              │     │        → vision.understand 流式推送     │
│              │     │  前端: 输入框左侧图片按钮 + 缩略图气泡  │
└──────────────┘     └───────────────────────────────────────┘
```

**关键设计原则**（来自经验 1178834）：
- **最小增量 + 先改现有入口**：不新建一堆独立服务，优先在现有 `upload_and_parse`、`_load_pdf`、`chat_router` 里做扩展
- 新增代码只做一件事：`vision_service.py` 只负责"图片 → glm-4.1v"，不混入任何索引/分块/路由逻辑

---

## 三、视觉模型配置与接入规范

### 3.1 新增配置项（`backend/app/config.py` + `.env.example`）

统一放在 `# ================================== # 视觉模型（VL）/ OCR 配置` 区块：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `VL_ENABLE` | `true` | 全局开关：关掉后 A/B/C 三场景都会回退（图片直接报错"视觉功能未启用"） |
| `VL_MODEL` | `glm-4.1v-thinking-flash` | 可切换 |
| `VL_API_KEY` | `""` | 用户给定的 key（`323ca7ec...gFp2cFuAXke5ifib`） |
| `VL_BASE_URL` | `https://open.bigmodel.cn/api/paas/v4` | 用户给定（注意：末尾不要 `/chat/completions`，**Base URL 只写到 `/v4`**）——经验 ID 818026 强调 Base URL 拼接问题 |
| `VL_TEMPERATURE` | `0.0` | OCR 时用 0，C 场景聊天可 override 为 0.3 |
| `VL_MAX_IMAGES` | `4` | 单请求最大图片张数（A/C 通用） |
| `VL_MAX_IMAGE_MB` | `10` | 单张图片大小上限（MB） |
| `VL_OCR_TEXT_THRESHOLD` | `100` | B 场景 PDF 每页原文本 < 此数 → 做 OCR |
| `VL_IMAGE_MAX_EDGE` | `1024` | 图片最长边压缩到此像素（省 token），≤0 表示不压缩 |

`config.reload()` 同步新增 9 个变量。

### 3.2 glm-4.1v 请求体规范（OpenAI 兼容）

**经验教训（ID 1178834）：先写最小可用调用规范再写代码，别硬写域名。**

智谱 glm-4.1v-thinking-flash 兼容 OpenAI 多模态 messages 格式：

```python
# C 场景：聊天 + 多图
messages = [
    {"role": "system", "content": SYSTEM_PROMPT_RAG},
    {"role": "user", "content": [
        {"type": "text", "text": "这张发票帮我看看开票日期和金额"},
        {"type": "image_url", "image_url": {"url": "https://host/_chat_images/x/a.jpg"}},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ..."}},  # A/B 场景优先用 base64
    ]},
]
```

两种传图方式：
| 场景 | 传图方式 | 原因 |
|---|---|---|
| A/B（索引期，图片在服务器本地） | **base64**（前缀 `data:image/jpeg;base64,`） | 不需要暴露静态 URL，流程更稳 |
| C（对话期，用户临时上传） | **URL**（`/uploads/_chat_images/sid/xxx.jpg`，由 StaticFiles 暴露） | 避免每次流式 SSE 都把 base64 字符串打到日志里 |

---

## 四、Vision Service 详细设计（新增 `backend/app/services/vision_service.py`）

### 4.1 对外接口

```python
def preprocess_image(image_bytes: bytes, max_edge: int = 1024) -> bytes:
    """
    图片预处理：
      1. Pillow 读取（自动识别格式）
      2. RGBA/P/LA → RGB 强制转换（经验 1219328：存 JPEG 前必转，否则 "cannot write mode RGBA as JPEG"）
      3. 最长边等比缩放到 ≤ max_edge（默认 1024px）
      4. 输出 JPEG bytes（质量 85）
    返回压缩后的 JPEG bytes。失败（坏图）抛出 ValueError。
    """

def image_to_base64_dataurl(image_bytes: bytes, max_edge: int = 1024) -> str:
    """预处理 + 转 base64 data URL：'data:image/jpeg;base64,/9j/...'"""

def ocr_image(image_bytes: bytes, hint: str = "") -> str:
    """
    A/B 场景：对一张图片做 OCR + 内容理解，返回结构化纯文本。
    输出格式（严格）：
      ===== 图片文字内容 =====
      <OCR 原文，按行换行>
      ===== 图片结构描述 =====
      <1-2 段话：图表类型/画面说明/版式布局（如果是流程图、发票、表格、海报要特别说明主体）>
    hint 是可选的上下文（如 "这是 PDF 第 3 页的内嵌图"）。
    失败 → 抛出异常（调用方自己降级，不吞错）。
    """

def ocr_image_batch(image_bytes_list: list[bytes], hints: list[str] | None = None) -> list[str]:
    """
    批次版（B 场景 PDF 多页内嵌图会用到）。
    内部串行调用 glm-4.1v，每张独立请求——避免多图混在一起 OCR 质量下降。
    """

def chat_with_images(
    messages: list[dict],   # 已经拼好 image_url 的多模态 messages
    stream: bool = False,
    enable_deep_think: bool = False,
    model: str | None = None,
):
    """
    C 场景：多模态对话。
    stream=False → 返回 (content_text, thinking_text)
    stream=True  → Generator[(type, token)], type ∈ {"thinking", "content"}
    注意：和 generator_service.chat / chat_stream 结构保持一致，但底层走 VL 模型。
    """
```

### 4.2 Prompt（内置在模块顶部常量，不要散着写）

- **OCR_PROMPT**：`你是一个高精度OCR和图片内容助手...严格输出“===== 图片文字内容 =====\\n<文字>\\n===== 图片结构描述 =====\\n<描述>”两段式。不要编造未看到的内容。如果是表格保留行列对齐感，用 \| 分隔...`
- **C 场景 system prompt**：直接复用现有 `SYSTEM_PROMPT_RAG`，但把"根据知识库回答"那段去掉（因为纯视觉回答，不走检索）——在 rag_service 里走新分支时用独立 prompt。

### 4.3 错误处理（必须打埋点日志，经验 1219328）

任何 vision 调用日志都记：
- 输入图片字节数（压缩前后）
- 请求开始/结束时间
- 上游 HTTP 状态码 + 错误体前 200 字符

失败不吞异常：调用方（A/B/C）要决定如何降级。A 场景 vision 失败 = 直接告诉用户"无法识别图片内容"，不要让空文本入库。

---

## 五、A 场景：图片作为文档上传入库

### 5.1 修改点 1：`backend/app/loaders/document_loader.py`

- `SUPPORTED_EXTS` 扩展图片扩展名：
  ```
  .jpg, .jpeg, .png, .tif, .tiff, .bmp, .webp
  ```
- 新增 `_load_image(file_path, name)`：
  - **不实际做 OCR**（load_document 要保持纯文本解析轻量），只返回一个**占位 Document**：
    ```python
    Document(
        page_content="",  # 空，后续 upload_and_parse 回填
        metadata={
            "source": name,
            "file_type": "image",
            "image_path": file_path,  # vision_service 后面读这个字段
            "needs_ocr": True,
        }
    )
    ```
- `load_document()` 尾部新增图片分支路由到 `_load_image`。

### 5.2 修改点 2：`backend/app/services/indexer_service.py` 的 `upload_and_parse`

保持原有"保存→load_document→缓存"流程，但在缓存前加一步**图片 OCR 回填**：

```python
docs = load_document(save_path, file_name=filename)
# --- 新增：图片类文档 → 走 vision OCR ---
for d in docs:
    if d.metadata.get("needs_ocr"):
        img_path = d.metadata.get("image_path") or save_path
        with open(img_path, "rb") as f:
            img_bytes = f.read()
        ocr_text = vision_service.ocr_image(img_bytes, hint=f"这是作为知识库文档上传的图片: {filename}")
        d.page_content = ocr_text
        d.metadata.pop("needs_ocr", None)
        d.metadata["ocr_by"] = "glm-4.1v-thinking-flash"
# --- 新增结束 ---
if not any(d.page_content.strip() for d in docs):
    raise RuntimeError("文件内容为空或无法解析")
_parsed_docs[file_id] = docs
```

后续 `chunk_and_store` **零改动**：因为 page_content 已经是 OCR 出来的纯文本，关键词提取/分块/嵌入/parent_content 全部走现有逻辑。

### 5.3 前端文件列表：图片文件显示专门 tag

`file_ext === '.jpg'...` 时在文件卡片右上角显示"**图片·OCR**"小 tag 即可，不新增 UI 区块。

---

## 六、B 场景：PDF/Word 内嵌图片 + 扫描件 OCR

### 6.1 依赖新增（`backend/requirements.txt`）

- `pymupdf>=1.24.0`（原名 fitz：渲染 PDF 每页为图片 + 提取内嵌图 + 提取每页文本）
- **注**：保留原来的 `pypdf`（PyPDFLoader），但 `_load_pdf` 切换到 PyMuPDF 主路径，PyPDF 作为兜底

### 6.2 `_load_pdf` 重写

**旧实现**：PyPDFLoader（只抓文本，不处理扫描件和内嵌图）

**新实现**（pymupdf 主路径）：

```python
def _load_pdf(file_path: str, name: str) -> list[Document]:
    import fitz  # pymupdf
    doc = fitz.open(file_path)
    docs: list[Document] = []
    ocr_hints: list[tuple[int, bytes, str]] = []  # (page_index, img_bytes, hint)

    # 第一轮：提取每页文本 + 收集需要 OCR 的图片
    for page_idx, page in enumerate(doc):
        raw_text = page.get_text("text") or ""
        # 收集每页内嵌图（xobjects + 真正 draw_image 的图）
        embedded_images: list[bytes] = []
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            try:
                base = doc.extract_image(xref)
                embedded_images.append(base["image"])
            except Exception:
                pass

        # 判定：该页是否需要 OCR（整页渲染为图片做 OCR）
        need_page_ocr = VL_ENABLE and len(raw_text.strip()) < VL_OCR_TEXT_THRESHOLD
        page_img_bytes: bytes | None = None
        if need_page_ocr:
            pix = page.get_pixmap(dpi=150)
            page_img_bytes = pix.tobytes("jpeg")

        final_text = raw_text
        ocr_blocks: list[str] = []

        # 排队整页 OCR
        if page_img_bytes:
            ocr_hints.append((
                page_idx, page_img_bytes,
                f"这是 PDF 第 {page_idx + 1} 页的整页扫描件 OCR，请识别全部文字和表格。"
            ))

        # 排队内嵌图 OCR（无论文本多少都做：图表是核心价值）
        for i, emb in enumerate(embedded_images):
            if i >= VL_MAX_IMAGES:  # 每页最多少于 VL_MAX_IMAGES 张，防止爆炸
                break
            ocr_hints.append((
                page_idx, emb,
                f"这是 PDF 第 {page_idx + 1} 页中第 {i + 1} 张内嵌图片，可能是图表/流程图/截图，请做 OCR 并描述结构。"
            ))

        docs.append(Document(
            page_content=final_text,
            metadata={
                "source": name,
                "file_type": "pdf",
                "page": page_idx + 1,
                "_pending_ocr_slots": [],  # 占位，vision 返回后按顺序填到 _append_ocr_texts
            }
        ))

    # 第二轮：批量做 vision OCR（串行，每页独立请求）
    if ocr_hints and VL_ENABLE:
        from .vision_service import ocr_image
        # 先记录每个 slot 归属于哪个 page_index
        page_to_extra_texts: dict[int, list[str]] = {i: [] for i in range(len(docs))}
        for page_idx, img_bytes, hint in ocr_hints:
            try:
                ocr_text = ocr_image(img_bytes, hint=hint)
                page_to_extra_texts[page_idx].append(ocr_text)
            except Exception as e:
                logger.warning(f"[vision] PDF p{page_idx+1} OCR 失败，跳过: {e}")

        # 回填：把 OCR 文本追加到对应页
        for page_idx, extras in page_to_extra_texts.items():
            if not extras:
                continue
            merged = "\n\n".join(extras)
            old = docs[page_idx].page_content or ""
            docs[page_idx].page_content = (old.rstrip() + "\n\n----- 图片识别内容 -----\n\n" + merged)
            docs[page_idx].metadata["ocr_appended_count"] = len(extras)
            docs[page_idx].metadata["ocr_by"] = "glm-4.1v-thinking-flash"

    doc.close()
    return docs
```

**关键细节**：
- **OCR 与文本提取分两轮**：先把所有页+图入队，再在一个统一的 `try/except` 循环里串行调 vision，这样任何单页 OCR 失败不会导致整个 PDF 加载失败（经验 1178834：定位问题→只补缺，不要整页全删重跑）
- `ocr_hints` 的提示信息里**明确写"这是第几页的什么图"**，提升 glm-4.1v 输出质量
- 追加文本用 `----- 图片识别内容 -----` 分隔符，方便后续 chunk 里看到哪些部分是 OCR 的

### 6.3 `_load_word` 内嵌图扩展

现有 `Docx2txtLoader` 只抓文本。新增：

```python
def _load_word(file_path: str, name: str) -> list[Document]:
    from docx import Document as DocxDoc
    # 先跑原 Docx2txtLoader 拿主文本（保持兼容）
    from langchain_community.document_loaders import Docx2txtLoader
    loader = Docx2txtLoader(file_path)
    docs = loader.load()

    # 新增：提取内嵌图片 OCR 追加到首条 doc 末尾
    if VL_ENABLE:
        try:
            dxd = DocxDoc(file_path)
            extras = []
            for rel in dxd.part.rels.values():
                if "image" not in rel.reltype:
                    continue
                try:
                    img_bytes = rel.target_part.blob
                    if len(img_bytes) < 100:
                        continue
                    from .vision_service import ocr_image
                    extras.append(ocr_image(img_bytes, hint="Word 文档中的内嵌图片"))
                except Exception as e:
                    logger.warning(f"[vision] Word 内嵌图 OCR 失败: {e}")
            if extras and docs:
                merged = "\n\n----- 图片识别内容 -----\n\n" + "\n\n".join(extras)
                docs[0].page_content = (docs[0].page_content or "").rstrip() + merged
                docs[0].metadata["ocr_appended_count"] = len(extras)
                docs[0].metadata["ocr_by"] = "glm-4.1v-thinking-flash"
        except Exception as e:
            logger.warning(f"[vision] Word 内嵌图提取跳过: {e}")

    for d in docs:
        d.metadata.setdefault("source", name)
        d.metadata.setdefault("file_type", "docx")
    return docs
```

### 6.4 失败兜底

如果 `pymupdf` 没装或抛异常 → 退回原来的 PyPDFLoader；如果 `DocxDoc` 内嵌图提取失败 → 不影响主文本加载，只打 warning。

---

## 七、C 场景：对话时直接贴图片提问（纯视觉回答）

### 7.1 后端路由

新增文件 **不要新建**，直接在 `backend/app/routers/chat_router.py` 上加一条：

```python
@router.post("/chat/image-stream")
async def chat_image_stream(
    session_id: str = Form(...),
    question: str = Form(""),
    enable_deep_think: bool = Form(False),
    model: str | None = Form(None),
    files: list[UploadFile] = File(..., description="图片附件，最多 4 张"),
):
    """
    多模态对话：文本 + 图片 → 纯视觉回答（不走 RAG 检索）。
    返回 SSE 事件流（和 /chat 完全一致：debug/source/thinking/thinking_token/thinking_done/token/done/error）。
    """
    # 校验：文件数量、大小、扩展名
    # 保存到 UPLOAD_DIR/_chat_images/{session_id}/{uuid}.jpg（预处理压缩后存）
    # 生成静态 URL：/uploads/_chat_images/sid/uuid.jpg
    # → 调 rag_service.ask_vl_stream(session_id, question, image_urls, enable_deep_think, model)
    # → SSE 流式返回（EventSourceResponse）
```

路由内的具体校验：
- 图片数 ≤ `VL_MAX_IMAGES`（默认 4）
- 单张 ≤ `VL_MAX_IMAGE_MB * 1024 * 1024` bytes
- 扩展名必须在图片白名单里

### 7.2 静态资源暴露（`backend/app/main.py`）

`mount` 一条：

```python
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
```

让 `d:\ai学习项目\my-rag\backend\uploads\_chat_images\xxx.jpg` 能通过 `https://host:8000/uploads/_chat_images/xxx.jpg` 访问。

### 7.3 `rag_service.py` 新增 `ask_vl_stream`

**不要修改现有 `ask_rag` / `ask_rag_stream`**，新增兄弟函数：

```python
def ask_vl_stream(
    question: str,
    session_id: str,
    image_urls: list[str],
    enable_deep_think: bool = False,
    model: str | None = None,
) -> Generator[dict, None, None]:
    """纯视觉回答：有图就不走检索。事件顺序同 ask_rag_stream：debug/source/thinking/thinking_token/thinking_done/token/done/error"""
    # SSE 调试：debug 事件带 intent="vl_chat"
    yield {"event": "debug", "data": {
        "intent": "vl_chat",
        "original_query": question,
        "rewritten_query": None,
        "image_count": len(image_urls),
        "retrieval": None,
    }}
    yield {"event": "source", "data": []}
    # 组装多模态 messages：SYSTEM_PROMPT_RAG 去掉"根据知识库回答"部分（或者用新的 VL_CHAT_SYSTEM）
    messages = [
        {"role": "system", "content": VL_CHAT_SYSTEM},
        # history[-6:]（和普通对话一样的最近 3 轮，但只写纯文本，不回填历史图片）
        {"role": "user", "content": [
            {"type": "text", "text": question or "请描述这张图片"},
            *[{"type": "image_url", "image_url": {"url": u}} for u in image_urls],
        ]},
    ]
    # 调 vision_service.chat_with_images(stream=True, enable_deep_think=enable_deep_think)
    # → thinking_token / content token / thinking_done / done 事件按原来的顺序推
    # → 写回 memory（只存 question 文本 + answer 文本，不存图片 URL，避免历史变巨大）
```

注意：写回记忆**不存图片 URL**，只存文字；如果下一轮用户追问，LLM 看不到上一轮的图片（合理：纯视觉对话上下文太吃 token）。

### 7.4 前端改造：`frontend/index.html`

**原则：在现有输入框结构上小改，不新建大段 HTML。**

#### 7.4.1 输入框左侧加图片小按钮

```
[🖼] [  输入消息...  ] [深度思考开关] [发送]
```

点击按钮 → 触发隐藏的 `<input type="file" multiple accept="image/*">`。

#### 7.4.2 待发送图片的缩略图预览条

在输入框上方显示一行小条：
- 1~4 张缩略图（圆角 + `×` 可删除）
- 超过 4 张提示"最多 4 张"，前端直接拦截，不等到后端报错

#### 7.4.3 发送：表单多部分上传

原来 `send()` 是 `POST /api/chat/stream`（JSON body）。现在改成：
- **没带图片** → 继续走老的 JSON `/chat/stream`（零侵入，老行为不变）
- **带了图片** → 走新的 `multipart/form-data` `/chat/image-stream`，字段名与 7.1 路由一致：`session_id`、`question`、`enable_deep_think`、`model`、`files`（多文件）

#### 7.4.4 User 消息气泡里显示缩略图

`m.role === 'user'` 时，如果消息有 `images: [{url, name}]` 字段（发消息时写入消息对象），在文本上方以 flex-wrap 网格展示缩略图（点击缩略图弹大图预览用 el-image-viewer）。

#### 7.4.5 调试面板

新增 `intent: "vl_chat"` 映射为 **"视觉对话"（青色 pill）**，并显示图片张数。

---

## 八、文件变更总清单

### 新增文件（2 个，遵守"最小增量"原则，不滥建文件）

| 文件 | 职责 |
|---|---|
| `backend/app/services/vision_service.py` | 图片预处理 / base64 / OCR / 多模态对话，统一 glm-4.1v 接入 |

### 修改文件（8 个，都是现有入口小改）

| 文件 | 改动点 |
|---|---|
| `backend/app/config.py` | 新增 VL_* 配置 + reload 同步 |
| `backend/.env.example` | 视觉模型配置区块（用户 key 示例用占位符，不硬写） |
| `backend/requirements.txt` | 加 `pymupdf`；确认 `Pillow` 存在（若没就补上） |
| `backend/app/loaders/document_loader.py` | SUPPORTED_EXTS 扩图片；新增 `_load_image`；重写 `_load_pdf`（PyMuPDF 主路径）；扩展 `_load_word`（内嵌图 OCR） |
| `backend/app/services/indexer_service.py` | upload_and_parse 中图片文档做 vision OCR 回填 page_content |
| `backend/app/services/rag_service.py` | 新增 `ask_vl_stream`（纯视觉流式 SSE，兄弟函数，不污染原 RAG）+ `VL_CHAT_SYSTEM` prompt |
| `backend/app/routers/chat_router.py` | 新增 `/chat/image-stream` 多部分表单接口 |
| `backend/app/main.py` | mount `/uploads` StaticFiles |
| `frontend/index.html` | 输入框加图片按钮 + 缩略图预览 + 多部分发送 + 气泡图片展示 + debug intent='vl_chat' |

### diff.md 新增第十一节

本轮完成后在 [diff.md](file:///d:/ai学习项目/my-rag/diff.md) 尾部追加 `第十一轮修改：视觉识别 + OCR + 多模态对话（glm-4.1v-thinking-flash 全链路）`。

---

## 九、风险缓解（经验教训映射）

| # | 风险 | 来源经验 | 缓解 |
|---|---|---|---|
| 1 | RGBA/PNG 转 JPEG 报错 "cannot write mode RGBA as JPEG" | 1219328 | `preprocess_image()` 统一 `if img.mode != 'RGB': img = img.convert('RGB')`，再存 JPEG |
| 2 | Base URL 写错（尾加 `/chat/completions`）导致 404 | 818026 | 配置注释明确：Base URL 截止到 `/v4` |
| 3 | 一次性引入 PaddleOCR/Tesseract 大量依赖后安装失败 | 1178834 | **明确本期不引入**，全链路 glm-4.1v 在线 |
| 4 | OCR 时"硬改"整个加载器导致文本 PDF 也走一遍 vision | 1219328 + 本 spec §6.2 | 文本≥100 字跳过 OCR；失败打 warning 不中断主流程 |
| 5 | 大尺寸图片 payload 过大导致上游超时/限流 | 1219328 | 最长边压到 1024px + JPEG q=85 + 日志埋点"输入体积/请求耗时/响应码" |
| 6 | 新增接口破坏原 `/chat/stream` 兼容性 | — | C 场景新路由 `/chat/image-stream`，没带图片时前端继续走旧接口 |

---

## 十、验证清单（实施完后逐项过）

重启后端 + 强刷前端：

| # | 场景 | 操作 | 预期结果 |
|---|---|---|---|
| 1 | A 图片入库 | 上传一张带文字的 PNG 截图 → 选递归分块→执行→问图里的关键词 | debug 面板 rewritten_query 正常；回答引用了图里的文字；来源块内容是 OCR 文本 |
| 2 | A 图片内容描述 | 上传一张流程图 / 海报图 → 问"这张图讲了什么" | 回答内容里有"图片结构描述"部分，不是只 OCR 字 |
| 3 | B 纯文本 PDF（不触发 OCR） | 上传原明日方舟.txt 转 PDF → 查看后端日志 | 日志里只记录"文本充足，跳过 OCR"，vision 不被调用 |
| 4 | B 扫描件 PDF | 上传一份"图片扫描成的 PDF"（文本很少）→ 问文档里的文字 | 回答引用到扫描件里的文字；chunk 内容里带"----- 图片识别内容 -----"分隔 |
| 5 | B Word 内嵌图 | 上传一份含内嵌截图的 docx → 问截图里的文字 | 截图文字能被回答引用到 |
| 6 | C 对话贴图片 | 聊天框发 1 张发票图 + "开票日期和含税金额" | 视觉回答模式（不走检索，intent=视觉对话 · 青色 pill）；回答正确 |
| 7 | C 多图限制 | 一次选 5 张图 | 前端直接拦截提示"最多 4 张"，不发请求 |
| 8 | C 大小限制 | 上传一张 >10MB 图 | 前/后端双拦截报错 |
| 9 | C 纯文本消息兼容 | 不选图正常打字发送 | 走旧 `/chat/stream` JSON 接口，行为完全不变 |
| 10 | RGBA→RGB 转码 | 上传带透明通道的 PNG | 不报错，OCR 正常 |
| 11 | VL API Key 错误兜底 | 临时把 VL_API_KEY 改成错的 → 传图片入库 | 提示"无法识别图片内容"，不会把空文本 chunk 入库 |
| 12 | 调试面板 | 做一次 A/C 场景操作 | A：intent 正常 + 查询改写显示；C：intent pill 青色显示"视觉对话 · N 张图" |
