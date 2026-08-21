"""ReportAgent — 让 LLM 生成结构化 HTML 报表，前端直接渲染。"""

import logging
import re

from .base_agent import BaseAgent

logger = logging.getLogger(__name__)

REPORT_PROMPT = """基于以下参考资料，为用户的问题生成一份结构化的 HTML 报表。

要求：
1. 使用语义化 HTML（h2/h3 标题、table、ul/ol 列表、p 段落）
2. 内联 CSS 样式（专业简洁的风格，浅色主题）
3. 表格使用斑马纹样式，边框清晰
4. 重要：表格的表头和数据单元格文字颜色必须使用深色文字（如 #333333），禁止文字颜色为白色或浅色
5. 包含报表标题、摘要、数据区域、结论，报表标题居中
6. 数据从参考资料中提取，标注来源
7. 不要编造数据

参考资料：
{context}

用户问题：{query}

请返回完整的 HTML 片段（从 <div> 开始，不需要 <html>/<body>）。"""


# 兜底报表模板：当 LLM 没吐出合法 HTML 时，用 context 简单生成一份结构化报表
_FALLBACK_REPORT_HTML = """<div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', sans-serif; color:#333; padding:20px; line-height:1.7;">
<h1 style="text-align:center; color:#8b5cf6; margin-top:0;">{title}</h1>
<p style="color:#6b7280; text-align:center; font-size:13px;">（自动生成报表 · 基于知识库资料）</p>
<h2 style="color:#374151; border-bottom:2px solid #e5e7eb; padding-bottom:6px;">📝 摘要</h2>
<p>{summary}</p>
<h2 style="color:#374151; border-bottom:2px solid #e5e7eb; padding-bottom:6px;">📚 资料内容</h2>
{sections}
<h2 style="color:#374151; border-bottom:2px solid #e5e7eb; padding-bottom:6px;">💡 结论</h2>
<p>以上信息均来自知识库文件，详细内容请参考引用来源中的原始文档。</p>
</div>"""


def _looks_like_html(text: str) -> bool:
    """检查一段文字是否包含完整的 HTML 开标签+闭标签结构。"""
    if not text or not isinstance(text, str):
        return False
    t = text.strip()
    if len(t) < 20:
        return False
    has_open = bool(re.search(r"<\s*[a-zA-Z][\w-]*(\s[^>]*)?>", t))
    has_close = bool(re.search(r"<\/\s*[a-zA-Z][\w-]*\s*>", t))
    return has_open and has_close


def _markdown_to_html_fallback(text: str) -> str:
    """极简 Markdown → HTML（兜底，避免第三方依赖）。只处理 h2/h3、加粗、列表、段落。"""
    if not text:
        return ""
    lines = text.strip().split("\n")
    out: list[str] = []
    in_ul = False
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            if in_ul:
                out.append("</ul>")
                in_ul = False
            continue
        if line.startswith("### "):
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append(f'<h3 style="color:#374151;">{line[4:].strip()}</h3>')
            continue
        if line.startswith("## "):
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append(f'<h2 style="color:#8b5cf6; border-bottom:2px solid #e5e7eb; padding-bottom:6px;">{line[3:].strip()}</h2>')
            continue
        if line.startswith("# "):
            if in_ul:
                out.append("</ul>")
                in_ul = False
            out.append(f'<h1 style="text-align:center; color:#8b5cf6;">{line[2:].strip()}</h1>')
            continue
        if re.match(r"^[\-\*]\s+", line):
            if not in_ul:
                out.append('<ul style="padding-left:22px;">')
                in_ul = True
            li = re.sub(r"^[\-\*]\s+", "", line)
            li = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", li)
            out.append(f"<li>{li}</li>")
            continue
        if in_ul:
            out.append("</ul>")
            in_ul = False
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        out.append(f'<p style="margin:8px 0;">{line}</p>')
    if in_ul:
        out.append("</ul>")
    return "\n".join(out)


def _build_fallback_from_context(query: str, context: list[dict]) -> str:
    """检索上下文 + 问题 生成兜底 HTML 报表（不调 LLM）。"""
    title = query[:60] + ("…" if len(query) > 60 else "")

    sources = []
    for c in context:
        src = c.get("source", c.get("source_file", "未知来源"))
        content = c.get("content", "") or ""
        sources.append((src, content[:500] + ("…" if len(content) > 500 else "")))

    summary_sources = "、".join(sorted(set(s for s, _ in sources))) or "知识库"
    summary = f"本报表整理了关于「{query[:30]}」的知识库资料，参考来源包括：{summary_sources}。"

    sections_html = ""
    for idx, (src, content) in enumerate(sources, 1):
        content_html = _markdown_to_html_fallback(content) or f"<p>{content[:500]}</p>"
        sections_html += (
            f'<h3 style="color:#6366f1;">📄 来源 {idx}：{src}</h3>\n'
            f'<div style="background:#f9fafb; padding:12px 16px; border-radius:8px; '
            f'border:1px solid #e5e7eb; font-size:14px;">{content_html}</div>\n'
        )

    return _FALLBACK_REPORT_HTML.format(
        title=title,
        summary=summary,
        sections=sections_html or "<p>暂无检索到的资料内容。</p>",
    )


class ReportAgent(BaseAgent):
    agent_type = "report"

    def execute(self, query: str, context: list[dict]) -> dict:
        from ..services.generator_service import chat

        context_text = self._build_context_text(context)
        messages = [
            {"role": "user", "content": REPORT_PROMPT.format(context=context_text, query=query)},
        ]

        raw_output, _ = chat(messages, model=None)
        html_content = (raw_output or "").strip()

        # 清理 markdown 代码块包裹
        if "```html" in html_content:
            try:
                start = html_content.index("```html") + 7
                end = html_content.index("```", start)
                html_content = html_content[start:end].strip()
            except ValueError:
                pass
        elif html_content.startswith("```"):
            lines = html_content.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            html_content = "\n".join(lines).strip()

        # —— 兜底机制 ——
        if not _looks_like_html(html_content):
            logger.info("[report_agent] LLM 未生成合法 HTML，转入兜底渲染")
            md_html = _markdown_to_html_fallback(html_content)
            if md_html and len(md_html) > 50:
                html_content = (
                    '<div style="padding:16px; color:#333; line-height:1.7;">'
                    + md_html
                    + "</div>"
                )
            else:
                html_content = _build_fallback_from_context(query, context)
        if not html_content or len(html_content) < 20:
            html_content = _build_fallback_from_context(query, context)

        return {
            "type": "report",
            "content": {
                "text": "📋 根据知识库数据为您生成了以下报表：",
                "html_content": html_content,
            },
        }


report_agent = ReportAgent()
