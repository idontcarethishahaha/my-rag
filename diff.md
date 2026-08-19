# MyRag AI 修改记录 (diff.md)

生成时间：2026-08-14（第二轮修改）

---

## 一、本轮修改总览

本轮修复了用户反馈的两个核心问题 + 若干 UI 稳定性问题：

| # | 问题 | 涉及文件 |
|---|------|----------|
| 1 | 回答结束后页面跳回顶部 + 深度思考按钮自动关闭 | `frontend/index.html` |
| 2 | 文档已入库但检索不到（阈值过高导致过滤） | `backend/app/store/vector_store.py`、`backend/app/services/retriever_service.py` |
| 3 | 页面抖动（transition:all + autoResize + scrollToBottom 频繁触发） | `frontend/index.html` |
| 4 | 历史对话看不到（后端 role=assistant vs 前端 role=ai 不匹配） | `frontend/index.html` |
| 5 | Event loop is closed 错误（_syncify finally 中 task.cancel） | `backend/app/services/generator_service.py` |

---

## 二、各文件详细修改

### 2.1 `frontend/index.html`

#### 2.1.1 深度思考按钮：el-button → 原生 button

**根因**：Element Plus `el-button` 在 `:disabled` 状态切换（`isStreaming` 变化）时内部重渲染，导致 `:class="{ on: enableDeepThink }"` 绑定的 `.on` 样式丢失，视觉上看起来"按钮自动关闭"。

```diff
- <el-button
-   class="deep-think-btn"
-   :class="{ on: enableDeepThink }"
-   @click="enableDeepThink = !enableDeepThink"
-   size="small"
- >
-   <span style="margin-right:3px;">✦</span>深度思考
- </el-button>

+ <button
+   class="deep-think-btn"
+   :class="{ on: enableDeepThink }"
+   @click="enableDeepThink = !enableDeepThink"
+   :disabled="isStreaming"
+ >
+   <span style="margin-right:3px;">✦</span>深度思考
+ </button>
```

CSS 也同步调整，新增 `:disabled` 灰化样式，`transition` 从 `all` 改为只过渡 `background/color/border-color`。

#### 2.1.2 消息 key 稳定化（防止 Vue 重建 DOM 导致滚动跳动）

**根因**：`v-for :key="idx"` 用数组序号当 key，每次 token 增量都可能触发 Vue 重新渲染整个列表，导致 `scrollTop` 丢失。

```diff
- v-for="(m, idx) in messages" :key="idx"

+ // setup() 中新增全局自增 uid 生成器
+ let _msgUidCounter = 0;
+ const _nextMsgUid = () => ('m_' + (++_msgUidCounter) + '_' + Date.now().toString(36));
+
+ // 每条消息创建时分配稳定 _uid
+ messages.value.push(reactive({ _uid: _nextMsgUid(), role: 'user', content: q }));
+ const aiMsg = reactive({ _uid: _nextMsgUid(), role: 'ai', ... });
+
+ // v-for key 改为稳定 uid
+ v-for="(m, idx) in messages" :key="m._uid || ('fallback_' + idx)"
```

#### 2.1.3 scrollToBottom 增加 force 参数

**根因**：`scrollToBottom` 的 `dist < 120` 判断在思考块展开 / 内容增长时不满足，导致不滚动。

```diff
- function scrollToBottom() {
-   nextTick(() => {
-     const el = msgListRef.value;
-     if (!el) return;
-     const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
-     if (dist < 120) el.scrollTop = el.scrollHeight;
-   });
- }

+ function scrollToBottom(force = false) {
+   nextTick(() => {
+     const el = msgListRef.value;
+     if (!el) return;
+     if (force) {
+       el.scrollTop = el.scrollHeight;
+       return;
+     }
+     const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
+     if (dist < 120) el.scrollTop = el.scrollHeight;
+   });
+ }
```

在 `done` 事件和兜底逻辑中调用 `scrollToBottom(true)` 强制滚动：
```diff
+ if (event === 'done') {
+   ...
+   scrollToBottom(true);  // 流结束后强制滚到底部
+ }
```

#### 2.1.4 done 事件增加 _finalized 标记

防止 SSE `done` 之后兜底逻辑重复处理消息：

```diff
+ if (event === 'done') {
+   aiMsg._finalized = true;
+   ...
+ }
+ // 兜底逻辑检查标记
+ if (isStreaming.value && !aiMsg._finalized) { ... }
```

#### 2.1.5 历史消息 role 映射修复

**根因**：后端 `memory_service` 存储 role 为 `assistant`，前端模板判断 `m.role === 'ai'` 渲染 AI 消息，不匹配导致历史对话不显示。

```diff
+ // 后端 role=assistant → 前端 role=ai
+ const role = sm.role === 'assistant' ? 'ai' : sm.role;
```

同时在 `switchConversation` 中为从 localStorage / 后端加载的旧消息补上 `_uid`。

#### 2.1.6 autoResize 优化（减少抖动）

```diff
- function autoResize() {
-   const el = textareaRef.value;
-   if (!el) return;
-   el.style.height = 'auto';           // ← 先设 auto 会瞬间收缩
-   el.style.height = Math.min(el.scrollHeight, 180) + 'px';
- }

+ function autoResize() {
+   const el = textareaRef.value;
+   if (!el) return;
+   const target = Math.min(el.scrollHeight, 180);
+   if (Math.abs(el.offsetHeight - target) > 1) {
+     el.style.height = target + 'px';  // ← 直接比较，不设 auto
+   }
+ }
```

#### 2.1.7 deep-think-btn 按钮开关样式修正

之前默认状态是黑底白字（看着像已激活），点击后变白底（像关闭）。修正为：
- **未开启**（默认）：白底黑字 + 浅边框
- **已开启**（`.on`）：黑底白字

---

### 2.2 `backend/app/store/vector_store.py`

**根因**：`similarity_search_with_score` 默认 `score_threshold=0.3`，ChromaDB 返回的 L2 距离转换成 `1/(1+d)` 后，很多有效文档的相似度在 0.2~0.3 之间，被阈值过滤掉了。

```diff
  def similarity_search_with_score(
      query: str,
      k: int = 6,
-     score_threshold: float = 0.3,
+     score_threshold: float = 0.0,   # 默认不过滤，最大化召回率
      filter: Optional[dict] = None,
  ) -> list[tuple[Document, float]]:
-     """阈值默认 0.3：低于此值的结果被过滤；设为 0 则不过滤。"""
+     """阈值默认 0.0（不过滤，最大化召回率）。"""
```

---

### 2.3 `backend/app/services/retriever_service.py`

新增检索日志，方便调试"为什么检索不到"的问题：

```diff
+ import logging
+ logger = logging.getLogger(__name__)

  def retrieve(...):
      ...
      docs_scores = similarity_search_with_score(query, k=recall_k, score_threshold=th)
+     logger.info(f"[retrieve] query='{query[:50]}', recall_k={recall_k}, threshold={th}, raw_hits={len(docs_scores)}")
      ...
+     if chunks:
+         logger.info(f"[retrieve] 返回 {len(chunks)} 个块, top_score={chunks[0].score:.4f}, sources={[c.source_file for c in chunks]}")
      return chunks
```

---

### 2.4 `backend/app/services/generator_service.py`

修复 `RuntimeError: Event loop is closed` 错误：

**根因**：`_syncify` 的 `main()` 异步生成器在 `finally` 块中调用 `task.cancel()` + `await task`，但此时事件循环已经被 `loop.close()` 关闭。

```diff
  async def main():
      task = asyncio.create_task(_drain())
      try:
          while True:
              ...
      finally:
-         task.cancel()
-         try:
-             await task
-         except (asyncio.CancelledError, Exception):
-             pass
+         # 不再 cancel：_drain 正常通过 q.put(("done", None)) 结束
+         # 循环关闭后 cancel 会报 RuntimeError: Event loop is closed
+         pass
```

---

## 三、验证清单

重启后端（`uvicorn app.main:app --reload --host 0.0.0.0 --port 8000`）+ 刷新前端后验证：

| # | 验证项 | 操作 | 预期结果 |
|---|--------|------|----------|
| 1 | 深度思考按钮保持 | 开启深度思考 → 发送问题 → 等待回答完成 | 按钮保持黑底白字 on 态，不自动关闭 |
| 2 | 滚动到底部 | 发送问题 → 等待回答完成 | 页面自动滚到最底部，不跳回顶部 |
| 3 | 页面不抖动 | 流式回答过程中观察页面 | 无明显抖动/跳动 |
| 4 | 历史对话可见 | 切换到之前的会话 | 历史消息正常显示（AI 消息有头像和气泡） |
| 5 | 文档检索正常 | 上传文档 → 问文档中的内容 | 后端日志显示 `raw_hits > 0`，AI 回答引用资料 |
| 6 | Event loop 错误 | 后端控制台 | 不再出现 `RuntimeError: Event loop is closed` |
| 7 | 闲聊兜底 | 问"你好你叫什么" | 正常回复，不说"根据知识库无法回答" |
| 8 | 思考过程折叠 | 开启深度思考 → 问推理题 | 出现可折叠的思考块，点击可展开/收起 |

---

# 第三轮修改：意图识别 + Query 改写（方案 A）

生成时间：2026-08-17

---

## 一、本轮修改总览

在 RAG 检索前新增"意图路由"环节。检索前调用一次轻量 LLM（`glm-4-flash`，`temperature=0.0`，`max_tokens=256`）做意图分类 + query 改写，根据意图走不同分支，避免所有问题都走完整 RAG（闲聊被强行检索、文件列表被强行 LLM、追问指代不清检索偏差）。

| # | 改动 | 涉及文件 |
|---|------|----------|
| 1 | 新增意图识别服务 | `backend/app/services/intent_service.py`（新建） |
| 2 | 在 RAG 编排中集成意图路由 | `backend/app/services/rag_service.py` |

### 设计目标

| 意图 | 行为 | 是否检索 | 是否调 LLM |
|------|------|----------|------------|
| `chat` | 闲聊 / 问候 / 个人问题，跳过检索 | ❌ | ✅ |
| `file_list` | 询问知识库文件列表，直接返回文件清单 | ❌ | ❌ |
| `kb_query` | 需查知识库的问题，完整 RAG | ✅ | ✅ |
| `follow_up` | 追问上文，先用历史改写 query 再走 RAG | ✅ | ✅ |

---

## 二、各文件详细修改

### 2.1 `backend/app/services/intent_service.py`（新建）

**职责**：调用 LLM 做意图分类 + query 改写，返回 `(intent, rewritten_query)`。

**核心实现**：

```python
# 意图标签
INTENT_CHAT = "chat"
INTENT_KB_QUERY = "kb_query"
INTENT_FILE_LIST = "file_list"
INTENT_FOLLOW_UP = "follow_up"
VALID_INTENTS = {INTENT_CHAT, INTENT_KB_QUERY, INTENT_FILE_LIST, INTENT_FOLLOW_UP}

def classify_intent(
    query: str,
    history: list[dict] | None = None,
    model: str | None = None,
) -> tuple[str, str]:
    # 拼 history（最近 2 条，1 轮）
    # 调 glm-4-flash 非流式
    # 解析 JSON → (intent, rewritten_query)
    # 失败默认回退 (kb_query, query)
```

