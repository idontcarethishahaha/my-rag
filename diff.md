# My-RAG 项目变更记录（diff.md）

> 本文件记录项目每次代码修改的变化，防止 AI "记忆漂移"。
> 格式：日期 + 变更类型 + 文件路径 + 变更内容摘要

---

## 2026-08-12 初始骨架搭建

### 变更类型：新增（骨架）

| 序号 | 文件路径 | 变更内容 |
|------|---------|---------|
| 1 | `backend/requirements.txt` | 新增：依赖清单（FastAPI+LangChain+ChromaDB+文档解析库） |
| 2 | `backend/.env.example` | 新增：环境变量模板（LLM/嵌入/向量库/RAG参数） |
| 3 | `backend/app/config.py` | 新增：pydantic-settings 统一配置管理 |
| 4 | `backend/app/__init__.py` | 新增：空 init |
| 5 | `backend/app/models/schemas.py` | 新增：Pydantic 数据模型（请求/响应 Schema） |
| 6 | `backend/app/loaders/document_loader.py` | 新增：文档加载器（PDF/Word/Excel/MD/TXT/HTML） |
| 7 | `backend/app/loaders/__init__.py` | 新增：空 init |
| 8 | `backend/app/splitters/text_splitter.py` | 新增：中文友好文本切块器 |
| 9 | `backend/app/embeddings/embed_factory.py` | 新增：嵌入模型工厂（智谱/DashScope/Ollama/OpenAI/Local） |
| 10 | `backend/app/store/vector_store.py` | 新增：向量库封装（Chroma/Qdrant/FAISS + 检索） |
| 11 | `backend/app/services/indexer_service.py` | 新增：索引流水线编排（Load→Split→Embed→Store） |
| 12 | `backend/app/services/retriever_service.py` | 新增：检索+重排服务 |
| 13 | `backend/app/services/generator_service.py` | 新增：LLM 生成服务 + RAG Prompt 模板 |
| 14 | `backend/app/services/rag_service.py` | 新增：RAG 推理流水线总编排（同步/流式 SSE） |
| 15 | `backend/app/services/memory_service.py` | 新增：对话记忆（滑窗 100 条） |
| 16 | `backend/app/services/__init__.py` | 新增：空 init |
| 17 | `backend/app/routers/index_router.py` | 新增：知识库管理 API（上传/列表/状态/删除） |
| 18 | `backend/app/routers/chat_router.py` | 新增：对话 API（流式 SSE + 非流式 + 会话管理） |
| 19 | `backend/app/main.py` | 新增：FastAPI 入口（CORS + 路由注册 + 生命周期预热） |
| 20 | `frontend/index.html` | 新增：豆包风格前端原型（CDN Vue3 + Element Plus） |

### 变更类型：新增（计划文档 + README）

| 序号 | 文件路径 | 变更内容 |
|------|---------|---------|
| 21 | `diff.md` | 新增：代码变更记录文档 |
| 22 | `docs/superpowers/plans/2026-08-12-my-rag-system.md` | 新增：完整实施计划（需求拆解、必做/加分、优先级、验收标准、数据库设计、API设计、目录结构、10个任务清单） |
| 23 | `README.md` | 新增：完整 README（安装、运行、配置、API说明、SSE协议、RAG流程、FAQ） |

## 2026-08-12 配置适配 + README 生成

### 变更类型：修改（配置适配用户实际环境）

| 序号 | 文件路径 | 变更内容 |
|------|---------|---------|
| 24 | `backend/app/config.py` | 修改：新增 URL 反引号清污逻辑；支持 `OPENAI_*` / `MODEL_NAME` 标准变量名兜底；新增 SiliconFlow provider；新增 LangSmith / Tavily 配置 |
| 25 | `backend/app/embeddings/embed_factory.py` | 修改：新增 `siliconflow` 分支（BGE-M3 跑在 SiliconFlow API 上）；SiliconFlow 不传 dimensions 参数；优化 local 分支 |
| 26 | `backend/requirements.txt` | 修改：增加 langsmith、langchain-ollama、langchain-chroma、lxml、beautifulsoup4；本地 BGE/重排/其他向量库改为注释 |
| 27 | `backend/.env.example` | 修改：两种命名惯例（OPENAI_* 兼容 + 自有 LLM_*）；新增 SiliconFlow 方案；新增 LangSmith/Tavily 配置 |
| 28 | `backend/.env` | 新增：用户实际配置（智谱 LLM + SiliconFlow BGE-M3 + LangSmith + Tavily）|

