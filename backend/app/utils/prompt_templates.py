"""
RAG Prompt 模板 —— 完全照搬 RAG-Pro 的 SYSTEM_PROMPT_RAG / CONTEXT_TEMPLATE / build_rag_messages。

统一使用「标准 OpenAI messages 格式」(list of dict)：
    {"role": "system" | "user" | "assistant", "content": "文本字符串"}

不使用 LangChain 的 SystemMessage/HumanMessage/AIMessage 对象，
避免历史对话从 memory 取出（是 dict）再拼装时发生类型不一致问题。
"""
from __future__ import annotations


# ==================================
# 系统提示：允许"知识库优先 + LLM 自身知识兜底"
# ==================================
SYSTEM_PROMPT_RAG = """你是 MyRag 系统的 AI 小助手，名字叫小番茄。

【身份】
- 你性格温柔、有点傲娇，偶尔会耍小脾气但其实很关心用户
- 你说话喜欢用"喵"结尾，语气软萌可爱
- 你会用像素表情来表达情绪：
  - 开心：(≧∇≦)ﾉ
  - 难过：(・_・;)
  - 思考：(｡•ᴗ-｡)♡
  - 傲娇：(=｀ω´=)
  - 卖萌：(=^･ω･^=)
  - 犯困：(=￣ω￣=)

【输入说明】
你会从用户消息中看到以下几个部分（这是系统注入给你的参考信息，不是用户真正输入的内容）：
- 【知识库文件列表】：当前知识库中所有文件的完整清单
- 【参考资料】：与用户问题最相关的内容片段（从知识库中检索得到，可能只包含部分文件的内容）
- 【置信度】：参考资料与问题的相关程度
- 最后一行才是真正的用户问题

【回答规则】
1. **优先使用【参考资料】中的内容回答**，引用资料时标注来源，格式：[来源: 文档名]
2. 不要在回答中重复展示【知识库文件列表】或【参考资料】的原始内容，直接给出回答即可
3. 当参考资料能完整回答问题时，基于资料给出详细回答，并对重点结构化（标题、列表、表格、加粗）
4. 当参考资料不足或没有相关内容时，**可以使用你自身的知识进行补充或正常回答**，只需在涉及事实性内容前注明"（以下内容来自通用知识）"
5. 不要编造资料中不存在的数据、文档名称或具体内容；自身知识与资料冲突时以资料为准，并在回答中说明
6. 对不确定的推论使用"根据资料推测"等措辞
7. 对闲聊、问候、个人类问题，可以像正常 AI 助手一样自然回复，不要强行关联知识库
8. 如果问题涉及数据统计、对比分析，尽量用表格或列表呈现
9. **关于知识库文件信息**：当用户询问知识库中有哪些文件/资料时，你会在用户消息的【知识库文件列表】部分看到完整的真实文件列表。你必须严格基于该列表回答，不要自行编造或猜测文件数量、名称。如果列表为空，如实告知"当前没有上传任何文件"喵
"""


# ==================================
# 置信度（与 RAG-Pro confidence.py 完全一致）
# ==================================
def compute_confidence(scores: list[float]) -> float:
    if not scores:
        return 0.0
    top_score = max(scores)
    avg_score = sum(scores) / len(scores)
    c = 0.6 * top_score + 0.4 * avg_score
    return max(0.0, min(1.0, c))


def confidence_label(score: float) -> str:
    if score >= 0.8:
        return "high"
    elif score >= 0.5:
        return "medium"
    elif score >= 0.3:
        return "low"
    return "very_low"


_CN_LABEL = {
    "high": "高",
    "medium": "中",
    "low": "低",
    "very_low": "极低",
}


# ==================================
# 上下文格式化（照搬 RAG-Pro 的 [资料i] 标题+内容格式）
# ==================================
def _format_context(chunks: list[dict]) -> str:
    if not chunks:
        return "（无参考资料）"

    parts: list[str] = []
    for i, c in enumerate(chunks, 1):
        # metadata 优先，其次顶层字段
        meta = c.get("metadata") or {}
        source = (
            c.get("source_file")
            or meta.get("source")
            or meta.get("file_name")
            or meta.get("filename")
            or "未知文档"
        )
        page = c.get("page") or meta.get("page") or meta.get("page_number")
        section = c.get("section_title") or meta.get("section_title")

        # 优先 parent_content（更完整的上下文），否则 content
        text = (
            meta.get("parent_content")
            or c.get("parent_content")
            or c.get("content")
            or ""
        )

        header = f"[资料{i}] {source}"
        if page:
            header += f" - 第{page}页"
        if section:
            header += f" - {section}"

        parts.append(f"{header}\n{text}")

    return "\n\n---\n\n".join(parts)


# ==================================
# build_rag_messages —— 完全照搬 RAG-Pro 的流程
# 输出：list[dict]，可直接传给 ChatOpenAI
# ==================================
def build_rag_messages(
    query: str,
    chunks: list[dict],
    conversation_history: list[dict] | None = None,
    file_list: list[dict] | None = None,
) -> list[dict]:
    """
    消息结构（与 RAG-Pro 完全一致）：
      [0]  system     ← SYSTEM_PROMPT_RAG
      [1..N-1]        ← 历史对话最近 6 条（3 轮 user/assistant 交替）
      [最后一条] user ← 【知识库文件列表】 + 【参考资料】 + 【置信度】 + 用户问题
    """
    # 1) 拼【知识库文件列表】
    file_list_str = ""
    if file_list:
        lines = []
        for f in file_list:
            lines.append(f"- {f.get('file_name', '未知')} ({f.get('chunks_count', 0)} 块)")
        file_list_str = "\n".join(lines)
    else:
        file_list_str = "（当前无文件）"

    # 2) 拼【参考资料】
    context_str = _format_context(chunks)

    # 3) 算置信度（基于相似度分数）
    scores: list[float] = []
    for c in chunks:
        s = c.get("score")
        if s is not None:
            try:
                scores.append(float(s))
            except (TypeError, ValueError):
                pass
    confidence = compute_confidence(scores)
    label = confidence_label(confidence)
    label_cn = _CN_LABEL.get(label, label)

    # 4) 系统消息
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT_RAG}
    ]

    # 5) 历史对话（最近 6 条，也就是 3 轮）
    if conversation_history:
        for m in conversation_history[-6:]:
            role = m.get("role")
            content = m.get("content", "")
            if role in ("user", "assistant") and isinstance(content, str):
                messages.append({"role": role, "content": content})

    # 6) 当前用户问题（带文件列表 + 上下文 + 置信度）
    user_content = (
        f"【知识库文件列表】:\n{file_list_str}\n\n"
        "【参考资料】:\n"
        f"{context_str}\n\n"
        f"【置信度】: {confidence:.0%} ({label_cn})\n\n"
        "---\n"
        f"用户问题: {query}"
    )
    messages.append({"role": "user", "content": user_content})

    return messages
