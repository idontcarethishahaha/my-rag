# My-RAG 完整 RAG 系统实施计划

> **用途：** 本文档是 my-rag 项目的唯一实施依据。所有后续开发必须对照此文档执行，防止 AI"记忆漂移"。
> **更新规则：** 每次需求变更或架构调整，必须同步更新本文档和 `diff.md`。

---

## 一、项目目标

从零搭建一个完整的 RAG（检索增强生成）系统，包含：

1. **阶段一（离线/索引流水线）：** 数据加载 → 文本切块 → 嵌入向量化 → 向量存储
2. **阶段二（在线/推理流水线）：** 用户提问 → Query 改写 → 向量检索 → （可选）重排 → 构造增强 Prompt → LLM 生成 → 挂载来源引用 → 返回用户
3. **前端交互界面：** 豆包风格的对话 UI（左侧会话栏 + 右侧聊天区 + 知识库管理）

---

## 二、需求拆解

### 2.1 必做项（MVP）

| 编号 | 需求 | 阶段 | 说明 |
|------|------|------|------|
| M01 | 文件上传入库 | 离线 | 支持 PDF/Word/Markdown/TXT，上传后自动切块+嵌入+存储 |
| M02 | 文本切块 | 离线 | 中文友好的 RecursiveCharacterTextSplitter，chunk_size=500, overlap=80 |
| M03 | 嵌入向量化 | 离线 | 智谱 embedding-3（API），256 维 |
| M04 | 向量存储 | 离线 | ChromaDB 本地文件持久化，零配置 |
| M05 | 向量检索 | 在线 | 余弦相似度，Top-K=6，阈值 0.6 过滤 |
| M06 | RAG Prompt 模板 | 在线 | 系统提示词 + 参考上下文 + 用户问题 |
| M07 | LLM 生成 | 在线 | 智谱 GLM-4.5-Flash，同步+流式 |
| M08 | 来源引用 | 在线 | 答案附带检索文档来源（文件名+页码+相似度） |
| M09 | 流式 SSE 输出 | 在线 | 逐 token 流式返回，前端实时渲染 |
| M10 | 对话记忆 | 在线 | 滑动窗口 100 条消息，按 session_id 隔离 |
| M11 | 会话管理 | 在线 | 新建/切换/删除会话 |
| M12 | 前端聊天界面 | 前端 | 豆包风格：会话栏+聊天区+输入框+欢迎页 |
| M13 | 前端知识库管理 | 前端 | 文件上传+已索引列表+删除 |
| M14 | 配置管理 | 全局 | .env 环境变量 + pydantic-settings 单例 |

### 2.2 加分项（Enhancement）

| 编号 | 需求 | 阶段 | 说明 |
|------|------|------|------|
| E01 | Excel 解析 | 离线 | 支持 .xlsx/.xls 文件 |
| E02 | HTML 解析 | 离线 | 支持 .html 文件 |
| E03 | 重排序 Rerank | 在线 | BGE-Reranker CrossEncoder 精排 Top-N |
| E04 | Query 改写 | 在线 | LLM 把口语化问题重写为检索友好的查询 |
| E05 | 自动摘要记忆 | 在线 | Token 超 4000 时触发 LLM 摘要压缩 |
| E06 | Agentic RAG | 在线 | 多轮检索循环（最多 3 轮），LLM 评估信息充足性 |
| E07 | 检索质量评估 | 在线 | Hit@K / MRR / NDCG 指标计算 |
| E08 | 深度思考开关 | 前端 | 切换 LLM 深度推理模式 |
| E09 | 多模型切换 | 前端 | 下拉选择不同 LLM（GLM/Qwen/Ollama） |
| E10 | 暗色主题 | 前端 | CSS 变量切换深色模式 |
| E11 | 响应式布局 | 前端 | 移动端适配 |
| E12 | Redis 持久化记忆 | 在线 | 对话记忆存 Redis，重启不丢失 |

### 2.3 优先级排序

```
P0（最高，MVP 必须完成）:
  M01 → M02 → M03 → M04 → M05 → M06 → M07 → M09 → M12 → M14

P1（MVP 补全）:
  M08 → M10 → M11 → M13

P2（加分项 - 检索增强）:
  E03 → E04 → E07

P3（加分项 - 记忆增强）:
  E05 → E06 → E12

P4（加分项 - 前端增强）:
  E01 → E02 → E08 → E09 → E10 → E11
```