### 当前状态
- 后端配置已接入用户真实 Key 环境（智谱 / SiliconFlow / LangSmith / Tavily）
- 支持标准 `OPENAI_API_KEY` 写法和 `LLM_*` 写法，两种都能用
- 前端可直接浏览器打开预览界面
- 对话记忆为内存版（重启丢失）
- 实施计划文档 + README 已完成

---

## 2026-08-12 文档加载器优化 + 前端错误处理

### 变更类型：修改

| 序号 | 文件路径 | 变更内容 |
|------|---------|---------|
| 29 | `backend/app/loaders/document_loader.py` | 修改：移除 `unstructured` 依赖；Markdown 改为直接读文本；Excel 改为 openpyxl 读取；HTML 改为 BeautifulSoup 解析；全链路用轻量稳定依赖 |
| 30 | `backend/requirements.txt` | 修改：移除 `unstructured`；新增 `docx2txt`（LangChain Docx2txtLoader 依赖）|
| 31 | `frontend/index.html` | 修改：上传错误处理改为先检查 `resp.ok` 再解析 JSON，非 JSON 响应给出清晰错误提示 |

### 原因
- `unstructured` 库在 Windows 上安装/运行不稳定，依赖 libmagic、pandoc 等系统级包
- 文档加载时崩溃导致 FastAPI 返回 HTML 错误页，前端解析 JSON 报 `Unexpected end of JSON input`

---

## 2026-08-12 修复 lru_cache Settings 不可哈希问题

### 变更类型：修复

| 序号 | 文件路径 | 变更内容 |
|------|---------|---------|
| 32 | `backend/app/embeddings/embed_factory.py` | 修改：`@lru_cache` → 模块级 `_instance` 单例模式（Settings 不可哈希）|
| 33 | `backend/app/store/vector_store.py` | 修改：`@lru_cache` → 模块级 `_instance` 单例模式 |
| 34 | `backend/app/config.py` | 修改：`@lru_cache` → 模块级 `_settings_instance` 单例模式；移除 `from functools import lru_cache` |
| 35 | `backend/app/main.py` | 修改：预热调用 `get_embeddings()` / `get_vector_store()` 不再传 settings 参数 |

### 原因
- `Settings`（pydantic-settings 实例）不可哈希，`@lru_cache` 要求所有参数可哈希
- 导致所有涉及 `get_embeddings(settings)` 或 `get_vector_store(settings)` 的调用报 `TypeError: unhashable type: 'Settings'`

---

## 2026-08-12 修复 LLM API Key 未传递问题

### 变更类型：修复

| 序号 | 文件路径 | 变更内容 |
|------|---------|---------|
| 36 | `backend/app/services/generator_service.py` | 修改：`@lru_cache` → 模块级 `_llm_instance` 单例；API Key/Base URL 改为可选传递（未配置时 LangChain 自动读环境变量）；新增 siliconflow provider 支持 |
| 37 | `backend/app/config.py` | 修改：用 `model_post_init` 替代 `field_validator` 做环境变量兜底（更可靠）；新增嵌入模型的 `EMBEDDING_API_KEY`/`EMBEDDING_BASE_URL`/`EMBEDDING_MODEL` 兜底 |

### 原因
1. `ChatOpenAI` 初始化时 `api_key=None` 显式传入，覆盖了 LangChain 自动读取 `OPENAI_API_KEY` 环境变量的行为
2. `field_validator` 在字段未显式设置时可能不触发兜底逻辑
3. `@lru_cache` 同样的 Settings 不可哈希问题

---