**分类 Prompt（关键约束）**：

```
你是一个意图分类器。根据用户问题和最近对话历史，判断意图并返回 JSON。
意图类型：chat / kb_query / file_list / follow_up
返回格式（严格 JSON，不要 markdown 代码块）：
{"intent": "意图标签", "rewritten_query": "改写后的问题"}

改写规则：
- follow_up 必须把指代词替换为具体内容
  例：历史提到"蜂医"，用户问"它有什么功效"
      → 改写为"蜂医有什么功效"
- chat / kb_query / file_list：rewritten_query 直接返回原问题
- rewritten_query 不能为空
```

**三层容错**（保证不会因分类失败导致整个 RAG 流程崩）：

```python
def _parse_result(raw: str, fallback_query: str) -> tuple[str, str]:
    # 1) 去 markdown 代码块包裹
    # 2) JSON 解析失败 → 回退 (kb_query, fallback_query)
    # 3) 未知意图标签 → 回退 (kb_query, fallback_query)
```

**失败兜底**：分类异常时默认走 `kb_query`，即完整 RAG 流程，确保不会因为分类服务故障而漏答。

---

### 2.2 `backend/app/services/rag_service.py`（修改）

#### 2.2.1 新增 import

```diff
  from .memory_service import get_memory_manager
  from .indexer_service import get_file_list
+ from .intent_service import classify_intent, INTENT_CHAT, INTENT_KB_QUERY, INTENT_FILE_LIST, INTENT_FOLLOW_UP
```

#### 2.2.2 `ask_rag`（非流式，测试路径）增加意图路由

```diff
  def ask_rag(...) -> tuple[str, str, list[DocumentChunk]]:
      memory = get_memory_manager()
      history_raw = memory.get_messages(session_id, last_n=6)
      history = _messages_to_dicts(history_raw)

+     # 1) 意图识别 + query 改写
+     intent, rewritten_query = classify_intent(question, history=history)
+
+     # 2) 路由：file_list → 直接返回文件列表，不调 LLM
+     if intent == INTENT_FILE_LIST:
+         file_list = get_file_list()
+         if not file_list:
+             answer = "当前知识库没有任何文件喵 (｡•ᴗ-｡)♡"
+         else:
+             lines = [f"- {f['file_name']} ({f['chunks_count']} 块)" for f in file_list]
+             answer = "知识库中当前有以下文件喵：\n" + "\n".join(lines)
+         memory.append(session_id, question, answer)
+         return answer, "", []
+
+     # 3) 路由：chat → 跳过检索，直接 LLM 回答
+     if intent == INTENT_CHAT:
+         messages = [{"role": "system", "content": SYSTEM_PROMPT_RAG}]
+         for m in history:
+             messages.append({"role": m["role"], "content": m["content"]})
+         messages.append({"role": "user", "content": question})
+         answer, thinking_text = chat(messages, enable_deep_think=enable_deep_think, model=model)
+         memory.append(session_id, question, answer)
+         return answer, thinking_text, []
+
+     # 4) kb_query / follow_up → 完整 RAG 流程
+     chunks = retrieve(rewritten_query, top_k=top_k)
      ...
+     messages = build_rag_messages(
+         query=rewritten_query,       # ← 用改写后的 query
+         chunks=_chunks_to_dicts(chunks),
+         conversation_history=history,
+         file_list=file_list,
+     )
      answer, thinking_text = chat(messages, enable_deep_think=enable_deep_think, model=model)
      memory.append(session_id, question, answer)   # ← 写原始 question，不写改写后的
      return answer, thinking_text, chunks
```

#### 2.2.3 `ask_rag_stream`（流式，前端主路径）增加意图路由

```diff
  def ask_rag_stream(...) -> Generator[dict, None, None]:
      memory = get_memory_manager()
      try:
          history_raw = memory.get_messages(session_id, last_n=6)
          history = _messages_to_dicts(history_raw)

+         # ---- 1) 意图识别 + query 改写 ----
+         intent, rewritten_query = classify_intent(question, history=history)
+         logger.info(f"[rag_stream] intent={intent}, rewritten='{rewritten_query[:80]}'")
+
+         # ---- 2) 路由：file_list → 直接返回文件列表 ----
+         if intent == INTENT_FILE_LIST:
+             file_list = get_file_list()
+             if not file_list:
+                 answer = "当前知识库没有任何文件喵 (｡•ᴗ-｡)♡"
+             else:
+                 lines = [f"- {f['file_name']} ({f['chunks_count']} 块)" for f in file_list]
+                 answer = "知识库中当前有以下文件喵：\n" + "\n".join(lines)
+             yield {"event": "source", "data": []}
+             yield {"event": "thinking", "data": None}
+             yield {"event": "thinking_done", "data": None}
+             for line in answer.split("\n"):
+                 yield {"event": "token", "data": line + "\n"}
+             memory.append(session_id, question, answer)
+             yield {"event": "done", "data": None}
+             return
+
+         # ---- 3) 路由：chat → 跳过检索，直接 LLM 回答 ----
+         if intent == INTENT_CHAT:
+             yield {"event": "source", "data": []}
+             messages = [{"role": "system", "content": SYSTEM_PROMPT_RAG}]
+             for m in history:
+                 messages.append({"role": m["role"], "content": m["content"]})
+             messages.append({"role": "user", "content": question})
+             yield {"event": "thinking", "data": None}
+             full_answer: list[str] = []
+             full_thinking: list[str] = []
+             thinking_phase_finished = False
+             for ttype, token in chat_stream(messages, enable_deep_think=enable_deep_think, model=model):
+                 if ttype == "thinking":
+                     full_thinking.append(token)
+                     yield {"event": "thinking_token", "data": token}
+                 elif ttype == "content":
+                     if not thinking_phase_finished:
+                         thinking_phase_finished = True
+                         yield {"event": "thinking_done", "data": None}
+                     full_answer.append(token)
+                     yield {"event": "token", "data": token}
+             if not thinking_phase_finished and full_thinking:
+                 yield {"event": "thinking_done", "data": None}
+             answer = "".join(full_answer)
+             if question and answer:
+                 memory.append(session_id, question, answer)
+             yield {"event": "done", "data": None}
+             return
+
+         # ---- 4) kb_query / follow_up → 完整 RAG 流程 ----
+         chunks = retrieve(rewritten_query, top_k=top_k)
          yield {
              "event": "source",
              "data": [c.model_dump() for c in chunks],
          }
          file_list = get_file_list()
          messages = build_rag_messages(
+             query=rewritten_query,        # ← 用改写后的 query
              chunks=_chunks_to_dicts(chunks),
              conversation_history=history,
              file_list=file_list,
          )
          ...
          # 写记忆：写原始 question，不写改写后的
          answer = "".join(full_answer)
          if question and answer:
              memory.append(session_id, question, answer)
```

---

## 三、SSE 事件流（按意图分支）

| 意图 | SSE 事件序列 |
|------|--------------|
| `file_list` | `source(空) → thinking → thinking_done → token(按行) → done` |
| `chat` | `source(空) → thinking → [thinking_token...] → thinking_done → token... → done` |
| `kb_query` / `follow_up` | `source(块列表) → thinking → [thinking_token...] → thinking_done → token... → done` |
| 异常 | `error`（含 traceback） |

**前端兼容性**：
- `chat` 和 `file_list` 分支会发空 `source` 事件，前端 `.sources` 渲染前需判空（已有逻辑通常 `v-if="sources.length"` 即可）。
- 所有分支都发 `thinking` / `thinking_done`，前端思考块逻辑无需区分意图。

---

## 四、记忆写入策略

| 分支 | 写入的 user 内容 |
|------|-------------------|
| `file_list` | 原始 `question` |
| `chat` | 原始 `question` |
| `kb_query` | 原始 `question` |
| `follow_up` | 原始 `question`（**不是**改写后的 `rewritten_query`） |

**为什么 follow_up 也写原始 question？**
- 改写后的 query 只用于检索这一步，不应污染对话历史。
- 用户在历史里看到的就是自己原话，符合直觉。
- 下一轮如果再追问，意图分类器会基于真实历史做改写，不会因为"上次也写了改写后的"导致累积走样。

---

## 五、潜在风险与边界

| # | 风险 | 缓解 |
|---|------|------|
| 1 | 意图分类 LLM 调用增加一次延迟（约 200~500ms） | 用 `glm-4-flash`（快），`max_tokens=256`，`temperature=0.0` |
| 2 | 分类失败导致流程中断 | 三层容错 + 失败回退 `kb_query` |
| 3 | 闲聊被误判为 `kb_query` 走了 RAG | 影响小，RAG 流程本来也支持"检索不到就闲聊兜底" |
| 4 | `file_list` / `chat` 分支发空 `source` 事件 | 前端需确认空列表不渲染异常来源块 |
| 5 | `file_list` 分支按行推送，前端 token 拼接 | 已用 `line + "\n"` 保证换行 |

---

## 六、验证清单

重启后端 + 刷新前端后验证：

| # | 验证项 | 操作 | 预期结果 |
|---|--------|------|----------|
| 1 | 闲聊路由 | 问「你好呀」「你是谁」 | 后端日志 `intent=chat`，跳过检索，AI 直接回答 |
| 2 | 文件列表路由 | 问「知识库里有哪些文件」「有哪些文档」 | 后端日志 `intent=file_list`，直接返回文件列表，不调 LLM |
| 3 | 知识库查询路由 | 问「明日方舟是什么」 | 后端日志 `intent=kb_query`，走完整 RAG，回答引用资料 |
| 4 | 追问改写 | 先问「蜂医是什么」→ 再问「它有什么功效」 | 第二句日志 `intent=follow_up, rewritten='蜂医有什么功效'`，检索用改写后的 query |
| 5 | 闲聊不出现来源块 | 闲聊后看回答下方 | 无 `.sources` 来源展示 |
| 6 | 文件列表流式效果 | 问文件列表 | 按行流式输出，不一次性出现 |
| 7 | 分类失败兜底 | 故意断网或改错 API key | 后端日志 `分类失败，回退到 kb_query`，仍能走 RAG |
| 8 | 记忆正确性 | 追问后查看 `chat_memory.db` | `messages` 表存的是原始 `question`，不是改写后的 |