### 2.4 验收标准

| 验收项 | 标准 |
|--------|------|
| 文件上传 | 上传一个 PDF，返回 file_id + chunks_count > 0 |
| 向量检索 | 提问后能检索到相关文档块，返回 score ≥ 0.6 |
| LLM 生成 | 答案基于上下文，不编造；附带来源引用 |
| 流式输出 | 前端逐字显示答案，有"思考中"动画 |
| 多轮对话 | 同一 session_id 内能理解上下文（"它多少钱？"能指代上文提到的课程） |
| 会话隔离 | 不同 session_id 的对话互不干扰 |
| 前端可用 | 浏览器打开 index.html 能看到完整界面，能上传文件、能对话 |
| 后端可启动 | `uvicorn app.main:app --reload` 无报错，/docs 可访问 |

---

## 三、技术选型

### 3.1 后端

| 环节 | 选型 | 理由 |
|------|------|------|
| 语言/框架 | Python 3.12 + FastAPI | LangChain 生态最完整，FastAPI 原生异步+SSE |
| RAG 框架 | LangChain 0.3+ | 文档加载/分块/嵌入/检索全链路覆盖 |
| 向量数据库 | ChromaDB（默认） | 零配置本地文件持久化，适合开发和小规模生产 |
| 嵌入模型 | 智谱 embedding-3（API） | 中文效果好，256 维省空间，OpenAI 兼容协议 |
| LLM | 智谱 GLM-4.5-Flash | 国内稳定，便宜（0.1元/百万tokens），流式支持好 |
| 文档解析 | pypdf / python-docx / unstructured | 覆盖 PDF/Word/Excel/MD/TXT/HTML |

### 3.2 前端

| 环节 | 选型 | 理由 |
|------|------|------|
| 框架 | Vue 3（CDN 引入） | 快速预览，免 npm install |
| UI 库 | Element Plus | 组件丰富，中文友好 |
| 通信 | fetch + ReadableStream | 消费 SSE 流式响应 |

### 3.3 可替换项（配置驱动）

| 配置项 | 默认值 | 可选值 |
|--------|--------|--------|
| LLM_PROVIDER | zhipu | dashscope / ollama / openai |
| EMBED_PROVIDER | zhipu | dashscope / ollama / openai / local(BGE-M3) |
| VECTOR_DB_TYPE | chroma | qdrant / faiss |
| RAG_ENABLE_RERANK | false | true（需装 FlagEmbedding） |

---

## 四、数据库设计

### 4.1 是否需要数据库？

**MVP 阶段：不需要传统数据库。**
- 向量数据 → ChromaDB（本地文件 `./vector_db/`）
- 对话记忆 → 内存 dict（重启丢失，加分项 E12 升级为 Redis）
- 文件元数据 → 内存 dict（加分项可升级为 SQLite）

### 4.2 加分项：SQLite 元数据表（可选）

如果后续需要文件元数据持久化，建一张 SQLite 表：

```sql
CREATE TABLE IF NOT EXISTS indexed_files (
    file_id      TEXT PRIMARY KEY,
    file_name    TEXT NOT NULL,
    file_type    TEXT,
    file_path    TEXT,
    chunks_count INTEGER DEFAULT 0,
    status       TEXT DEFAULT 'pending',  -- pending/indexing/done/failed
    created_at   TEXT DEFAULT (datetime('now')),
    error        TEXT
);
```

---

## 五、系统接口设计（API 清单）

### 5.1 知识库管理（/api/index）

| 方法 | 路径 | 功能 | 请求 | 响应 |
|------|------|------|------|------|
| POST | `/api/index/upload` | 上传文件入库 | multipart/form-data: file | `{file_id, file_name, chunks_count, status}` |
| GET | `/api/index/status` | 列出所有已索引文件 | — | `[{file_id, file_name, status, progress, chunks_count, error}]` |
| GET | `/api/index/status/{file_id}` | 查询单个文件状态 | path: file_id | `{file_id, file_name, status, progress, chunks_count, error}` |
| DELETE | `/api/index/{file_id}` | 删除文件索引 | path: file_id | `{status: "ok"}` |

### 5.2 对话 / RAG 问答（/api/chat）

