---
name: "rag-pipeline"
description: "RAG pipeline with pluggable chunking, rerank, and embedding fallback. Invoke when building RAG systems, knowledge base Q&A, document indexing, vector retrieval, or LLM generation with context injection."
---

# RAG Pipeline Skill

从文档索引到 LLM 生成的完整 RAG 流水线，各阶段可插拔、可独立开关。

## 架构总览

```
【离线流水线 · 索引阶段】
  Load（加载文档）→ Split（递归切块 + parent_context）→ Embed（三级回退嵌入）→ Store（ChromaDB 持久化）

【在线流水线 · 查询阶段】
  Query → [Intent Classify（可选）] → Retrieve（大召回）→ [Rerank（可选）] → Generate（普通/深度思考）→ [SSE Stream（可选）]
```

**可插拔开关**：
- `enable_intent`：意图路由（默认 off，开启后按 4 类意图分支处理）
- `enable_rerank`：重排序（默认 off，开启后 local/remote/auto 三模式）
- `enable_deep_think`：深度思考模式（默认 off，开启后通过 SSE 返回 reasoning_content）
- `enable_sse_stream`：SSE 流式输出（默认 on）
- `parent_context`：父块上下文（索引时注入，检索时自动生效，无需开关）

**对外统一入参**：
```python
def ask_rag_stream(
    question: str,           # 用户问题
    session_id: str,         # 会话 ID（多轮记忆）
    enable_deep_think: bool = False,
    enable_rerank: bool | None = None,   # None = 走配置默认
    model: str | None = None,            # None = 走默认模型
    top_k: int | None = None,
) -> Generator[dict, None, None]
```

---

## 各阶段设计

### 1. 文档加载（Load）

支持多格式：PDF / Word(.docx) / Excel(.xlsx) / Markdown / TXT / HTML。

**关键原则**：
- 优先轻量依赖（pdfplumber、python-docx），避免 unstructured 在 Windows 上的问题
- 加载后产出统一格式：`list[Document]`（LangChain Document，含 `page_content` + `metadata`）
- metadata 保留：`page`（页码）、`section_title`（章节标题）

### 2. 文本切块（Split）

**递归分隔符策略**（优先保证语义完整）：
```
分隔符优先级：段落(\n\n) → 换行(\n) → 句末标点(. ！？。) → 分号(；) → 逗号(，) → 顿号(、) → 空格 → 逐字
```

**Token 估算**（用于控制块大小）：
- 中文：每 1.5 字算 1 token
- 非中文：每 4 字符算 1 token

**默认参数**：
- `chunk_size`：500 字符（约 250 token）
- `chunk_overlap`：80 字符（约 40 token）
- 相邻块之间加 overlap，避免关键信息在切分处丢失

### 3. 父块上下文（parent_context）

在切块完成后、入库前，为每个 chunk 注入 `metadata["parent_content"]`：
- 将前一块 + 自身 + 后一块的文本合并存入 `parent_content`
- 首块只有 自身 + 后一块
- 尾块只有 前一块 + 自身
- 块间用 `\n\n` 分隔

**检索时自动生效**：`_format_context()` 优先使用 `meta.get("parent_content")` 而非 `content`，让 LLM 获得更完整的语境。

### 4. 嵌入向量（Embed）

**三级回退策略**（按优先级自动选择）：
1. **local**：本地 BGE-M3（FlagEmbedding），零成本，离线可用
2. **Ollama**：本地 Ollama 服务，需提前拉取模型
3. **OpenAI 兼容 API**：智谱 / SiliconFlow / 通义，远端调用

**配置独立**：嵌入模型可独立配置 API_KEY / BASE_URL / MODEL，未设置则回退到主 LLM 配置。

### 5. 向量存储（Store）

- 默认 ChromaDB 本地文件存储，零配置
- 每个 chunk 存储内容：文本 + 向量 + metadata（file_id、source、chunk_index、parent_content、page、section_title、token_count）
- 模块级单例，避免重复初始化
- 检索使用 `similarity_search_with_score`，返回 `(Document, score)` 元组列表

### 6. 意图路由（可选，`enable_intent`）

在检索前调用一次轻量 LLM（temperature=0.0, max_tokens=256）做意图分类 + query 改写。

**4 类意图**：
| 意图 | 行为 | 检索 | 调 LLM |
|------|------|------|--------|
| `chat` | 闲聊 / 问候 | 跳过 | 直接回答 |
| `file_list` | 问知识库文件列表 | 跳过 | 直接返回文件清单 |
| `kb_query` | 需查知识库的问题 | 完整 RAG | 带上下文生成 |
| `follow_up` | 追问上文 | 改写 query 后走 RAG | 带上下文生成 |

**三层容错**：去 markdown 包裹 → JSON 解析 → 未知标签回退到 `kb_query`。