**后端重启命令**（在 `backend` 目录下）：
```powershell
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

---

# 第四轮修改：Rerank 重排序（本地 CrossEncoder + SiliconFlow 远端 API 双模式）

生成时间：2026-08-17

---

## 一、本轮修改总览

检索阶段之前只有"向量相似度 → 截断"，**向量相似度 ≠ 真实语义相关性**，容易出现噪声 chunk 排名靠前、真正相关的 chunk 被挤出 Top-K 的情况。本轮在"大召回 → 截断"之间加一步 **Cross-Encoder Rerank（语义重排）**，用 bge-reranker-v2-m3 给 (query, chunk) 成对打 0~1 relevance 分，再按分截断。

参考 RAG-Pro 的 `backend/app/core/reranker.py` 本地 CrossEncoder 实现，同时新增 SiliconFlow HTTP 后端，做成 **双模式 + 自动降级**，不阻塞检索流程。

| # | 改动 | 涉及文件 |
|---|------|----------|
| 1 | 新增 Rerank 配置（ENABLE/PROVIDER/MODEL/DEVICE/BASE_URL/API_KEY/TIMEOUT/TOP_N） | `backend/app/config.py` |
| 2 | 重写检索服务：双模式 rerank（local/remote/auto）+ 失败降级 | `backend/app/services/retriever_service.py` |
| 3 | .env 示例同步新增 Rerank 配置（双方案注释） | `backend/.env.example` |

### 设计目标

检索新流水线：

```
query
  → rewrite_query（占位）
  → 向量大召回 recall_k=20（阈值 0 不过滤，保证召回率）
  → Rerank（可选，RERANK_ENABLE=true 且命中>1）：
       provider=auto   → 优先本地 FlagReranker → 失败/无模型 → SiliconFlow remote → 失败/超时 → 降级
       provider=local  → 本地 FlagReranker（复用 RAG-Pro 已有 bge-reranker-v2-m3）
       provider=remote → SiliconFlow /v1/rerank（Cohere 兼容，复用 EMBEDDING key/URL）
  → 截断到 keep_n（默认 rerank 开=3，关=6）
  → DocumentChunk 格式化输出
```

### 两种 rerank 后端对比

| 维度 | local（FlagReranker 本地） | remote（SiliconFlow HTTP） |
|---|---|---|
| 模型 | `bge-reranker-v2-m3`，复用 `D:\ai学习项目\RAG-Pro\backend\models\BAAI\bge-reranker-v2-m3` 路径 | `BAAI/bge-reranker-v2-m3`（同模型，服务端） |
| 成本 | 0（本地 CPU 跑） | $0.02 / 百万 tokens（几乎免费） |
| 速度 | CPU ~300-500ms（20 条 chunks） | ~100ms |
| 依赖 | FlagEmbedding（embedding 用本地时已装） | httpx（已在 requirements.txt） |
| 适用 | 追求零成本 / 断网也能用 | 追求速度 / 本地没装 FlagEmbedding |

**默认 `RERANK_PROVIDER=auto`**：有本地模型就先用（省成本），没有就走远端。都失败了**自动降级**到"按向量分数截断"，不影响主流程。

---

## 二、各文件详细修改

### 2.1 `backend/app/config.py`（修改）

在 RAG 超参区之后新增 **Rerank 配置区**，并同步更新 `reload()` 函数。

```python
# ==================================
# Rerank 重排序配置（默认复用 SiliconFlow Embedding 配置）
# ==================================
RERANK_ENABLE = os.getenv("RERANK_ENABLE", str(RAG_ENABLE_RERANK)).lower() == "true"
RERANK_PROVIDER = os.getenv("RERANK_PROVIDER", "auto").strip().lower()  # auto | local | remote
RERANK_MODEL = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
RERANK_DEVICE = os.getenv("RERANK_DEVICE", "cpu").strip().lower()      # cpu | cuda
# 默认复用 EMBEDDING 配置（如果用的是 SiliconFlow 就能直接用）
RERANK_BASE_URL = _clean_url(os.getenv("RERANK_BASE_URL", EMBEDDING_BASE_URL))
RERANK_API_KEY = os.getenv("RERANK_API_KEY", EMBEDDING_API_KEY)
RERANK_TIMEOUT = float(os.getenv("RERANK_TIMEOUT", "10.0"))  # 秒，超时直接降级
RERANK_TOP_N = int(os.getenv("RERANK_TOP_N", str(RAG_RERANK_TOP_N)))
```

关键设计：
- `RERANK_ENABLE` 复用 `RAG_ENABLE_RERANK` 的值（避免两个开关冲突）
- `RERANK_BASE_URL/API_KEY` **默认复用 EMBEDDING 的配置**——如果你 embedding 已经配了 SiliconFlow，rerank 不用再写第二遍 key
- `RERANK_TIMEOUT=10.0`：显式超时，避免默认等 1-2 分钟导致系统卡顿
- `reload()` 函数同步新增 8 个变量，保证运行中改 .env 后 `config.reload()` 能生效

---

### 2.2 `backend/app/services/retriever_service.py`（完全重写）

从 87 行扩到 345 行，新增以下能力：

#### 2.2.1 本地模型路径查找（复用 RAG-Pro）

```python
def _has_local_reranker_model() -> Optional[Path]:
    # 查两个候选目录：
    #   1. D:\ai学习项目\RAG-Pro\backend\models\BAAI\bge-reranker-v2-m3
    #   2. D:\ai学习项目\my-rag\backend\models\BAAI\bge-reranker-v2-m3
    # 有 pytorch_model.bin 或 model.safetensors 就算命中
```

#### 2.2.2 模块级本地 reranker 单例 + 懒加载

```python
_local_reranker = None
_local_reranker_checked = False  # 只尝试加载一次；失败就不再反复试，避免每次都卡
```

和 embedding 一样，模块级单例，首次使用时加载，避免每次请求都重新 load 模型。

#### 2.2.3 `retrieve()` 主流程改造

```diff
  def retrieve(question, top_k=None, threshold=None, enable_rerank=None):
      # --- 第一步：大召回（不变，recall_k=20） ---
      docs_scores = similarity_search_with_score(query, k=recall_k, score_threshold=th)
      raw_hits = len(docs_scores)

      # --- 第二步：决定是否 rerank ---
+     use_rerank = enable_rerank if enable_rerank is not None else RERANK_ENABLE
+     use_rerank = bool(use_rerank and raw_hits > 1)  # 只有命中>1 rerank 才有意义
+     keep_n = min(raw_hits, (top_k if top_k is not None else (RERANK_TOP_N if use_rerank else RAG_TOP_K)))
+
+     if use_rerank:
+         reranked = _rerank(query, docs_scores, top_n=keep_n)
+         if reranked is not None:
+             docs_scores_final = reranked   # 成功：用 rerank 后的顺序+分数
+         else:
+             # 降级：按原向量分数截断（和之前的行为完全一致）
+             docs_scores.sort(key=lambda x: x[1], reverse=True)
+             docs_scores_final = docs_scores[:keep_n]
+     else:
+         # 不启用 rerank：和之前行为一致
+         docs_scores.sort(key=lambda x: x[1], reverse=True)
+         docs_scores_final = docs_scores[:keep_n]

      # --- 第三步：DocumentChunk 格式化（不变） ---
```

**关键设计**：启用 rerank 后 `keep_n` 默认取 `RERANK_TOP_N=3`，不启用取 `RAG_TOP_K=6`。原因：rerank 后更精准，可以少而精，减少上下文 token 占用；关掉 rerank 时保持和以前一致的行为，不影响已上线流程。

#### 2.2.4 `_rerank()` 三模式路由

```python
def _rerank(query, docs_scores, top_n) -> Optional[list[tuple[Document, float]]]:
    provider = RERANK_PROVIDER or "auto"
    passages = [...]  # 统一准备，按 provider 选不同截断长度

    if provider == "local":
        return _rerank_local(query, passages, docs_scores, top_n)
    if provider == "remote":
        return _rerank_remote(query, passages, docs_scores, top_n)

    # ---- auto（默认）：优先 local，失败回退 remote ----
    local_path = _has_local_reranker_model()
    if local_path is not None:
        res = _rerank_local(query, passages, docs_scores, top_n)
        if res is not None:
            return res
        logger.warning("local 后端失败，auto 回退到 remote")
    else:
        logger.info("未检测到本地模型，auto 模式走 remote")
    return _rerank_remote(query, passages, docs_scores, top_n)
```

#### 2.2.5 `_rerank_local()`：本地 FlagReranker

参考 RAG-Pro 的实现：

```python
def _rerank_local(query, passages, docs_scores, top_n):
    # 懒加载（只试一次）
    if _local_reranker is None and not _local_reranker_checked:
        _local_reranker_checked = True
        local_path = _has_local_reranker_model()
        if local_path is None:
            return None
        from FlagEmbedding import FlagReranker
        use_fp16 = RERANK_DEVICE not in {"cpu", ""}
        _local_reranker = FlagReranker(str(local_path), use_fp16=use_fp16)

    pairs = [[query, p] for p in passages]
    scores = _local_reranker.compute_score(pairs, normalize=True)  # 0~1
    indexed = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)[:top_n]
    reranked = [(docs_scores[idx][0], float(score)) for idx, score in indexed]
    return reranked
```

**失败都 return None（不抛异常）**，调用方检测 None 就走降级。

#### 2.2.6 `_rerank_remote()`：SiliconFlow HTTP API

```python
def _rerank_remote(query, passages, docs_scores, top_n):
    if not (RERANK_API_KEY and RERANK_BASE_URL and RERANK_MODEL):
        return None

    import httpx
    url = RERANK_BASE_URL.rstrip("/") + "/rerank"   # SiliconFlow: https://api.siliconflow.cn/v1/rerank
    headers = {"Authorization": f"Bearer {RERANK_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": RERANK_MODEL,
        "query": query,
        "documents": passages,
        "top_n": top_n,
    }

    try:
        with httpx.Client(timeout=httpx.Timeout(RERANK_TIMEOUT, connect=3.0)) as client:
            resp = client.post(url, headers=headers, json=payload)
            if resp.status_code != 200:
                ...  # 记录错误日志 → return None
            data = resp.json()
    except httpx.TimeoutException:
        ...  # return None
    except Exception as e:
        ...  # return None

    # 解析：results = [{"index": 0, "relevance_score": 0.92}, ...]
    results = data.get("results") or []
    reranked = [(docs_scores[idx][0], float(score)) for idx, score in map_results(results)]
    return reranked
```

**多层保护**：
1. 配置缺失 → 直接跳过
2. HTTP 非 200 → 透传错误体 → 降级
3. `TimeoutException` → 降级
4. 任何异常 → 降级
5. 解析后无有效结果 → 降级

**全部失败都 return None，不会抛异常到调用方**，主流程只检查 None → 自动走向量分数截断。

---

### 2.3 `backend/.env.example`（修改）

新增 "Rerank 重排序配置" 区块，把 RAG 超参里的 `RAG_ENABLE_RERANK` 从 `false` 改成 `true`（默认开启体验更好），同时写了**两套完整注释方案**：

```bash
# 方案 A：SiliconFlow 远端 API（推荐，和 BGE-M3 embedding 复用 key）
RERANK_ENABLE=true
RERANK_PROVIDER=remote
RERANK_MODEL=BAAI/bge-reranker-v2-m3
# RERANK_BASE_URL=            # 没写复用 EMBEDDING_BASE_URL
# RERANK_API_KEY=             # 没写复用 EMBEDDING_API_KEY
RERANK_TIMEOUT=10.0
RERANK_TOP_N=3

