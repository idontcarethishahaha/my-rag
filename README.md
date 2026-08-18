# My-RAG 🍅

> 从零搭建的完整 RAG（检索增强生成）系统：数据加载 → 切块 → 嵌入 → 向量存储 → 检索 → LLM 生成 → 来源引用
> 
> 前端采用豆包风格交互界面，后端 Python + FastAPI + LangChain，支持多种LLM和向量数据库。

![技术栈](https://img.shields.io/badge/Python-3.12+-blue) ![FastAPI](https://img.shields.io/badge/FastAPI->=0.115-green) ![LangChain](https://img.shields.io/badge/LangChain->=0.3-orange) ![Vue3](https://img.shields.io/badge/Vue-3.4-cyan) ![ChromaDB](https://img.shields.io/badge/ChromaDB->=0.5-purple)

---

## 🌟 特性

- **多LLM支持**：智谱GLM、通义千问、Ollama本地模型、OpenAI兼容接口
- **多向量数据库**：ChromaDB（零配置）、Qdrant、FAISS
- **多格式文档**：PDF/Word/Excel/Markdown/TXT/HTML
- **智能分块**：中文友好的语义分块，支持重叠
- **流式对话**：实时响应，逐字显示
- **对话记忆**：滑动窗口上下文管理
- **来源引用**：精确的文件名、页码、相似度标注
- **豆包UI**：现代化交互界面，支持拖拽上传

---

## 目录

- [功能概览](#功能概览)
- [技术栈](#技术栈)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [配置说明](#配置说明)
- [运行](#运行)
- [API 文档](#api-文档)
- [SSE 事件协议](#sse-事件协议)
- [RAG 流程说明](#rag-流程说明)
- [常见问题](#常见问题)

---

## 功能概览

### 阶段一：数据准备 / 索引流水线（离线）

| 步骤 | 说明 | 支持格式 |
|------|------|---------|
| 数据加载（Load） | 从文件读取原始文档 | PDF / Word / Excel / Markdown / TXT / HTML |
| 文本切块（Split） | 中文友好的语义分块 | chunk_size=500, overlap=80 |
| 嵌入（Embed） | 文本块转向量 | 智谱 embedding-3 / DashScope / Ollama / 本地 BGE-M3 |
| 存储（Store） | 向量+原文存入向量库 | ChromaDB（默认）/ Qdrant / FAISS |

### 阶段二：查询 / 推理流水线（在线）

| 步骤 | 说明 |
|------|------|
| 用户提问 | 接收自然语言问题 |
| Query 改写 | （可选）LLM 优化查询语句 |
| 向量检索 | 余弦相似度召回 Top-K=6，阈值 0.6 过滤 |
| 重排序 | （可选）BGE-Reranker 精排 Top-N |
| 构造增强 Prompt | 检索结果 + 用户问题 → RAG Prompt 模板 |
| LLM 生成 | 智谱 GLM-4.5-Flash，流式输出 |
| 来源引用 | 答案附带文件名 + 页码 + 相似度 |
| 对话记忆 | 滑动窗口 100 条，多轮上下文 |

### 前端界面（豆包风格）

- 左侧会话栏：新建 / 切换 / 删除会话
- 右侧聊天区：流式逐字显示 + 思考中动画 + 来源引用标签
- 知识库管理抽屉：拖拽上传 + 已索引列表 + 删除
- 欢迎页：快捷问题卡片
- 顶部栏：模型切换 + 深度思考开关

---

## 技术栈

| 层 | 技术 | 版本 |
|----|------|------|
| 后端框架 | FastAPI | ≥0.115 |
| RAG 框架 | LangChain | ≥0.3 |
| 向量数据库 | ChromaDB | ≥0.5 |
| 嵌入模型 | 智谱 embedding-3 | 256 维 |
| LLM | 智谱 GLM-4.5-Flash | OpenAI 兼容协议 |
| 文档解析 | pypdf / python-docx / unstructured | — |
| 前端框架 | Vue 3 | 3.4（CDN 引入） |
| 前端 UI | Element Plus | 2.8（CDN 引入） |
| Python | 3.12+ | — |

---

## 项目结构

```
my-rag/
├── docs/superpowers/plans/
│   └── 2026-08-12-my-rag-system.md       ← 完整实施计划
├── diff.md                                ← 代码变更记录
├── backend/
│   ├── app/
│   │   ├── main.py                        ← FastAPI 入口
│   │   ├── config.py                      ← 配置管理
│   │   ├── models/schemas.py              ← 数据模型
│   │   ├── loaders/document_loader.py     ← 文档加载
│   │   ├── splitters/text_splitter.py     ← 文本切块
│   │   ├── embeddings/embed_factory.py    ← 嵌入模型
│   │   ├── store/vector_store.py          ← 向量库
│   │   ├── services/
│   │   │   ├── indexer_service.py         ← 索引流水线
│   │   │   ├── retriever_service.py       ← 检索+重排
│   │   │   ├── generator_service.py       ← LLM 生成
│   │   │   ├── rag_service.py             ← 推理编排
│   │   │   └── memory_service.py          ← 对话记忆
│   │   └── routers/
│   │       ├── index_router.py            ← 知识库 API
│   │       └── chat_router.py             ← 对话 API
│   ├── data/uploads/                     ← 上传文件目录
│   ├── vector_db/                        ← ChromaDB 数据目录
│   ├── .env.example                      ← 环境变量模板
│   └── requirements.txt                  ← 依赖清单
└── frontend/
    └── index.html                         ← 前端界面
```

---

## 快速开始

### 前置条件

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) 包管理工具（推荐用于快速虚拟环境和依赖管理）
- 智谱 API Key（去 [开放平台](https://open.bigmodel.cn/) 注册获取）
- 现代浏览器（Chrome / Edge / Firefox）

#### 安装 uv（如果尚未安装）

```bash
# Windows (通过 PowerShell)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# 或者使用 winget
winget install --id Astral.uv

# 验证安装
uv --version
```

### 第 1 步：克隆项目

```bash
cd d:\ai学习项目\my-rag
```

### 第 2 步：配置环境变量

```bash
cd backend
copy .env.example .env
```

编辑 `.env`，填入你的智谱 API Key：

```ini
LLM_API_KEY=你的智谱API Key
EMBED_API_KEY=你的智谱API Key
```

### 第 3 步：用 uv 创建虚拟环境并安装依赖

```bash
# 用 uv 创建一个 Python 3.12.7 虚拟环境
uv venv --python 3.12.7
# 激活环境
.venv\Scripts\activate
# 安装依赖
cd D:\ai学习项目\my-rag\backend
cd D:\code\my-rag\backend
uv pip install -r requirements.txt -i https://mirrors.ustc.edu.cn/pypi/web/simple
```

### 第 4 步：启动后端

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

启动成功后会看到：

```
[my-rag] 嵌入模型 + 向量库 初始化完成
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### 第 5 步：打开前端

直接用浏览器打开 `frontend/index.html` 即可。

或者访问后端 Swagger 文档：`http://localhost:8000/docs`

---

## 配置说明

所有配置通过 `backend/.env` 文件管理，完整选项如下：

### 服务配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `SERVER_HOST` | 0.0.0.0 | 监听地址 |
| `SERVER_PORT` | 8000 | 监听端口 |

### LLM 配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_PROVIDER` | zhipu | 提供商：zhipu / dashscope / ollama / openai |
| `LLM_API_KEY` | — | API Key（必填） |
| `LLM_BASE_URL` | https://open.bigmodel.cn/api/paas/v4 | API 地址 |
| `LLM_CHAT_MODEL` | glm-4.5-flash | 模型名称 |

**切换到通义千问：**

```ini
LLM_PROVIDER=dashscope
LLM_API_KEY=你的DashScope Key
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_CHAT_MODEL=qwen-plus
```

**切换到 Ollama 本地模型：**

```ini
LLM_PROVIDER=ollama
LLM_BASE_URL=http://localhost:11434
LLM_CHAT_MODEL=qwen2.5:7b
```

### 嵌入模型配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `EMBED_PROVIDER` | zhipu | 提供商：zhipu / dashscope / ollama / openai / local |
| `EMBED_API_KEY` | — | API Key |
| `EMBED_BASE_URL` | https://open.bigmodel.cn/api/paas/v4 | API 地址 |
| `EMBED_MODEL` | embedding-3 | 模型名称 |
| `EMBED_DIMENSION` | 256 | 向量维度 |

### 向量数据库配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `VECTOR_DB_TYPE` | chroma | 类型：chroma / qdrant / faiss |
| `VECTOR_DB_PATH` | ./vector_db | ChromaDB 持久化目录 |
| `VECTOR_DB_COLLECTION` | my_rag_knowledge | 集合名 |
| `VECTOR_DB_HOST` | localhost | Qdrant 主机 |
| `VECTOR_DB_PORT` | 6333 | Qdrant 端口 |

### RAG 参数

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `RAG_CHUNK_SIZE` | 500 | 每块字符数 |
| `RAG_CHUNK_OVERLAP` | 80 | 相邻块重叠字符数 |
| `RAG_TOP_K` | 6 | 检索召回数 |
| `RAG_SIMILARITY_THRESHOLD` | 0.6 | 相似度阈值（低于则过滤） |
| `RAG_ENABLE_RERANK` | false | 是否启用重排序 |
| `RAG_RERANK_TOP_N` | 3 | 重排后保留条数 |
| `RAG_MAX_TOKENS_LIMIT` | 8000 | 上下文最大 Token |
| `RAG_SUMMARY_THRESHOLD` | 4000 | 摘要触发阈值 |

### 路径配置

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `UPLOAD_DIR` | ./data/uploads | 上传文件存储目录 |

---

## 运行

### 启动后端

```bash
cd backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 启动前端

无需启动服务器，直接用浏览器打开 `frontend/index.html`。

### 上传文档建立索引

**方式一：前端界面**

1. 点击左侧"知识库"
2. 拖入或选择文件（PDF/Word/Excel/MD/TXT/HTML）
3. 等待"入库成功"提示

**方式二：API 调用**

```bash
curl -X POST http://localhost:8000/api/index/upload \
  -F "file=@你的文档.pdf"
```

### 开始对话

**方式一：前端界面**

在输入框输入问题，按 Enter 发送。

**方式二：API 调用（非流式）**

```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d "{\"question\":\"文档讲了什么\",\"session_id\":\"test001\",\"stream\":false}"
```

**方式三：API 调用（流式）**

```bash
curl -N -X POST http://localhost:8000/api/chat/stream \
  -H "Content-Type: application/json" \
  -d "{\"question\":\"文档讲了什么\",\"session_id\":\"test001\",\"stream\":true}"
```

---

## API 文档

启动后端后访问 `http://localhost:8000/docs` 可查看交互式 Swagger 文档。

### 知识库管理

#### POST /api/index/upload — 上传文件入库

上传文件，自动执行 Load → Split → Embed → Store。

**请求：** `multipart/form-data`

| 字段 | 类型 | 说明 |
|------|------|------|
| file | File | 文件（支持 PDF/Word/Excel/MD/TXT/HTML） |

**响应：**

```json
{
  "file_id": "a1b2c3d4e5f6",
  "file_name": "文档.pdf",
  "chunks_count": 12,
  "status": "success"
}
```

#### GET /api/index/status — 列出所有已索引文件

**响应：**

```json
[
  {
    "file_id": "a1b2c3d4e5f6",
    "file_name": "文档.pdf",
    "status": "done",
    "progress": 1.0,
    "chunks_count": 12,
    "error": null
  }
]
```

#### GET /api/index/status/{file_id} — 查询单个文件状态

**响应：** 同上单个对象

#### DELETE /api/index/{file_id} — 删除文件索引

**响应：**

```json
{ "status": "ok", "message": "已删除索引" }
```

---

### 对话 / RAG 问答

#### POST /api/chat — 非流式问答

**请求：**

```json
{
  "question": "文档讲了什么内容",
  "session_id": "test001",
  "stream": false,
  "top_k": null
}
```

**响应：**

```json
{
  "answer": "这篇文档主要讲述了...",
  "sources": [
    {
      "chunk_id": "abc123",
      "content": "文档原文片段...",
      "source_file": "文档.pdf",
      "page": 1,
      "score": 0.85,
      "metadata": {}
    }
  ],
  "session_id": "test001",
  "usage": {}
}
```

#### POST /api/chat/stream — 流式问答（SSE）

**请求：** 同上，`stream: true`

**响应：** Server-Sent Events 流，详见 [SSE 事件协议](#sse-事件协议)

---

### 会话管理

#### POST /api/conversations/new — 创建新会话

**响应：**

```json
{ "session_id": "abc-def-123", "title": "新对话" }
```

#### GET /api/conversations — 会话列表

**响应：**

```json
[
  {
    "session_id": "abc-def-123",
    "title": "文档讲了什么",
    "created_at": "2026-08-12T10:30:00",
    "last_message": "这篇文档主要..."
  }
]
```

#### DELETE /api/conversations/{session_id} — 清空会话

**响应：**

```json
{ "status": "ok" }
```

---

### 健康检查

#### GET / — 根目录信息

```json
{
  "name": "my-rag",
  "version": "0.1.0",
  "docs": "/docs",
  "status": "running",
  "vector_db": "chroma",
  "llm_provider": "zhipu"
}
```

#### GET /api/ping — 健康检查

```json
{ "status": "ok" }
```

---

## SSE 事件协议

流式问答（`/api/chat/stream`）返回 Server-Sent Events 格式，事件按顺序：

| 事件顺序 | event | data | 说明 |
|---------|-------|------|------|
| 1 | `source` | `[{chunk_id, content, source_file, page, score}]` | 检索到的引用来源 |
| 2 | `thinking` | `null` | LLM 开始生成 |
| 3 | `token` | `"一个字"` | 逐 token 输出（重复多次） |
| 4 | `done` | `null` | 生成完毕 |
| — | `error` | `"错误信息"` | 出错时 |

**SSE 原始格式：**

```
event: source
data: {"chunk_id":"abc","content":"...","source_file":"文档.pdf","page":1,"score":0.85}

event: thinking
data: null

event: token
data: "这"

event: token
data: "篇"

event: done
data: null
```

**前端消费方式：** 使用 `fetch` + `ReadableStream` 读取并解析，详见 `frontend/index.html` 中的 `send()` 函数。

---

## RAG 流程说明

### 离线流水线（索引）

```
文件上传
  │
  ├─ Load    document_loader.py    按扩展名选择 Loader → [Document]
  ├─ Split   text_splitter.py      中文友好分块 → [更小的 Document]
  ├─ Embed   embed_factory.py      智谱 embedding-3 → 向量
  └─ Store   vector_store.py       ChromaDB 持久化存储
```

### 在线流水线（推理）

```
用户提问
  │
  ├─ Query 改写     retriever_service.py     （可选）LLM 优化查询
  ├─ 向量检索        vector_store.py          Top-K=6，阈值 0.6
  ├─ 重排序          retriever_service.py     （可选）BGE-Reranker
  ├─ 构造 Prompt     generator_service.py     系统提示 + 上下文 + 问题
  ├─ LLM 生成        generator_service.py     GLM-4.5-Flash 流式输出
  ├─ 来源引用        rag_service.py           文件名 + 页码 + 相似度
  └─ 记忆写入        memory_service.py        滑动窗口 100 条
```

### RAG Prompt 模板

```
你是一个严谨、诚实的知识库问答助手。

请基于下面【参考上下文】中的信息回答用户问题。必须遵守规则：
1. 回答要尽量基于【参考上下文】，不要编造上下文里没有提到的内容。
2. 如果上下文不足以回答问题，请诚实说："根据现有资料，暂时无法回答这个问题"。
3. 尽量引用具体出处（文件名 / 页码）。
4. 使用简洁、清晰的中文表达。

【参考上下文】
[1] 来源：文档.pdf（第1页）
内容：文档原文片段...

[2] 来源：笔记.md
内容：...
```

---

## 常见问题

### Q: 启动报错 `ModuleNotFoundError`

确保在 `backend/` 目录下执行 `pip install -r requirements.txt`，且 Python 版本 ≥ 3.12。

### Q: 前端页面打开后对话报错

确认后端已启动在 `http://localhost:8000`。打开浏览器开发者工具（F12）查看 Console 和 Network 错误。

### Q: 上传文件报错 `不支持的文件类型`

检查文件扩展名。支持：`.pdf .docx .doc .xlsx .xls .md .markdown .txt .log .csv .html .htm`

### Q: LLM 回答不相关

可能原因：
1. 文档未成功入库 → 检查 `/api/index/status` 状态
2. 相似度阈值太高 → 在 `.env` 中降低 `RAG_SIMILARITY_THRESHOLD`（如 0.4）
3. Top-K 太小 → 调大 `RAG_TOP_K`（如 10）

### Q: 如何切换 LLM 模型

编辑 `.env`，修改 `LLM_PROVIDER`、`LLM_API_KEY`、`LLM_BASE_URL`、`LLM_CHAT_MODEL`，重启后端。

### Q: 如何切换向量数据库

编辑 `.env`，修改 `VECTOR_DB_TYPE` 为 `qdrant` 或 `faiss`。Qdrant 需要 Docker 启动：

```bash
docker run -p 6333:6333 qdrant/qdrant
```

### Q: 对话记忆重启后丢失

当前使用内存存储。如需持久化，可升级为 Redis（加分项 E12）。

### Q: ChromaDB 数据在哪

默认在 `backend/vector_db/` 目录下，删除该目录可清空所有向量数据。

---

## License

MIT