| 方法 | 路径 | 功能 | 请求 | 响应 |
|------|------|------|------|------|
| POST | `/api/chat` | 非流式问答（调试用） | `{question, session_id, stream:false, top_k?}` | `{answer, sources[], session_id}` |
| POST | `/api/chat/stream` | 流式问答（SSE） | `{question, session_id, stream:true, top_k?}` | `event: source/thinking/token/done/error` |

### 5.3 会话管理（/api/conversations）

| 方法 | 路径 | 功能 | 请求 | 响应 |
|------|------|------|------|------|
| POST | `/api/conversations/new` | 创建新会话 | — | `{session_id, title}` |
| GET | `/api/conversations` | 会话列表 | — | `[{session_id, title, created_at, last_message}]` |
| DELETE | `/api/conversations/{session_id}` | 清空会话 | path: session_id | `{status: "ok"}` |

### 5.4 健康检查

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/` | 根目录信息 |
| GET | `/api/ping` | 健康检查 |
| GET | `/docs` | Swagger API 文档 |

### 5.5 SSE 事件协议

```
event: source\ndata: [{chunk_id, content, source_file, page, score}]\n\n
event: thinking\ndata: null\n\n
event: token\ndata: "一个字"\n\n
event: done\ndata: null\n\n
event: error\ndata: "错误信息"\n\n
```

---

## 六、目录结构与模块职责

```
my-rag/
├── docs/
│   └── superpowers/
│       └── plans/
│           └── 2026-08-12-my-rag-system.md   ← 本文件（实施计划）
├── diff.md                                    ← 代码变更记录
├── backend/
│   ├── app/
│   │   ├── __init__.py
│   │   ├── main.py                            ← FastAPI 入口：CORS + 路由注册 + 生命周期预热
│   │   ├── config.py                           ← 配置管理：pydantic-settings 单例，从 .env 读取
│   │   │
│   │   ├── models/
│   │   │   └── schemas.py                     ← 数据模型：Pydantic Schema（请求/响应/事件）
│   │   │
│   │   ├── loaders/
│   │   │   └── document_loader.py              ← Load 阶段：按扩展名分发到不同 Loader
│   │   │      职责：读取文件 → 返回 [Document(page_content, metadata)]
│   │   │
│   │   ├── splitters/
│   │   │   └── text_splitter.py                ← Split 阶段：中文友好分块
│   │   │      职责：[Document] → [更小的 Document]，保留语义边界
│   │   │
│   │   ├── embeddings/
│   │   │   └── embed_factory.py               ← Embed 阶段：嵌入模型工厂
│   │   │      职责：按配置返回 LangChain Embeddings 单例
│   │   │
│   │   ├── store/
│   │   │   └── vector_store.py                ← Store/Retrieve 阶段：向量库封装
│   │   │      职责：add_documents / similarity_search_with_score / delete_by_file
│   │   │
│   │   ├── services/
│   │   │   ├── indexer_service.py             ← 索引流水线编排（离线）
│   │   │   │    职责：Load → Split → Embed → Store，文件上传入库
│   │   │   ├── retriever_service.py          ← 检索+重排服务
│   │   │   │    职责：query → 向量检索 Top-K → （可选）重排 → 返回 [DocumentChunk]
│   │   │   ├── generator_service.py           ← LLM 生成服务
│   │   │   │    职责：RAG Prompt 模板 + LLM 调用（同步/流式）
│   │   │   ├── rag_service.py                ← 推理流水线总编排（在线）
│   │   │   │    职责：检索 → 组装 Prompt → LLM 生成 → 返回答案+来源
│   │   │   └── memory_service.py             ← 对话记忆
│   │   │        职责：按 session_id 管理滑动窗口，append/get/clear
│   │   │
│   │   ├── routers/
│   │   │   ├── index_router.py                ← 知识库管理 API 路由
│   │   │   │    职责：HTTP 请求 → 调用 indexer_service → 返回响应
│   │   │   └── chat_router.py                 ← 对话 API 路由
│   │   │        职责：HTTP 请求 → 调用 rag_service → SSE 流式返回
│   │   │
│   │   └── utils/                              ← 工具函数（预留）
│   │
│   ├── data/
│   │   └── uploads/                            ← 上传文件存储目录
│   ├── vector_db/                              ← ChromaDB 持久化目录
│   ├── .env.example                            ← 环境变量模板
│   ├── .env                                    ← 实际配置（需用户创建，gitignore）
│   └── requirements.txt                        ← 依赖清单
│
└── frontend/
    └── index.html                              ← 豆包风格单页应用
         职责：会话管理 + 聊天界面 + 知识库管理