# 方案 B：本地 CrossEncoder（免费，复用 RAG-Pro 已有模型）
# RERANK_ENABLE=true
# RERANK_PROVIDER=local
# RERANK_MODEL=BAAI/bge-reranker-v2-m3
# RERANK_TOP_N=3
# RERANK_DEVICE=cpu           # cpu | cuda
```

---

## 三、降级流程图

```
                    +--------------------+
                    | RERANK_ENABLE=true |
                    +---------+----------+
                              |
                    +---------v----------+
                    |  RERANK_PROVIDER   |
                    +---------+----------+
                              |
         +--------------------+--------------------+
         |                    |                    |
    +----v----+         +-----v-----+        +-----v------+
    |  local  |         |   remote   |        |    auto    |
    +----+----+         +-----+-----+        +-----+------+
         |                    |                    |
         v                    v                    v
  检测本地模型路径      检查 key/url        检测本地模型 ---> 有 ---> 本地 FlagReranker ---> 成功? ---> OK
         |                    |                    ^                          |
         v                    v                    |                          v 失败
   FlagReranker          SiliconFlow /rerank       |                    回退 remote ---> 成功? ---> OK
    compute_score           HTTP POST              |                          |
         |                    |                    |                          v 失败
         v                    v                    |                    终极降级：
   成功？              成功？                     |                  按向量分数截断
     |  |                |  |                      |                        |
  OK  降级             OK  降级 <------------------+                        |
     +--------------------+-----------------------------------------------+
                              |
                              v
              retrieve() 拿到最终排序结果
                      输出 DocumentChunk
```

---

## 四、潜在风险与边界

| # | 风险 | 缓解 |
|---|------|------|
| 1 | 本地 FlagEmbedding 没装 → local 后端不能用 | auto 模式自动回退 remote；remote 模式不受影响 |
| 2 | SiliconFlow API 不稳定/限流 | `RERANK_TIMEOUT=10s` 显式超时 + 降级走向量截断 |
| 3 | 首次本地加载模型 ~1-2 秒（只发生一次） | 懒加载 + 模块级单例，后续请求 0 加载开销；日志会提示 |
| 4 | enable_rerank=True 但 key 和本地模型都没有 | 日志打 warning，跳过 rerank，走向量截断 |
| 5 | rerank 后 DocumentChunk.score 从"向量相似度 1/(1+L2)"变成"rerank 0~1 相关性"，两批分不可横向比较 | 分数只做 chunk 间排序用，不做跨请求比较，不影响业务 |
| 6 | 保留条数从 6 变 3（启用 rerank 后默认） | 可显式设 `RERANK_TOP_N=6` 保持相同数量 |

---

## 五、验证清单

重启后端 + 刷新前端后验证：

| # | 验证项 | 操作 | 预期结果（后端日志关键字） |
|---|--------|------|---------------------------|
| 1 | 远端 rerank 正常走通 | `.env` 设 `RERANK_PROVIDER=remote` + SiliconFlow key，问知识库问题 | `[retrieve] raw_hits=X` → `[rerank] remote ...` 无 warning → `[retrieve] rerank OK, keep 3/X` |
| 2 | 本地 rerank 正常走通 | `.env` 设 `RERANK_PROVIDER=local`，确保 RAG-Pro 有 bge-reranker-v2-m3 模型 | `[rerank] 加载本地 FlagReranker: ...` → `[retrieve] rerank OK, keep 3/X` |
| 3 | auto 模式优先本地 | `.env` 设 `RERANK_PROVIDER=auto`，确认本地模型存在 | 日志显示走 local，不请求 remote |
| 4 | auto 模式回退 remote | 临时删本地模型目录，重启后端 | `未检测到本地 rerank 模型，auto 模式走 remote` → remote 正常执行 |
| 5 | 无配置降级 | `.env` 临时清空 `RERANK_API_KEY` + 设 `RERANK_PROVIDER=remote` | `跳过 remote：未配置` → `rerank 降级（...），按向量分数截断 top_N` |
| 6 | 超时降级 | 临时把 `RERANK_TIMEOUT=0.001`，问问题 | `remote 超时（>0.001s），降级` → 仍能正常回答（走向量截断） |
| 7 | 最终文档顺序变化 | 同一问题开/关 rerank 各问一次，对比返回 sources 列表 | 排序应该不同（rerank 会把更语义相关的顶上来） |
| 8 | rag_service.py 不用改仍能跑 | 直接走原有 ask_rag / ask_rag_stream | 所有流程不变；retrieve() 内部自动做 rerank，上层无需感知 |
| 9 | 意图识别仍能正常路由 | 四类问题（chat/file_list/kb_query/follow_up）各问一次 | 日志仍显示 intent=xxx，只有 kb_query/follow_up 走到检索阶段并触发 rerank |

**后端重启命令**（在 `backend` 目录下）：
```powershell
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**建议首次验证的最小 `.env` 配置**（远端模式，与 embedding 共用 SiliconFlow key）：
```bash
# Embedding（已有）
EMBEDDING_API_KEY=sk-your-siliconflow-key
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_MODEL=BAAI/bge-m3

# Rerank（复用 EMBEDDING 的 key/URL，不用重写）
RERANK_ENABLE=true
RERANK_PROVIDER=remote
RERANK_TOP_N=3
```

---

# 第五轮修改：前端调试面板（意图 / Query改写 / Rerank 排名变化可视化）

## 一、本轮修改总览

第三、四轮分别加了**意图路由 + Query 改写**和**Rerank 重排序**，但是否生效对用户完全是**黑盒**：
- 我不知道当前问题被分到哪个意图类别？（chat？kb_query？还是 follow_up？）
- 意图分类器有没有做 Query 改写？改成什么了？和我原话差多少？
- Rerank 有没有实际走通？走的本地还是远端？有没有自动降级？
- 每个 chunk 最终展示顺序和向量阶段原始排名比，到底上升/下降了多少名？哪些 chunk 被 rerank 顶上去了？

本轮在**检索层打标 → RAG 层组装事件 → 前端渲染面板**三层加调试信息透传：
1. **retriever_service.retrieve()** 把每个 chunk 的向量阶段排名 `__vec_rank` 和最终排名 `__final_rank` 注入 metadata，并返回结构化 debug_info
2. **rag_service.ask_rag_stream()** 所有分支在 `source` 事件前新增 `debug` SSE 事件，携带 `{intent, original_query, rewritten_query, retrieval}`
3. **前端 index.html** 新增 debug 事件监听，在来源块上方显示**可直观查看的调试面板**，并在每个 source tag 右下角加**排名变化徽章**（↗️上升 / ↘️下降 / ➖不变）

---

## 二、代码变更明细

### 2.1 `backend/app/services/retriever_service.py`（修改）

核心改动：`retrieve()` 返回签名从 `list[DocumentChunk]` 升级为 `tuple[list[DocumentChunk], dict]`。

```python
def retrieve(
    question: str,
    top_k: Optional[int] = None,
    threshold: Optional[float] = None,
    enable_rerank: Optional[bool] = None,
) -> tuple[list[DocumentChunk], dict]:
    # ... 原有向量大召回 / rerank / 截断逻辑 ...

    # --- 新增：打标 + 组装 debug_info ---
    # 1) 给向量大召回阶段的每个 chunk 打 1-based __vec_rank
    vec_rank_map: dict[str, int] = {}
    for i, (doc, score) in enumerate(raw_docs_scores):
        cid = doc.metadata.get("chunk_id") or str(i)
        vec_rank_map[cid] = i + 1

    # 2) 给最终输出的每个 chunk 同时注入 __vec_rank 和 __final_rank（1-based）
    chunks_debug: list[dict] = []
    for i, chunk in enumerate(chunks):
        vec_r = vec_rank_map.get(
            chunk.metadata.get("chunk_id") or f"fallback_{i}",
            len(raw_docs_scores),  # 找不到放一个比最终排最后的兜底值
        )
        final_r = i + 1
        # 原地注入到 chunk.metadata
        chunk.metadata["__vec_rank"] = vec_r
        chunk.metadata["__final_rank"] = final_r
        chunks_debug.append({
            "chunk_id": chunk.metadata.get("chunk_id"),
            "source_file": chunk.metadata.get("source_file"),
            "vec_rank": vec_r,
            "final_rank": final_r,
            "delta": vec_r - final_r,  # >0 上升，<0 下降，0 不变
            "relevance": chunk.score,
        })

    debug_info = {
        "raw_hits": len(raw_docs_scores),
        "final_count": len(chunks),
        "rerank_enabled": rerank_enabled,
        "rerank_backend": rerank_backend,   # "local" / "remote" / null (降级) / None (未启用)
        "top_relevance": chunks[0].score if chunks else 0.0,
        "chunks_debug": chunks_debug,
    }

    return chunks, debug_info
```

> **注意**：因为返回值变成 tuple，所有调用 `retrieve()` 的地方（rag_service.py 2 处）都要改成 `chunks, retrieval_debug = retrieve(...)` 接收。

---

### 2.2 `backend/app/services/rag_service.py`（修改）

三处改动：

#### 2.2.1 `ask_rag()`：从 3-tuple → 4-tuple（非流式调试接口）

```python
def ask_rag(...) -> tuple[str, str, list[DocumentChunk], dict]:
    """返回 (answer, thinking_text, chunks, debug_info)。
    debug_info: {intent, original_query, rewritten_query, retrieval}
    """
    ...
    if intent == INTENT_FILE_LIST:
        ...
        debug = {"intent": intent, "original_query": question, "rewritten_query": None, "retrieval": None}
        return answer, "", [], debug
    if intent == INTENT_CHAT:
        ...
        debug = {"intent": intent, "original_query": question, "rewritten_query": None, "retrieval": None}
        return answer, thinking_text, [], debug
    # kb_query / follow_up
    chunks, retrieval_debug = retrieve(rewritten_query, top_k=top_k)   # <-- tuple 解包
    ...
    debug = {
        "intent": intent,
        "original_query": question,
        "rewritten_query": rewritten_query if rewritten_query != question else None,
        "retrieval": retrieval_debug,
    }
    return answer, thinking_text, chunks, debug
```

#### 2.2.2 `ask_rag_stream()`：注释新增 debug 事件

SSE 事件顺序调整为：
```
debug → source → thinking → thinking_token → thinking_done → token → done → error
```

#### 2.2.3 `ask_rag_stream()`：4 分支都在 `source` 事件前 `yield debug`

- **file_list 分支**：`retrieval = null`，`rewritten_query = null`
- **chat 分支**：`retrieval = null`，`rewritten_query = null`
- **kb_query / follow_up 分支**：tuple 接收 `retrieve(...)`，`rewritten_query` 只有和原 question 不一样才非空

统一数据结构：
```javascript
// 前端能直接拿到的 debug data
{
  intent: "kb_query" | "follow_up" | "chat" | "file_list",
  original_query: "用户原话",
  rewritten_query: "改写后的 query" 或 null,
  retrieval: {          // chat/file_list 为 null
    raw_hits: 6,        // 向量大召回数
    final_count: 3,     // 最终输出数
    rerank_enabled: true,
    rerank_backend: "remote" | "local" | null,   // null = 已降级
    top_relevance: 0.85,
    chunks_debug: [ {chunk_id, source_file, vec_rank, final_rank, delta, relevance}, ... ]
  } | null
}
```

---

### 2.3 `backend/app/routers/chat_router.py`（修改）

L36 解包 ask_rag 返回值——原来只有 3 项，现在 ask_rag 返回 4 项，加第 4 项接收（不使用，仅避免 `ValueError: too many values to unpack`）：

```python
answer, thinking_text, sources, _debug_info = rag_service.ask_rag(...)
```

> chat_stream() 不用改：它遍历 rag_service.ask_rag_stream()，SSE 透传任何事件，自然也能把 debug 事件传给前端。

