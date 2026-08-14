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