```

### 模块间数据流

```
【离线流水线】
文件上传 → indexer_service
              → document_loader.load_document()    [Load]
              → text_splitter.split_documents()    [Split]
              → vector_store.add_documents()       [Embed + Store]
                 → embed_factory.get_embeddings()  [向量化]

【在线流水线】
用户提问 → rag_service.ask_rag_stream()
              → retriever_service.retrieve()       [Retrieve]
                 → vector_store.similarity_search() [向量检索]
                 → _rerank()                       [可选重排]
              → generator_service.build_rag_prompt() [构造增强 Prompt]
              → generator_service.chat_stream()   [LLM 生成]
              → memory_service.append()            [记忆写入]
```

---

## 七、任务清单

### Task 1: 项目初始化与依赖安装

**文件：**
- 已创建: `backend/requirements.txt`
- 已创建: `backend/.env.example`
- 已创建: `backend/app/config.py`

- [ ] **Step 1: 复制 .env.example 为 .env**

```bash
cd d:\ai学习项目\my-rag\backend
copy .env.example .env
```

- [ ] **Step 2: 填入智谱 API Key**

在 `.env` 中设置：
```
LLM_API_KEY=你的智谱API Key
EMBED_API_KEY=你的智谱API Key
```

- [ ] **Step 3: 安装依赖**

```bash
cd d:\ai学习项目\my-rag\backend
pip install -r requirements.txt
```

- [ ] **Step 4: 验证后端启动**

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```
验收：浏览器访问 `http://localhost:8000/docs` 看到 Swagger 文档

- [ ] **Step 5: 验证前端打开**

直接用浏览器打开 `frontend/index.html`，看到豆包风格界面

- [ ] **Step 6: 更新 diff.md**

---

### Task 2: 索引流水线验证（M01-M04）

**文件：**
- 已创建: `backend/app/loaders/document_loader.py`
- 已创建: `backend/app/splitters/text_splitter.py`
- 已创建: `backend/app/embeddings/embed_factory.py`
- 已创建: `backend/app/store/vector_store.py`
- 已创建: `backend/app/services/indexer_service.py`

- [ ] **Step 1: 准备测试文档**

准备一个 PDF 或 TXT 文件放到 `backend/data/uploads/` 目录

- [ ] **Step 2: 通过 API 上传文件**

```bash
curl -X POST http://localhost:8000/api/index/upload \
  -F "file=@test.pdf"
```
验收：返回 `{"file_id":"xxx","chunks_count":N,"status":"success"}`，N > 0

- [ ] **Step 3: 验证向量库数据**

```bash
curl http://localhost:8000/api/index/status
```
验收：能看到刚上传的文件状态为 `done`

- [ ] **Step 4: 更新 diff.md**

---

### Task 3: 推理流水线验证（M05-M09）

**文件：**
- 已创建: `backend/app/services/retriever_service.py`
- 已创建: `backend/app/services/generator_service.py`
- 已创建: `backend/app/services/rag_service.py`
- 已创建: `backend/app/routers/chat_router.py`

- [ ] **Step 1: 非流式问答测试**

```bash
curl -X POST http://localhost:8000/api/chat \
  -H "Content-Type: application/json" \
  -d '{"question":"文档讲了什么内容","session_id":"test001","stream":false}'
```
验收：返回 `answer` 字段非空，`sources` 数组非空且含来源文件名和 score

- [ ] **Step 2: 流式问答测试**

```bash
curl -X POST http://localhost:8000/api/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"question":"文档讲了什么内容","session_id":"test001","stream":true}'
```
验收：终端逐行输出 `event: token\ndata: "字"` 格式的 SSE 事件

- [ ] **Step 3: 前端联调**

浏览器打开 `frontend/index.html`，输入问题，验收：
- 看到"思考中"动画
- 逐字显示答案
- 答案下方显示来源标签（文件名+相似度%）

- [ ] **Step 4: 更新 diff.md**

---

### Task 4: 对话记忆与会话管理验证（M10-M11）

**文件：**
- 已创建: `backend/app/services/memory_service.py`
- 已创建: `backend/app/routers/chat_router.py`（会话管理接口）

- [ ] **Step 1: 多轮对话测试**