---

### 2.4 `frontend/index.html`（修改）

#### 2.4.1 数据层

```javascript
const lastSources = ref([]);
const lastDebugInfo = ref(null);   // ← 新增
```

3 处清空时机**与 lastSources 完全同步**（防止旧调试信息显示在新对话上）：
- 新建会话（`newConversation()`）
- 切换会话（`switchConversation(sid)`）
- 发送新消息前（`send()` 里 aiMsg 入队之后）

```javascript
lastSources.value = [];
lastDebugInfo.value = null;  // ← 三处都加这一行
```

`return {}` 导出：
```javascript
return {
  ...,
  isStreaming, lastSources, lastDebugInfo, enableDeepThink,
  ...
};
```

#### 2.4.2 SSE 事件循环新增 debug 监听

```javascript
if (event === 'source')  { lastSources.value = data || []; }
if (event === 'debug')   { lastDebugInfo.value = data || null; }   // ← 新增
```

#### 2.4.3 模板：调试面板 UI（sources 块上方）

```html
<!-- 调试面板（意图识别 / 查询改写 / Rerank 排名变化） -->
<div v-if="messages.length > 0 && lastDebugInfo" class="debug-panel">
  <div>
    <!-- 标题 -->
    <div class="debug-title"><span class="sparkle">✦</span><span>调试信息</span></div>

    <!-- ① 意图 pill（4 色） -->
    <div class="debug-row">
      <span class="debug-label">意图路由</span>
      <span class="intent-pill" :class="lastDebugInfo.intent">
        {{ {kb_query:'知识库查询', follow_up:'追问联想', chat:'日常闲聊', file_list:'文件列表'}[lastDebugInfo.intent] }}
      </span>
      <span class="debug-sub">（glm-4-flash 分类）</span>
    </div>

    <!-- ② Query 改写对比（只有改写了才显示） -->
    <div v-if="lastDebugInfo.rewritten_query" class="debug-row query-rewrite">
      <div class="debug-label">查询改写</div>
      <div class="query-compare">
        <div class="qc qc-old"><span class="qc-tag">原始</span><span class="qc-text">{{ original }}</span></div>
        <div class="qc-arrow">→</div>
        <div class="qc qc-new"><span class="qc-tag">改写后</span><span class="qc-text">{{ rewritten }}</span></div>
      </div>
    </div>

    <!-- ③ Rerank 状态条 + 说明（只有有检索时显示） -->
    <div v-if="lastDebugInfo.retrieval" class="debug-row">
      <span class="debug-label">检索 / Rerank</span>
      <span class="rerank-bar" :class="ok|fallback"><span class="bar-sq" :style="width%"></span></span>
      <span class="debug-sub">
        ✅ 本地 CrossEncoder / SiliconFlow 远端 API · Top 3/6 · 相关性 85%
        或 ⚠️ Rerank 调用失败，已自动降级
        或 仅向量检索（未启用 Rerank）· Top 6/6 · 相关性 72%
      </span>
    </div>
  </div>
</div>
```

#### 2.4.4 模板：source tag 排名变化徽章

每个 source tag 右下角新增一个**条件显示**的 `.rank-diff` 徽章：
- 只在 `rerank_enabled=true` 且 `rerank_backend != null`（即 rerank 真的执行了，不是未启用或已降级）时才显示
- 走 `metadata.__vec_rank` vs `metadata.__final_rank` 比较
  - `final < vec` → ↗️ 上升 X 名（绿）
  - `final > vec` → ↘️ 下降 X 名（红）
  - `相等` → ➖ 排名不变（灰）

```html
<div class="source-tag" v-for="(s,i) in lastSources" :key="i">
  <el-icon><Document /></el-icon>
  <span>{{ s.source_file }}</span>
  <span class="score">{{ (s.score*100).toFixed(0) }}%</span>
  <!-- 新增：Rerank 排名变化徽章 -->
  <span
    v-if="显示条件"
    class="rank-diff" :class="up|down|same"
  >↗️ 上升 N / ↘️ 下降 N / ➖ 排名不变</span>
</div>
```

#### 2.4.5 CSS 样式（追加到 `.sources` 样式之后）

关键类名：
- `.debug-panel`：760px 居中，毛玻璃半透明白底 + 细边框 + 9px 圆角，与 sources 列对齐
- `.intent-pill.kb_query | .follow_up | .chat | .file_list`：绿/紫/橙/蓝 4 色胶囊 + 细边
- `.query-compare / .qc / .qc-old / .qc-new / .qc-tag`：原始/改写 2 卡片对比
- `.rerank-bar`：110×8px 条形 + ok 绿 / fallback 橙 2 色填充
- `.rank-diff.up | .down | .same`：source tag 上的小徽章 3 色

---

## 三、debug 数据流向图

```
用户输入 question
  │
  ▼
classify_intent() ───────────────────────►  intent + rewritten_query
  │
  ▼
rag_service.ask_rag_stream 路由分支
  ├─ chat / file_list
  │     └─ retrieval = null
  │
  └─ kb_query / follow_up
        └─ retrieve(rewritten_query) ─► tuple(chunks, retrieval_debug)
                                            │
                                            ├─ 给 chunks[i].metadata 注入
                                            │    __vec_rank (向量阶段排名 1-based)
                                            │    __final_rank (最终排名 1-based)
                                            │
                                            └─ retrieval_debug = {
                                                  raw_hits, final_count,
                                                  rerank_enabled, rerank_backend,
                                                  top_relevance, chunks_debug
                                               }
  │
  ▼
组装统一 debug_payload
  { intent, original_query, rewritten_query, retrieval }
  │
  ▼
yield {"event": "debug", "data": payload}      ◄── SSE 新增事件，在 source 之前
  │
  ▼
前端 SSE 解析循环：
  event === 'debug' → lastDebugInfo.value = data
  │
  ▼
Vue 模板响应式渲染：
  ┌─ lastDebugInfo 非空 → .debug-panel 显示
  │     ① 意图 4 色 pill
  │     ② Query 改写对比（条件显示）
  │     ③ Rerank 状态条 + 文字说明（条件显示）
  │
  └─ source tag + 条件 rank-diff 徽章
        ↑↑ 走 chunk.metadata.__vec_rank vs __final_rank 差值渲染 ↑↑
```

---

## 四、潜在风险与边界

| # | 风险 | 缓解 |
|---|------|------|
| 1 | metadata 里的 `__vec_rank / __final_rank` 会不会泄漏给用户？ | 只注入不显示；前端只有调试面板+source tag 条件读取；不会出现在回答正文里 |
| 2 | retrieval = null（chat/file_list）时 UI 会不会报错？ | 所有 `.retrieval` 相关 DOM 都套了 `v-if="lastDebugInfo.retrieval"` |
| 3 | rewritten_query 与 original_query 完全相同时显示"改写对比"会很蠢 | 后端组装时直接把 rewritten_query 设 null，前端 `v-if` 整块不渲染 |
| 4 | rerank_backend = null（启用了但降级了）≠ rerank_enabled = false（完全没开） | 两种状态分别显示：前者 ⚠️ 橙色条 + 降级文字；后者"仅向量检索（未启用 Rerank）" |
| 5 | chat_router 里原有非流式接口返回 3-tuple 预期报错？ | 已显式把 chat_router.py L36 改成 `answer, thinking_text, sources, _debug_info = ...` 解包 |
| 6 | retrieve() 返回值改 tuple，其它模块调用会不会炸？ | 全项目 grep 只有 rag_service.py 2 处调用，均已同步改为 tuple 解包 |
| 7 | 新/切会话后 lastDebugInfo 仍显示旧数据？ | 3 处清空时机与 lastSources 同步 |

---

## 五、验证清单

重启后端 + 刷新前端后验证：

| # | 验证项 | 操作 | 预期结果 |
|---|--------|------|----------|
| 1 | chat 意图路由正确 | 发送 "你好呀 今天天气怎么样" | 面板意图=日常闲聊（**橙 pill**），检索/Rerank 行不显示，改写行不显示，sources 空 |
| 2 | file_list 意图路由正确 | 发送 "知识库有多少文件呀" | 面板意图=文件列表（**蓝 pill**），AI 直接返回文件清单文本，检索行不显示 |
| 3 | kb_query 意图 + Query改写 | 发模糊问题如 "我问过的那个明日方舟干员" | 意图=知识库查询（**绿 pill**）或追问联想（**紫 pill**）；若有改写则显示"原始 → 改写后"对比卡片 |
| 4 | Rerank 正常启用 · 远端 | `.env` 设 `RERANK_ENABLE=true` `RERANK_PROVIDER=remote` + key 配置正确 | 检索/Rerank 行显示 ✅ SiliconFlow 远端 API · Top 3/6 · 相关性 8X%；进度条为**绿色** |
| 5 | Rerank 正常启用 · 本地 | `.env` 设 `RERANK_PROVIDER=local` 且本地模型存在 | 显示 ✅ 本地 CrossEncoder · Top N/M；进度条**绿色** |
| 6 | Rerank 已启用但自动降级 | `RERANK_PROVIDER=remote` 临时清空 API KEY 重启后端 | 条为**橙色** + ⚠️ "Rerank 调用失败，已自动降级为向量排序"；chunk 仍有结果，不会白屏 |
| 7 | Rerank 未启用 | `.env` 设 `RERANK_ENABLE=false` | 显示 "仅向量检索（未启用 Rerank）· Top 6/6 · 相关性 XX%" |
| 8 | source tag 排名变化徽章（↑4 的启用 rerank 状态下） | 看每个 source tag 右下角 | 出现 ↗️ 上升 N / ↘️ 下降 N / ➖ 排名不变 小徽章；↑6 ↑7 的状态徽章不显示 |
| 9 | 新建会话清空面板 | 点击"新建会话" | 调试面板消失；再发新消息时才重新出现对应面板 |
| 10 | 切换会话清空面板 | 从对话 A 切到对话 B（B 没发过新消息） | 调试面板消失 |
| 11 | 后端非流式接口不报错 | `POST /api/chat` 随便问一句 | HTTP 200 正常返回 ChatResponse，无 500 ValueError unpack |

---

# 第六轮修改：父块上下文（Parent Chunk Context）

生成时间：2026-08-18

---

## 一、本轮修改总览

检索到的 chunk 因为切块策略（500 字符/块），可能只包含问题相关信息的**片段**，缺少前后文语境。比如：
- 问"它的副作用是什么？"，检索到的 chunk 只包含"副作用包括头晕"而没有前半句"该药物..."
- 问"蜂医怎么部署？"，检索到的 chunk 只包含"部署在关键路口"而没有前半句"蜂医的部署位置..."

本轮在**索引阶段**为每个 chunk 注入"父块上下文"（前一块 + 自身 + 后一块的合并内容）到 `metadata.parent_content` 中，检索时由已有的 `_format_context()` 自动优先使用该扩展上下文，让 LLM 获得更完整的语境。

| # | 改动 | 涉及文件 |
|---|------|----------|
| 1 | 为每个 chunk 注入相邻 3 块合并的 parent_content | `backend/app/services/indexer_service.py` |

### 设计目标