**记忆写入策略**：所有分支都写**原始 question**（不写改写后的 query），保持对话历史真实。

### 7. 检索（Retrieve）

**大召回策略**：
- `recall_k = max(20, top_k)`，默认召回 20 条
- `score_threshold = 0.0`，不过滤，最大化召回率
- 不启用 rerank 时，按向量相似度截断到 `top_k`（默认 6）

### 8. 重排序（Rerank，可选，`enable_rerank`）

**三模式 + 自动降级**：
| 模式 | 后端 | 说明 |
|------|------|------|
| `local` | 本地 FlagReranker + bge-reranker-v2-m3 | 零成本，CPU ~300-500ms |
| `remote` | SiliconFlow /v1/rerank（Cohere 兼容） | ~100ms，复用 embedding key |
| `auto` | 优先 local，失败回退 remote | 默认模式 |

**降级链**：配置缺失 → 跳过 → 超时 → 降级 → 解析失败 → 降级 → 终极降级：按向量分数截断。

**模块级单例 + 懒加载**：本地 reranker 只加载一次，后续请求零加载开销。失败后不再重试（`_local_reranker_checked` 标记）。

**启用后参数变化**：`keep_n` 从 `RAG_TOP_K=6` 变为 `RERANK_TOP_N=3`（少而精，减少 token 占用）。

### 9. 生成（Generate）

**双模式**：
- **普通模式**：LangChain ChatOpenAI，标准流式输出
- **深度思考模式**：直接用 AsyncOpenAI + `extra_body={"enable_thinking": True}`，绕过 LangChain 确保 `enable_thinking` 参数正确传递

**SSE 流式解析**：分别提取 `delta.reasoning_content`（思考 token）和 `delta.content`（回答 token）。

**国内 API 直连**：智谱等国内域名绕过 HTTP_PROXY，避免代理超时。

### 10. SSE 事件序列

```
debug（可选）→ source → thinking → [thinking_token...] → thinking_done → [token...] → done
```

| 事件 | data | 说明 |
|------|------|------|
| `debug` | `{intent, original_query, rewritten_query, retrieval}` | 调试信息（意图、改写、检索详情） |
| `source` | `[DocumentChunk...]` | 检索到的知识库来源 |
| `thinking` | `null` | 深度思考开始 |
| `thinking_token` | `str` | 思考过程增量 token |
| `thinking_done` | `null` | 思考结束 |
| `token` | `str` | 回答增量 token |
| `done` | `null` | 流结束 |
| `error` | `str` | 异常 traceback |

### 11. Prompt 构建

**system prompt 要点**：
- 设定 AI 身份和性格
- 明确告诉 LLM 哪些是系统注入的参考信息（知识库文件列表、参考资料、置信度），哪些是用户真正的问题
- 要求不要在回答中重复展示系统注入的参考信息
- 知识库优先 + LLM 自身知识兜底，兜底时标注"（以下内容来自通用知识）"

**置信度计算**：`0.6 * top_score + 0.4 * avg_score`，分 high/medium/low/very_low 四档。

**上下文格式**：`[资料i] 来源文件 - 页码\nparent_content`，块间用 `\n\n---\n\n` 分隔。

---

## 配置管理

**统一模式**：`dotenv` + `os.getenv()`，与其他项目保持一致。

**配置分区**：
- `[llm.main]`：主 LLM（API_KEY / BASE_URL / MODEL_ID）
- `[llm.embedding]`：嵌入模型（独立配置，回退主模型）
- `[rag]`：RAG 超参（chunk_size / top_k / threshold）
- `[rerank]`：重排序（enable / provider / model / timeout / top_n）
- `[vector_db]`：向量库（type / path / collection）

**URL 清理**：统一去除 URL 上意外的反引号和前后空格。

---

## 错误处理与降级

| 阶段 | 失败场景 | 降级策略 |
|------|----------|----------|
| 嵌入 | 本地模型未安装 | 回退到 Ollama → OpenAI 兼容 API |
| 意图分类 | LLM 调用失败 / JSON 解析失败 | 回退到 `kb_query`，走完整 RAG |
| 重排序 | 本地模型缺失 / 远端超时 / API 错误 | 降级到向量分数截断 |
| 生成 | 事件循环关闭 | 移除 `finally` 块中的 `task.cancel()` |
| 向量检索 | 无命中 | 返回空列表，LLM 用自身知识回答 |

---

## 索引管理

**文件生命周期**：
- 上传：保存到磁盘 → Load → Split → 注入 parent_content → Embed → Store
- 删除：向量库 delete_by_file → 磁盘文件删除 → 内存 _progress_store 移除
- 恢复：启动时从磁盘扫描 uploads 目录，恢复文件列表，清理孤立 chunk

**chunk 数统计**：通过 `vs.get()` 查询向量库实际 chunk 数，不依赖内存计数。