同一 session_id 连续提问：
```
第1轮: "文档里提到了哪些技术？"
第2轮: "第一个技术具体是什么？"  ← 应该能理解"第一个技术"指代上文
```
验收：第 2 轮回答能正确引用第 1 轮的上下文

- [ ] **Step 2: 会话隔离测试**

不同 session_id 提问，验收：会话之间互不干扰

- [ ] **Step 3: 前端会话切换**

在左侧栏新建多个会话，切换时验收：消息记录独立

- [ ] **Step 4: 更新 diff.md**

---

### Task 5: 知识库管理前端联调（M13）

**文件：**
- 已创建: `frontend/index.html`（知识库抽屉组件）

- [ ] **Step 1: 上传文件**

点击左侧"知识库" → 拖入或选择文件 → 验收：显示"入库成功"

- [ ] **Step 2: 查看已索引列表**

验收：列表显示文件名、切块数、状态

- [ ] **Step 3: 删除文件**

点击删除 → 验收：列表更新，该文件不再出现在检索结果中

- [ ] **Step 4: 更新 diff.md**

---

### Task 6（加分）: 重排序 Rerank（E03）

**文件：**
- 修改: `backend/requirements.txt`（取消注释 FlagEmbedding）
- 修改: `backend/app/services/retriever_service.py`（实现 `_rerank`）
- 修改: `backend/.env.example`（`RAG_ENABLE_RERANK=true`）

- [ ] Step 1: 安装 FlagEmbedding
- [ ] Step 2: 实现 BGE-Reranker 重排逻辑
- [ ] Step 3: 对比重排前后的检索结果
- [ ] Step 4: 更新 diff.md

---

### Task 7（加分）: Query 改写（E04）

**文件：**
- 修改: `backend/app/services/retriever_service.py`（实现 `rewrite_query`）
- 新增: `backend/app/utils/query_rewriter.py`

- [ ] Step 1: 用 LLM 实现查询改写 Prompt
- [ ] Step 2: 对比改写前后的检索效果
- [ ] Step 3: 更新 diff.md

---

### Task 8（加分）: 检索质量评估（E07）

**文件：**
- 新增: `backend/app/services/eval_service.py`
- 新增: `backend/app/routers/eval_router.py`

- [ ] Step 1: 实现 Hit@K / MRR / NDCG 指标计算
- [ ] Step 2: 实现批量测试接口
- [ ] Step 3: 更新 diff.md

---

### Task 9（加分）: 自动摘要记忆（E05）

**文件：**
- 修改: `backend/app/services/memory_service.py`（升级为 SummarizingMemory）
- 新增: `backend/app/utils/token_counter.py`

- [ ] Step 1: 实现 Token 计数器
- [ ] Step 2: 实现摘要压缩逻辑（Token > 4000 时触发）
- [ ] Step 3: 更新 diff.md

---

### Task 10（加分）: 前端增强（E08-E11）

**文件：**
- 修改: `frontend/index.html`

- [ ] Step 1: 深度思考开关功能联动后端
- [ ] Step 2: 多模型切换下拉框
- [ ] Step 3: 暗色主题 CSS 变量
- [ ] Step 4: 移动端响应式适配
- [ ] Step 5: 更新 diff.md

---

## 八、风险与注意事项

| 风险 | 影响 | 应对 |
|------|------|------|
| 智谱 API Key 未配置 | LLM 和嵌入无法调用 | .env 必须填入有效 Key |
| ChromaDB 版本兼容 | langchain-chroma 版本变化导致 API 不兼容 | requirements.txt 锁定版本 |
| PowerShell 执行策略 | 无法运行 .ps1 脚本 | 用 Python 命令替代 |
| 前端 CORS | 浏览器跨域请求被拦 | 后端已配置 `allow_origins=["*"]` |
| 大文件上传超时 | FastAPI 默认无限制 | 加分项可配置上传大小限制 |
| 内存记忆重启丢失 | 对话历史消失 | 加分项 E12 升级为 Redis |

---

## 九、开发顺序总结

```
第1步: 配置 .env + pip install + 启动后端        (Task 1)
第2步: 上传一个文档，验证索引流水线               (Task 2)
第3步: 提问验证推理流水线（后端curl + 前端界面）    (Task 3)
第4步: 验证多轮对话和会话管理                      (Task 4)
第5步: 前端知识库管理联调                          (Task 5)
--- MVP 完成 ---
第6步: 加分项按优先级实施                          (Task 6-10)
```