```
索引阶段（indexer_service.py）：
  文档切块 → [chunk0, chunk1, chunk2, chunk3, ...]
    ↓
  为每个 chunk 计算 parent_content：
    chunk0: chunk0 + chunk1 的合并内容（无前一块）
    chunk1: chunk0 + chunk1 + chunk2 的合并内容
    chunk2: chunk1 + chunk2 + chunk3 的合并内容
    ...
    chunkN: chunkN-1 + chunkN 的合并内容（无后一块）
    ↓
  存入 metadata["parent_content"] → 向量库

检索阶段（retriever_service.py → prompt_templates.py）：
  向量检索 → 命中 chunk1
    ↓
  DocumentChunk.metadata["parent_content"] = chunk0+chunk1+chunk2
    ↓
  _format_context() 优先读取 parent_content
    ↓
  LLM 获得 chunk0+chunk1+chunk2 的完整上下文
```

**关键特性**：
- 仅在索引阶段修改，检索和生成阶段零改动
- parent_content 存在于 metadata 中，随 chunk 一起存入向量库并检索回来
- 提示模板中 `_format_context()` 已优先使用 `parent_content`，无需额外修改
- 对已有索引的文件：需要重新索引（删除后重新上传）才能生效

---

## 二、各文件详细修改

### 2.1 `backend/app/services/indexer_service.py`（修改）

在"给每个块打元数据（file_id / source）"之后新增父块上下文注入逻辑。

**改动位置**：`index_uploaded_file()` 函数内，第 161-170 行。

```python
# 为每个 chunk 注入父块上下文（前一块 + 自身 + 后一块的合并内容）
# 让 LLM 在检索时获得更完整的语境，避免因切块导致信息碎片化
for i, ch in enumerate(chunks):
    parts = []
    if i > 0:
        parts.append(chunks[i - 1].page_content)
    parts.append(ch.page_content)
    if i < len(chunks) - 1:
        parts.append(chunks[i + 1].page_content)
    ch.metadata["parent_content"] = "\n\n".join(parts)
```

**实现细节**：
- 使用 `chunks[i-1].page_content`（前一块文本）+ `ch.page_content`（自身文本）+ `chunks[i+1].page_content`（后一块文本）三块合并
- 块间用 `\n\n` 分隔（与原始文档段落分隔一致）
- 首块（i=0）只有自身 + 后一块
- 尾块（i=len-1）只有前一块 + 自身
- 单块文档（len=1）只有自身

---

## 三、数据流验证

```
用户上传"明日方舟.txt"
  ↓
index_uploaded_file()
  ↓
split_documents() → 产生 4 个 chunk
  ↓
for i, ch in enumerate(chunks):          ← 新增循环
    ch.metadata["parent_content"] = "前一块 + 自身 + 后一块"
  ↓
store.add_documents(chunks)              ← 含 parent_content 的 metadata 入库
  ↓
用户提问 → retrieve(rewritten_query)
  ↓
similarity_search_with_score()           ← 返回的 Document 含 parent_content 在 metadata
  ↓
DocumentChunk(metadata=dict(doc.metadata))
  ↓
build_rag_messages() → _format_context()
  ↓
meta.get("parent_content") →            ← 优先命中
    → "[资料1] 文件名\n前一块内容\n\n自身内容\n\n后一块内容"
  ↓
LLM 收到完整上下文，回答更准确
```

---

## 四、潜在风险与边界

| # | 风险 | 缓解 |
|---|------|------|
| 1 | 已有索引的文件不会自动获得 parent_content，需要重新上传 | 删除文件后重新上传即可；diff.md 已标注 |
| 2 | parent_content 可能比原有的 content 长 2-3 倍，增加 token 消耗 | 合理：LLM 本就需要更多上下文才能准确理解；token 增加量 = chunk_size × 2 ≈ 1000 tokens/块，总 token 仍在可控范围 |
| 3 | 相邻块可能是无关内容（如文档不同章节交接处） | 概率低：分块策略按自然段落/句子切分，相邻块通常是连续的同一话题 |
| 4 | 如果 chunk 本身已经足够长，parent_content 增加冗余 | 默认 500 字符/块，叠加 3 块 ~1500 字符，对 glm-4.5-flash 的 128K 上下文窗口来说完全可以接受 |
| 5 | parent_content 在 debug 面板中显示为 content 而非 parent_content | 调试面板显示的是 `chunk.content`（chunk 自身内容），不影响 LLM 实际使用的上下文 |

---

## 五、验证清单

重启后端（**必须**） + 刷新前端后验证：

| # | 验证项 | 操作 | 预期结果 |
|---|--------|------|----------|
| 1 | 新上传文件含 parent_content | 删除"明日方舟.txt" → 重新上传 → 问文档中的问题 | AI 回答内容更饱满，能引用相邻块的信息；后端日志无异常 |
| 2 | 旧文件不受影响 | 上传一个新文件并询问 | 旧文件仍可正常检索回答（只是没有 parent_content 加成） |
| 3 | 单块文档 | 上传一个极短文件（<500 字符） | 正常索引，parent_content = 自身内容 |
| 4 | 不影响已有用户 | 发送闲聊（"你好"） | 意图路由正常，不走检索，不受 parent_content 影响 |
| 5 | 不影响已有 RAG 流程 | 关闭 Rerank / 不启用深度思考 | 所有功能正常，parent_content 只在 LLM 生成时增加上下文，不影响其他流程 |

---

# 第七轮修改：多种分块策略（recursive / intelligent / table / parent_child）

生成时间：2026-08-18

参考项目：`D:\ai学习项目\RAG-Pro\backend\app\core\chunker.py`

---

## 一、本轮修改总览

**核心目标**：借鉴 RAG-Pro 的多策略分块器，从原来仅有的 `recursive` 一种，扩展为 **4 种可选用法**，根据文件类型/配置自动选择最合适的切块方案，提升检索召回质量。

| # | 改动点 | 说明 | 涉及文件 |
|---|--------|------|----------|
| 1 | 新增 3 种分块器 | `IntelligentChunker`、`TableChunker`、`ParentChildChunker`（原只有 `RecursiveChunker`） | `backend/app/splitters/text_splitter.py` |
| 2 | 新增分块方法配置 | `.env` 中 `CHUNK_METHOD` 可指定全局默认（recursive/intelligent/table/parent_child） | `backend/app/config.py` |
| 3 | 索引端接入多策略 | 上传文件时按扩展名启发式选策略（CSV/Excel→table，其他→CHUNK_METHOD 默认）；parent_child 走两层分块专用路径，子块 metadata 直接写入真正的父块完整文本 | `backend/app/services/indexer_service.py` |
| 4 | 检索 debug 扩字段 | debug_info 的 chunks_debug 新增 `chunk_method`、`has_parent_content`，调试面板能看出每块的来源策略 | `backend/app/services/retriever_service.py` |

**4 种分块方法对比**：

| 方法 | 适用文档 | 特点 | 父块上下文 |
|------|----------|------|------------|
| `recursive`（默认） | 通用文本 | 按 \n\n→\n→句末→逗号→空格→逐字 递归切，overlap 重叠块尾 | 相邻 3 块拼接（近似） |
| `intelligent` | 有章节结构的文档（书籍/论文/规范） | 先识别 `# / 第X章 / 序号标题` 等标题行做结构分块；过短章节自动合并；超长章节退回 recursive | 相邻 3 块拼接（近似） |
| `table` | CSV / Excel / Markdown 表格 | CSV 每一行前缀带完整表头列名，MD 表格整张保留，非表格部分退回 recursive | 相邻 3 块拼接（近似） |
| `parent_child` | 对上下文精度要求高的文档 | 真正的两层分块：父块 1536 token(0 overlap) → 子块 512 token(64 overlap)。**检索的是子块，给 LLM 的是完整父块文本**，避免相邻 3 块拼接把不相关内容混进来 | 子块命中后取**真实父块完整内容**（精确） |

---

## 二、各文件详细修改

### 2.1 `backend/app/splitters/text_splitter.py`（完全重写）

#### 2.1.1 统一 TextChunk 数据结构（与 RAG-Pro 对齐）

新增 `ChunkMethod = Literal["recursive", "intelligent", "table", "parent_child"]` 类型标注；`TextChunk` 增补 `parent_chunk_index` 字段，专门用于 parent_child 模式。

#### 2.1.2 RecursiveChunker 内部接口改造成 chunk_pages

原来只有 `chunk_documents(LangChain Doc) → LangChain Doc`，现在增加中间层 `chunk_pages(list[dict]) → list[TextChunk]`，其他 3 个分块器复用同样的 pages → TextChunk 模式，最后统一走 `_text_chunks_to_langchain()` 转 LangChain Document。

#### 2.1.3 新增 IntelligentChunker

- `_detect_sections()`：正则 `^(#{1,6}\s+.+|第[一二三四五六七八九十\d]+[章节部分].+|[一二三四五六七八九十\d]+[、\.]\s*.+)$` 匹配标题行，切成 `{title, text}` 段
- `_merge_small_sections()`：过小章节（<min_chunk_tokens=50）自动与下一段合并，避免出现过碎的 chunk
- 超过 chunk_size 的段：构造临时 page，退回 `RecursiveChunker.chunk_pages()` 递归切

#### 2.1.4 新增 TableChunker

- CSV/TSV/XLSX（metadata 含 `headers`）：每一行用 `f"表格数据（列：{', '.join(headers)}）\n{line}"` 包装，确保检索"某列最大值是多少"这种行级 query 时，chunk 里直接带列名，语义自足
- Markdown 表格：正则 `(\|.+\|[\r\n]+\|[-:\s|]+\|[\r\n]+(?:\|.+\|[\r\n]+)+)` 提取整张表作单 chunk
- 非表格内容：构造临时 page，退回 RecursiveChunker

#### 2.1.5 新增 ParentChildChunker（核心改动）

```
第一层：parent_chunker = RecursiveChunker(parent_size=1536, overlap=0)
第二层：child_chunker  = RecursiveChunker(child_size=512, overlap=64)
```

流程：
1. 先用 `parent_chunker` 把文档切成大块父块（overlap=0，父块之间不重叠）
2. 对每一个父块，构造临时 page，用 `child_chunker` 切成子块
3. 每个子块写入：
   - `parent_chunk_index = parent.chunk_index`（引用）
   - `child.metadata["parent_content"] = parent.text`（**完整父块文本直接入库**，检索时省得再查 DB）

`chunk_pages()` 返回 `(child_chunks, parent_chunks)` tuple；`chunk_documents()` 只返回子块（兼容对外统一入口）。

#### 2.1.6 工厂函数 get_chunker() + 兼容接口 split_documents()

```python
def get_chunker(
    method: ChunkMethod = "recursive",
    chunk_size: int = 512,       # tokens
    chunk_overlap: int = 64,     # tokens
    min_chunk_size: int = 50,    # tokens，仅 intelligent
): ...

def split_documents(
    documents: list[Document],
    chunk_size: int = 500,        # 传字符数，与老接口兼容
    chunk_overlap: int = 80,
    method: ChunkMethod = "recursive",   # 新增参数
) -> list[Document]: ...
```

字符数→tokens 换算规则保持不变：`token_size = max(256, chars // 2)`。

### 2.2 `backend/app/config.py`（新增 CHUNK_METHOD）

