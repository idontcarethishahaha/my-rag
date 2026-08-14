# MyRag AI 修改记录 (diff.md)

生成时间：2026-08-14

---

## 一、修改总览

本轮针对 **D:\ai学习项目\my-rag** 做了两个核心改造：

| # | 需求 | 涉及文件 |
|---|------|----------|
| 1 | 思考过程可折叠展示 | `frontend/index.html` |
| 2 | 思考过程真正可见（之前是假的/拿不到），**完全参考 tomatocat-agent 中 LLMProvider 的做法** | `backend/app/services/generator_service.py`、`backend/app/services/rag_service.py`、`backend/app/routers/chat_router.py`、`backend/app/models/schemas.py` |
| 3 | 修复"只能根据知识库回答"的问题，改成 **知识库优先 + LLM 自身知识兜底** | `backend/app/utils/prompt_templates.py` |

---

## 二、各文件详细修改

### 2.1 前端：`frontend/index.html`（整体替换）

#### 2.1.1 整体视觉

```diff
- 左侧边栏：浅灰绿 #f7f8f6 背景
- 品牌区：🌱 brand-icon + "MyRag AI" + 左侧 "‹" 返回图标
- 新对话按钮：白底圆角 + "+" 绿色加号，按钮内文字左对齐
- 会话列表：激活项 "⋄" 菱形图标 + 浅灰底 (#e8e9e8)，hover 时显示删除小叉
- 底栏提示："当前会话暂存于内存"

- 顶栏：极简设计
  - 左侧：‹ 返回图标 + 加粗会话标题
  - 右侧："回到博客" 文字（点击打开知识库抽屉）

- 欢迎页：
  - 🌱 大图标 + "你好呀，我是 MyRag AI 的小助手"
  - 副标题："负责在这里陪伴你，解答你的问题，帮助你更好地探索技术世界。😊 有什么可以帮助你的吗？✨"
  - 底部 radial-gradient 绿色柔光背景

- 输入工具栏：
  - 模型选择器：✦ 钻石符 + 绿点 + "GLM-4-Flash (智谱·快速) ▾"
  - 深度思考按钮：黑底白字 "✦深度思考"（按下后白底黑字 on 态）
  - 右侧："1 个模型可用"

- 输入框：
  - 超大圆角 (22px) + 深阴影
  - placeholder："给 MyRag AI 发送消息"
  - 发送按钮：绿色方形 #16a34a，内含 ↑ 图标，hover 时轻微上移
  - 底部提示："内容由 AI 生成，请注意甄别 · Enter 发送，Shift + Enter 换行"
```

#### 2.1.2 思考过程折叠块（核心新增 UI）

```diff
+ 在每条 AI 回答的上方嵌入思考过程折叠卡片：
+   .thinking-block → 卡片容器（默认收起，.open 展开）
+     .thinking-header → 点击区
+       左：✦ 深度思考 · 3.2 秒（✦ 黄色钻石 + 思考耗时统计）
+       右：展开 ▾ / 收起 ▾（旋转动画）
+     .thinking-body → 可折叠主体，CSS max-height 过渡动画
+       .thinking-content → 灰色小字 pre-wrap 保留换行（灰色 #6b7280）
+
+ 思考中 loading 状态（三点跳动）：
+   "思考中 · · ·"（CSS @keyframes bounce）
+
+ 逻辑：
+   - 深度思考模式下默认展开折叠块
+   - 点击可切换收起/展开
+   - 思考内容存储在 messages[i].thinking_text，仅存于 localStorage（不入库）
```

#### 2.1.3 SSE 事件消费（新增 thinking 相关事件）

```javascript
// 在 fetch ReadableStream 的解析循环中，新增以下事件处理：
if (event === 'thinking')        // → 显示"思考中…"三点 loading
if (event === 'thinking_token')  // → m.thinking_text += token，累加到折叠块
if (event === 'thinking_done')   // → 标记思考阶段结束，准备正式回答
```


---

### 2.2 后端：`backend/app/services/generator_service.py`（完全重写，参考 tomatocat-agent LLMProvider）

#### 2.2.1 为什么之前看不到思考过程？

**错误做法（第一次尝试）**：用 httpx 手写请求，传 `thinking: {"type": "enabled"}`
→ 智谱不识别这个参数，永远不会返回 `reasoning_content`。

**正确做法（tomatocat-agent 同款，本次重写）**：用 `AsyncOpenAI` + `extra_body={"enable_thinking": True}`

```python
# tomatocat-agent 原版写法：
if self.enable_thinking:
    kwargs["extra_body"] = {"enable_thinking": True}
await client.chat.completions.create(**kwargs)

# 思考过程解析（tomatocat 同款字段）：
reasoning = getattr(delta, "reasoning_content", None)   # 思考 token
content   = getattr(delta, "content", None)             # 回答 token
```

#### 2.2.2 新架构

