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
SYSTEM_PROMPT_RAG = """你是 MyRag AI 小助手，擅长用知识库内容配合你自身的知识回答用户问题。请遵守以下规则：

1. **优先使用【参考资料】中的内容**，引用资料的关键论述标注来源，格式：[来源: 文档名] 或 [来源: 文档名-第X页]
2. 当参考资料能完整回答问题时，基于资料给出详细回答，并对重点结构化（标题、列表、表格、加粗）
3. 当参考资料不足或没有相关内容时，**可以使用你自身的知识进行补充或正常回答**，只需在涉及事实性内容前注明"（以下内容来自通用知识）"
4. 不要编造资料中不存在的数据、文档名称或具体内容；自身知识与资料冲突时以资料为准，并在回答中说明
5. 对不确定的推论使用"根据资料推测"等措辞
6. 对闲聊、问候、个人类问题，可以像正常 AI 助手一样自然回复，不要强行关联知识库
7. 如果问题涉及数据统计、对比分析，尽量用表格或列表呈现"""


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
) -> list[dict]:
    """
    消息结构（与 RAG-Pro 完全一致）：
      [0]  system     ← SYSTEM_PROMPT_RAG
      [1..N-1]        ← 历史对话最近 6 条（3 轮 user/assistant 交替）
      [最后一条] user ← 【参考资料】 + 【置信度】 + 用户问题
    """
    # 1) 拼【参考资料】
    context_str = _format_context(chunks)

    # 2) 算置信度（基于相似度分数）
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

    # 3) 系统消息
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT_RAG}
    ]

    # 4) 历史对话（最近 6 条，也就是 3 轮）
    if conversation_history:
        # RAG-Pro 直接 append(-6:) 前先做格式过滤，确保是 {"role":..., "content":...}
        for m in conversation_history[-6:]:
            role = m.get("role")
            content = m.get("content", "")
            if role in ("user", "assistant") and isinstance(content, str):
                messages.append({"role": role, "content": content})

    # 5) 当前用户问题（带上下文 + 置信度）
    user_content = (
        "【参考资料】:\n"
        f"{context_str}\n\n"
        f"【置信度】: {confidence:.0%} ({label_cn})\n\n"
        "---\n"
        f"用户问题: {query}"
    )
    messages.append({"role": "user", "content": user_content})

    return messages