在 `RAG 超参数` 区块顶部新增：

```python
# 分块方法: recursive(递归) / intelligent(按章节) / table(表格优化) / parent_child(两层父子分块)
CHUNK_METHOD = os.getenv("CHUNK_METHOD", "recursive").strip().lower()
```

默认值 `recursive`，保持与现有行为完全一致，不破坏老用户配置。

### 2.3 `backend/app/services/indexer_service.py`

#### 2.3.1 导入扩展

从 config 导入 `CHUNK_METHOD`；从 splitters 导入 `get_chunker` 和 `ChunkMethod`。

#### 2.3.2 新增 `_guess_chunk_method(filename, sample_text)` 启发式判定

```
CSV/XLSX/XLS/TSV 扩展名 → "table"
文本里 |: 出现 或 连续两行 |（Markdown 表格特征）→ "table"
其他 → CHUNK_METHOD 配置值（合法性校验，非法值退回 "recursive"）
```

这是一种**启发式**——用户随时可以通过 `.env` 里指定 `CHUNK_METHOD=parent_child` 覆盖全部文件。

#### 2.3.3 `index_uploaded_file()` 分两路分块

```
method == "parent_child"?
  ├─ YES → 构造 pages → chunker.chunk_pages(pages) → 拿 (child_chunks, _)
  │        → _text_chunks_to_langchain 转 LangChain Doc
  │        → 子块 metadata 已自带 parent_content（chunk_pages 写入）
  └─ NO  → split_documents(docs, method=method) → 返回 LangChain Doc
           → 非 parent_child 方法，再走相邻 3 块拼接注入 parent_content
```

每块还会额外打 `ch.metadata["chunk_method"] = method`，用于检索阶段 debug。

#### 2.3.4 返回值新增 chunk_method

`index_uploaded_file()` 返回的 dict 新增 `"chunk_method": method` 字段，方便前端以后做 UI 展示（比如"这个文件用了什么分块"）。

### 2.4 `backend/app/services/retriever_service.py`

在第三步 DocumentChunk→chunks_debug 映射里扩字段：

```python
chunks_debug.append({
    "chunk_id": chunk.chunk_id,
    "source_file": chunk.source_file,
    "chunk_method": meta.get("chunk_method") or "legacy_parent_adjacent",
    "has_parent_content": bool(meta.get("parent_content")),
    "vec_rank": vec_rank,
    "final_rank": final_rank,
    "change": change,
})
```

`prompt_templates.py` 里 `_format_context()` 的 `meta.get("parent_content")` 逻辑无需改动，天然兼容两种注入方式（相邻 3 块拼接 / 父块真实文本）。

---

## 三、切换方法步骤

### 方法 1：全局默认（改 .env）

```env
# 可选值: recursive / intelligent / table / parent_child
CHUNK_METHOD=parent_child
```

改完重启后端，新上传的文件全部走指定策略。

### 方法 2：对某个文件单独指定（当前实现）

当前实现是**按扩展名启发式 + .env 全局默认**。如果后续要做"上传弹窗里选分块方式"，只需在 index_router 里接收一个 `chunk_method` 表单字段，传给 `index_uploaded_file(file_bytes, filename, method=...)`。

---

## 四、潜在风险与边界

| # | 风险 | 缓解 |
|---|------|------|
| 1 | 老索引文件 chunk_method/parent_content 字段不全 | `chunks_debug` 已经退回 `"legacy_parent_adjacent"`，LLM 侧无感知；想享受新策略只需重传 |
| 2 | parent_child 子块数量比其他策略多 ~3 倍（因为 child 比父块碎） | ChromaDB 本地文件存储，容量不是瓶颈；子块越小，嵌入向量对局部语义越敏感，召回更准 |
| 3 | intelligent 的章节标题正则未覆盖"无编号标题"场景 | 标题没被检测到的段会走 `if current["text"].strip()` 兜底段落，语义还是完整的，只是没拿到 section_title |
| 4 | table 对 CSV 中文列名做 `', '.join(headers)` 如果列多会显得长 | 合理：列名就是语义的一部分，对"XX 列的 XX"这种查询有决定性提升；实在过长可在 chunk_size 估算下自然切分 |
| 5 | CHUNK_METHOD 写错名字 | `_guess_chunk_method` 做了白名单校验，非法值退回 `recursive`，不会中断流程 |

---

## 五、验证清单

**重启后端 + 重新上传文件后**验证（父块上下文需要重建索引才生效，老 chunk 没有新字段）：

| # | 验证项 | 操作 | 预期结果 |
|---|--------|------|----------|
| 1 | 默认 recursive 仍正常 | 不传 CHUNK_METHOD .env，上传 txt 提问 | 回答正常；chunks_debug 中 chunk_method="recursive" |
| 2 | CSV 自动选 table | 上传一个 .csv 文件，问其中一列的数值 | 每一行 chunk 都带 "表格数据（列：xxx）"前缀，AI 直接定位行 |
| 3 | parent_child 生效 | CHUNK_METHOD=parent_child，上传文本后提问 → 查 debug | chunks_debug 中 chunk_method="parent_child"，has_parent_content=true |
| 4 | 父块上下文优于相邻 3 块 | 用 parent_child 模式上传，问跨子块但在同一父块的问题 | LLM 引用连续父块内容，不会出现"相邻块交界是章节边界"的拼接噪声 |
| 5 | intelligent 识别章节 | 把一份有"第1章…第2章…"的文件上传，问"第2章讲了什么" | chunk.section_title 记录了章节标题，debug 面板可验证 |
| 6 | 不破坏 rerank / 意图 / 深度思考 | 深度思考+Rerank+知识库查询组合提问 | 所有原有功能流程正常，debug 事件 token 输出符合预期 |
| 7 | 不影响闲聊分支 | 发送 "你好" + "你是谁" | 意图识别 chat → 直接回答，不触发检索流程 |

---

# 第八轮修改：上传与分块分离 + 前端分块方式选择器

生成时间：2026-08-18

参考项目：`D:\ai学习项目\RAG-Pro`（两段式上传-分块设计）

---

## 一、本轮修改总览

**核心目标**：参考 RAG-Pro 的两段式设计，将"上传文件"和"分块入库"拆成两个独立步骤，用户上传文件后在前端选择分块方式，再执行分块入库。

| # | 改动点 | 说明 | 涉及文件 |
|---|--------|------|----------|
| 1 | 上传与分块分离 | 原 `index_uploaded_file()` 拆为 `upload_and_parse()` + `chunk_and_store()` | `backend/app/services/indexer_service.py` |
| 2 | 新增 3 个 API 端点 | `POST /{file_id}/chunk`（分块入库）、`GET /methods`（分块方式列表）；原有 `POST /upload` 改为仅解析 | `backend/app/routers/index_router.py` |
| 3 | Schema 扩展 | 新增 `ChunkRequest`、`ChunkResponse`、`ChunkMethodItem`；`IndexStatusResponse` 新增 `chunk_method` 等字段 | `backend/app/models/schemas.py` |
| 4 | 前端分块选择器 | 文件列表中，待分块文件显示下拉选 + 执行按钮；已入库文件显示分块方式标签 | `frontend/index.html` |

**流程对比**：

```
旧流程：上传文件 → 自动分块(递归) → 自动入库 → 返回"切成 N 块"
新流程：上传文件 → 解析(不分块) → 用户选分块方式 → 执行分块 → 入库 → 返回"切成 N 块"
```

---

## 二、各文件详细修改

### 2.1 `backend/app/services/indexer_service.py`（完全重写）

#### 核心拆分

原 `index_uploaded_file()` 拆为两个函数：

| 函数 | 职责 | 返回 |
|------|------|------|
| `upload_and_parse(file_bytes, filename)` | 保存到磁盘 → Load 解析文档 → 缓存到 `_parsed_docs` 内存 | `{file_id, status: "parsed", chunks_count: 0}` |
| `chunk_and_store(file_id, chunk_method)` | 从内存/磁盘取文档 → Split 分块 → Embed 嵌入 → Store 入库 | `{file_id, chunks_count, chunk_method, status: "success"}` |

#### 新增数据结构

- `CHUNK_METHODS_INFO`：4 种分块方式的元数据列表，供 `GET /methods` 返回
- `IndexProgress` 新增 `chunk_method` 字段，记录已使用的分块方式
- `_parsed_docs: dict[str, list[LCDocument]]`：内存缓存解析后的文档，避免重复解析

#### `_restore_from_disk()` 改进

恢复时检查向量库中是否有该 file_id 的 chunk：
- 有 chunk → `status="done"`
- 无 chunk → `status="parsed"`（待分块）

#### `chunk_and_store()` 容错

- 内存缓存丢失（如重启后）→ 从磁盘重新解析文件
- 文件已分块 → 报错"请先删除后重新上传"
- 分块方法校验 → 不合法报错

### 2.2 `backend/app/routers/index_router.py`

新增端点：

| 端点 | 方法 | 说明 |
|------|------|------|
| `POST /api/index/{file_id}/chunk` | POST | 接收 `{chunk_method}` JSON body，执行分块入库 |
| `GET /api/index/methods` | GET | 返回可用分块方式列表 |

修改端点：

| 端点 | 变更 |
|------|------|
| `POST /api/index/upload` | 从"上传+分块+入库"改为"仅上传+解析" |

### 2.3 `backend/app/models/schemas.py`

新增模型：
- `ChunkRequest`：分块请求体（`chunk_method` 字段）
- `ChunkResponse`：分块入库响应（含 `chunks_count` + `chunk_method`）
- `ChunkMethodItem`：分块方式信息（`value` / `label` / `description` / `scenario`）

修改模型：
- `IndexFileResponse`：`chunks_count` 默认 0，`status` 默认 `"parsed"`
- `IndexStatusResponse`：新增 `chunk_method`、`file_size`、`file_ext` 字段

### 2.4 `frontend/index.html`

#### HTML 变更

文件列表卡片改为条件布局：
- `status === "parsed"`：卡片变列布局，下方显示分块方式下拉框 + 执行分块按钮
- `status === "done"`：显示块数 + 分块方式标签（如"父子分块"）
- `status === "indexing"`：显示"处理中"

#### CSS 新增

- `.file-card--column`：列布局（待分块时展开）
- `.chunk-row`：分块选择行（下拉框 + 按钮横排）

#### JS 新增

- `chunkMethods` ref + `loadChunkMethods()`：从 `GET /api/index/methods` 加载
- `getChunkMethodLabel(value)`：值→标签映射
- `executeChunk(f)`：调 `POST /{file_id}/chunk`，成功后刷新文件列表
- `loadIndexedFiles()`：给每个文件加 `selected_method` 默认值和 `_chunking` 状态
- `customUpload()`：提示语从"入库成功"改为"解析成功，请选择分块方式"

---

## 三、验证清单

**重启后端 + 刷新前端后**验证：