```diff
+ 普通模式 → LangChain ChatOpenAI（单例，不启用思考）
+ 深度思考模式 → AsyncOpenAI 直接调用（绕过 LangChain）：
+    传 extra_body={"enable_thinking": True}
+    国内域名（open.bigmodel.cn）→ httpx.AsyncClient(proxy=None) 绕过代理
+    流式：delta.reasoning_content 作为 ("thinking", token)
+           delta.content           作为 ("content", token)
```

#### 2.2.3 同步→异步桥接

由于 FastAPI 路由是同步的（rag_service 中 ask_rag_stream 是 Generator），新增 `_syncify()`：
- 用 `asyncio.new_event_loop` + `Queue` 把异步生成器包装为同步生成器
- 确保事件循环的正确关闭，避免多线程下的 asyncio 警告

---

### 2.3 后端：`backend/app/services/rag_service.py`（思考阶段事件拆解）

在原有的 `source → token → done` 事件流基础上，增加思考阶段：

```diff
  事件顺序：
    source          引用来源 chunks
+   thinking        进入思考阶段（触发前端"三点 loading"）
+   thinking_token  思考过程文本增量（前端累加到可折叠块）
+   thinking_done   首个 content token 到达前自动补发
    token           正式回答文本增量
    done            全部结束
    error           异常
```

**关键设计**：`thinking_done` 由 rag_service 在收到第一个 `'content'` type 时自动补发，避免前端判断"是否还在思考"出错。

---

### 2.4 后端：`backend/app/routers/chat_router.py` 和 `schemas.py`

```diff
# schemas.py / ChatRequest：
+ enable_deep_think: bool = Field(default=False, description="是否启用深度思考模式")

# chat_router.py：
  chat() 和 chat_stream() 均新增透传 enable_deep_think 参数
  SSE 注释更新为完整事件列表
```

---

### 2.5 后端：`backend/app/utils/prompt_templates.py`（修复"只能根据知识库回答"）

#### 之前的系统提示（严格限制）

```
1. 仅基于【参考资料】中的内容回答问题，不得编造、推测或补充参考资料中没有的信息
3. 如果参考资料不足以完整回答问题，明确说明："根据现有知识库资料，暂无法完整回答此问题"
```

#### 修改后的系统提示（知识库优先 + LLM 兜底）

```
1. 优先使用【参考资料】中的内容，引用资料关键论述标注来源
3. 当参考资料不足或没有相关内容时，可以使用你自身的知识进行补充或正常回答，
   只需在涉及事实性内容前注明"（以下内容来自通用知识）"
6. 对闲聊、问候、个人类问题，可以像正常 AI 助手一样自然回复，不要强行关联知识库
```

**效果**：
- 问知识库有的内容 → 优先用资料，标注来源
- 问知识库没有但 LLM 知道的事实 → 标注"（以下内容来自通用知识）"回答
- 闲聊「你好 / 你叫什么名字」→ 正常回复，不再说"根据现有知识库无法回答"

---

## 三、数据流图

```
前端：✦深度思考按钮开启
  ↓ POST /api/chat/stream
    { question, session_id, enable_deep_think: true }
  ↓
chat_router.chat_stream()
  ↓ 透传 enable_deep_think
rag_service.ask_rag_stream()
  ├── 1. retrieve() → SSE: source {chunks}
  ├── 2. memory.get_messages(last_n=6) + build_rag_messages()
  └── 3. generator_service.chat_stream(enable_deep_think=True)
           ↓（AsyncOpenAI + extra_body={"enable_thinking": True}）
           ├── yield ("thinking", token) → SSE: thinking_token 事件
           └── yield ("content",  token) → SSE: token 事件（首个之前补发 thinking_done）
  └── 4. memory.append(question, answer) → 思考内容不入库，仅存前端缓存
    ↓
前端 ReadableStream 解析
  ├── thinking       → 显示思考中三点 loading
  ├── thinking_token → m.thinking_text += token → 折叠块内容增长
  ├── thinking_done  → 思考阶段结束
  ├── token          → m.content += token → Markdown 正式回答
  └── done           → isStreaming = false，写入 localStorage
```

---

## 四、验证清单

重启后端 & 刷新前端后验证：

| 验证项 | 操作 | 预期结果 |
|--------|------|----------|
| 品牌名 | 看页面 UI | 
| 深度思考过程 | 开启 ✦深度思考 按钮，问一个推理题如"100 以内素数和是多少" | ① 出现"思考中·三点"→ ② 折叠块显示思考内容 → ③ 正式回答 |
| 思考折叠 | 点击"深度思考"卡头 | 可折叠 / 展开，▾ 旋转动画 |
| 闲聊 LLM 兜底 | 问"你好你叫什么" / "1+1 等于几" | 正常回答，不说"根据知识库无法回答" |
| 知识库优先 | 问知识库中的问题 | 带 [来源: 文档名] 标注，引用资料内容 |
| 知识库外事实 | 问知识库没有的通识（如"法国首都"） | 标注"（以下内容来自通用知识）"后正常回答 |
| 引用来源显示 | 回答后消息下方 | 出现"📄 文件名 XX%" source-tag 标签 |