| # | 验证项 | 操作 | 预期结果 |
|---|--------|------|----------|
| 1 | 上传只解析不分块 | 上传一个 txt 文件 | 提示"解析成功，请选择分块方式"；文件列表显示"待分块"标签 + 下拉框 + 执行按钮 |
| 2 | 选择分块方式后执行 | 选"递归分块" → 点"执行分块" | 提示"分块成功，切成 N 块"；文件状态变为"已入库"，显示块数和分块方式 |
| 3 | 父子分块 | 选"父子分块" → 执行 | 成功入库，debug 面板 chunk_method="parent_child" |
| 4 | 表格分块 | 上传 CSV → 选"表格分块" → 执行 | 成功入库，每行 chunk 带表头前缀 |
| 5 | 重启后恢复 | 上传文件不执行分块 → 重启后端 | 文件列表仍显示"待分块"，可正常选择方式执行分块 |
| 6 | 已分块文件不可重复 | 对"已入库"文件尝试再次分块 | 报错"文件已分块入库，请先删除后重新上传" |
| 7 | 删除后重新上传 | 删除文件 → 重新上传 → 选不同方式分块 | 全流程正常，新分块方式生效 |
| 8 | 对话功能不受影响 | 分块入库后正常提问 | RAG 问答正常，debug 面板显示 chunk_method |

---

# 第九轮修改：Query 改写 + Hybrid Search + 置信度评分 + 关键词/问题生成

生成时间：2026-08-18

---

## 一、本轮修改总览

| # | 功能 | 说明 | 涉及文件 |
|---|--------|------|----------|
| 1 | Query 改写 | >50字用glm-4-flash压缩为关键词 | `retriever_service.py` |
| 2 | Hybrid Search | dense+BM25 sparse → RRF融合 | `vector_store.py`、`utils/bm25.py` |
| 3 | 置信度评分 | 0.6*top+0.4*avg 加权分级 | `retriever_service.py` |
| 4 | 关键词+问题生成 | 每chunk提取5关键词+生成3问题 | `keyword_service.py`、`indexer_service.py` |

## 二、新增文件

- `backend/app/services/keyword_service.py`：extract_keywords() + generate_questions()
- `backend/app/utils/bm25.py`：BM25Index + rrf_fuse()

## 三、修改文件

### retriever_service.py
- rewrite_query()：空壳 → 真正LLM改写（>50字触发）
- retrieve()：新增enable_hybrid参数；debug_info新增confidence/confidence_label/search_mode/rewritten_query
- 修复rerank分支的d[0].metadata bug → d.metadata

### vector_store.py
- 新增get_all_documents() + hybrid_search()

### indexer_service.py
- chunk_and_store()：分块时自动提取keywords和questions存入metadata

## 四、验证清单

| # | 验证项 | 预期结果 |
|---|--------|----------|
| 1 | Hybrid Search | debug面板显示search_mode=hybrid |
| 2 | 短query不改写 | rewritten_query=null |
| 3 | 长query改写 | rewritten_query为压缩后关键词 |
| 4 | 置信度 | 显示confidence值和label |
| 5 | 关键词提取 | has_keywords=true |
| 6 | 问题生成 | has_questions=true |
| 7 | BM25稀疏检索 | 关键词命中文档排名上升 |
| 8 | RRF融合 | 两路都命中文档排名更靠前 |
| 9 | Rerank不受影响 | hybrid结果上rerank正常 |
| 10 | 对话正常 | chat和kb_query分支不受影响 |
| 11 | 空库不报错 | 文档数0/1时降级为dense |

---

# 第十轮修改：Multi-Query 多查询分解 + HyDE 假设文档（查询重构彻底重构）

生成时间：2026-08-19

参考：用户提供的"四大查询重构技术"（提示工程 / 多查询分解 / 退步提示 / HyDE）+ RAG-Pro

---

## 一、问题背景

第九轮的 query 改写存在两个问题：
1. **两条改写链路互相打架**：debug 面板显示的 `rewritten_query` 是 intent 层的（其 prompt 规定 kb_query 时原样返回），而检索层的关键词改写在 `retriever_service` 里，用户根本看不到
2. **50 字阈值**：检索层改写只对 >50 字的 query 触发，大部分 query 直接跳过；且单条改写幅度太小（LLM 倾向最小改动）

## 二、本轮方案：多查询分解 + HyDE（四大技术中的两个）

```
question
  → intent 层（不变：意图分类 + follow_up 指代消解）
  → 检索层查询重构（新增）：
      ① Multi-Query 分解：LLM 一次调用生成
         主查询（关键词+同义词扩展）+ 2 个子查询（不同视角拆解）
      ② HyDE（可选）：LLM 生成一段"假设答案"文档
  → 多路检索：每个查询 dense + BM25 sparse 各一路
     + HyDE 文档 dense 一路 → 全部 RRF 融合
  → rerank（用主查询） → 截断 → 置信度
```

解决"改写幅度太小"的根本方式：**不再依赖单条改写，而是拆成多条互补查询**——涵盖面广的问题（如"各种玩法+特殊玩法+长期玩法"）会被拆成多路分别检索，各自召回再融合。

## 三、代码变更

| 文件 | 变更 |
|---|---|
| `config.py` | 新增 `MULTI_QUERY_ENABLE`(默认true) / `MULTI_QUERY_COUNT`(3) / `HYDE_ENABLE`(默认false) / `HYDE_DOC_LEN`(200) |
| `retriever_service.py` | 删除单条 rewrite_query 的 LLM 改写；新增 `generate_multi_queries()`（Multi-Query JSON 分解 + HyDE 生成，失败降级为 [原问题]）；`retrieve()` 接入 `hybrid_search_multi`；debug_info 用 `search_queries`/`hyde_doc` 替代 `rewritten_query` |
| `vector_store.py` | 新增 `hybrid_search_multi(queries, hyde_doc, k)`：BM25 索引只建一次，每查询 dense+sparse 各一路，HyDE 加一路 dense，递归 RRF 融合；旧 `hybrid_search()` 变为兼容壳 |
| `.env.example` | 新增"查询重构"配置区块 |
| `frontend/index.html` | debug 面板新增"多查询分解"区块（主/子查询标签分色）+ "HyDE 假设文档"区块（开启时显示） |

## 四、前端 debug 面板新结构

```
✦ 调试信息
├── 意图路由：[知识库查询]（glm-4-flash 分类）
├── 查询改写（仅 follow_up 追问时显示：原始 → 改写后）
├── 多查询分解（3 路 · RRF 融合）
│   ├── [主] 明日方舟 游戏 玩法 特殊玩法 活动玩法 长期玩法 常驻玩法
│   ├── [子1] 明日方舟 日常玩法 系统功能
│   └── [子2] 明日方舟 活动 限时玩法 常驻内容
├── HyDE 假设文档（HYDE_ENABLE=true 时显示）
└── 检索 / Rerank：状态条 + Top N/M + 相关性
```

## 五、性能说明

- Multi-Query：+1 次 glm-4-flash 调用（~200ms）+ 每路 1 次 dense 嵌入（3 路 ~300ms）
- HyDE（默认关）：额外 +1 次 LLM + 1 次嵌入
- BM25 索引每次检索重建（当前文档量小可接受；未来优化：chunk 入库时增量维护索引）

## 六、验证清单

重启后端（需彻底重启，勿依赖 --reload）+ 强刷前端后：

| # | 验证项 | 操作 | 预期结果 |
|---|--------|------|----------|
| 1 | 多查询分解生效 | 提问"我想了解一下明日方舟这个游戏里面的各种玩法，特别是一些特殊玩法和长期玩法" | debug 面板显示 3 路查询，主查询为关键词组合 |
| 2 | 主查询关键词化 | 同上 | 主查询不再是原句，而是"明日方舟 游戏 玩法 …"形式 |
| 3 | 子查询差异化 | 同上 | 子查询聚焦不同侧面（日常玩法 / 活动玩法） |
| 4 | 短问题也分解 | 提问"蜂医是什么"（6 字以上） | 也会分解为多查询（无 50 字门槛） |
| 5 | RRF 融合生效 | 后端日志 | `[hybrid_multi] queries=3, routes=6+, fused=N` |
| 6 | Rerank 正常 | 开启 rerank 提问 | rerank 在多路融合结果上重排，相关性显示正常 |
| 7 | HyDE 开关 | .env 设 HYDE_ENABLE=true 重启 | debug 面板显示"HyDE 假设文档"区块 |
| 8 | LLM 失败降级 | 断网/改错 API key 提问 | 降级为单条原问题检索，不报错 |
| 9 | 追问仍正常 | 先问蜂医，再问"它有什么功效" | intent 层改写为"蜂医有什么功效"（原有功能不变） |
| 10 | 检索质量提升 | 同一长问题对比第十轮前后回答 | 覆盖面更全（特殊玩法/长期玩法都有内容） |

---

# 第十轮·补丁 A：修复多查询子查询差异化不足

生成时间：2026-08-19

## 一、问题背景

第十轮上线后实测发现：多查询分解虽然生效（debug 面板显示 3 路），但**子查询互相几乎相同**。

例如用户问"明日方舟肉鸽模式、基建系统、干员养成、危机合约"时，模型输出：

```
主：明日方舟 肉鸽模式 基建系统 干员养成 危机合约 长期玩法
子1：明日方舟 肉鸽模式 基建系统 干员养成 危机合约 玩法介绍   ← 几乎相同，只换后缀
子2：明日方舟 肉鸽模式 基建系统 干员养成 危机合约 系统特点   ← 几乎相同，只换后缀
```

**根因**：Prompt 只要求"从不同视角拆分"，但未提供足够有区分度的示例，LLM 倾向最小改动，只在相同内容上替换尾缀。这样的多路检索毫无意义——每路召回基本相同的文档，RRF 融合退化为单路。

## 二、修复方案：强化子查询差异化约束

修改 `retriever_service.py` 的 `_MULTI_QUERY_SYSTEM` Prompt：

| 改动 | 旧 | 新 |
|---|---|---|
| 子查询规则 | "从不同视角拆解，聚焦一个侧面"（模糊） | **"每个子查询必须聚焦原问题中一个独立的子话题/子领域，关键词交集应 < 50%"**（硬约束） |
| 显式禁止 | 无 | **"❌ 禁止在相同内容上换后缀（如玩法介绍/系统特点/内容说明——这是无效的）"** |
| 示例 | "明日方舟 日常玩法 系统功能"（仍含全部关键词） | **明日方舟肉鸽/基建/干员养成/危机合约逐个拆开**（每个子查询只含对应子话题少数关键词）|
| 额外示例 | 无 | 增加番茄工作法示例（原理 vs 应用场景） |

## 三、代码变更

| 文件 | 变更 |
|---|---|
| `retriever_service.py` L67-L99 | 重写 `_MULTI_QUERY_SYSTEM`：明确禁止换后缀、强制独立子话题、关键词交集<50%、增加明日方舟 4 子话题与番茄工作法示例 |

## 四、验证清单

重启后端 + 强刷前端后：

| # | 验证项 | 操作 | 预期结果 |
|---|--------|------|----------|
| 1 | 子查询差异化 | 提问含多子话题的长问题 | 子查询聚焦各自独立子话题，关键词交集明显 < 50% |
| 2 | 不再换后缀 | 同上 | 不再出现"玩法介绍 / 系统特点 / 内容说明"这类仅换尾缀的查询 |
| 3 | 多路召回 | 观察后端日志 | `routes=6+`，各子查询召回的 chunk 来源不同 |
| 4 | RRF 融合质量 | 对比第十轮 | 不同子话题的内容都能体现在回答里 |
