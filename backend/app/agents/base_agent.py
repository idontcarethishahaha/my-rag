"""BaseAgent：所有输出格式智能体的抽象基类 + 输出格式检测。"""

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)

# 输出格式类型
OUTPUT_TEXT = "text"
OUTPUT_CHART = "chart"
OUTPUT_REPORT = "report"
OUTPUT_DATA = "data_table"

VALID_OUTPUTS = {OUTPUT_TEXT, OUTPUT_CHART, OUTPUT_REPORT, OUTPUT_DATA}

# —— 关键词规则前置匹配（命中即直接返回，省 LLM 调用 + 避免误判）——
_KEYWORD_RULES: list[tuple[set[str], str]] = [
    (
        {
            "柱状图", "条形图", "折线图", "饼图", "散点图", "雷达图",
            "热力图", "漏斗图", "仪表盘", "直方图", "面积图",
            "对比图", "趋势图", "分布图", "占比图", "可视化",
            "bar chart", "line chart", "pie chart", "chart", "graph", "plot",
        },
        OUTPUT_CHART,
    ),
    (
        {
            "数据表格", "数据表", "表格", "统计表", "对比表", "汇总表",
            "数据对比", "统计一下", "汇总一下", "tabulate",
        },
        OUTPUT_DATA,
    ),
    (
        {
            "结构化报表", "报表", "报告", "汇总报告", "分析报告", "调研报告",
            "总结报告", "分章节", "分章节介绍", "report",
        },
        OUTPUT_REPORT,
    ),
]


def _rule_match_format(query: str) -> str | None:
    import re
    q = (query or "").lower()
    for kw_set, fmt in _KEYWORD_RULES:
        for kw in kw_set:
            if "*" in kw or "." in kw or "+" in kw:
                try:
                    if re.search(kw, q):
                        logger.info(f"[agent_router] 规则命中 kw='{kw}' → {fmt}")
                        return fmt
                except re.error:
                    pass
            else:
                if kw in q:
                    logger.info(f"[agent_router] 规则命中 kw='{kw}' → {fmt}")
                    return fmt
    return None

# 意图检测 prompt（复用 intent_service 的 LLM 调用模式）
_FORMAT_DETECT_SYSTEM = """你是一个输出格式路由器。根据用户的问题，判断最适合的回答格式。

可选格式：
- text: 普通文本回答（默认）
- chart: 当用户明确要求图表（柱状图/折线图/饼图等），或问题是关于数据趋势/对比/占比的
- report: 当用户要求报告/总结/汇总，或问题涉及多维度综合分析
- data_table: 当用户要求数据表格/统计/对比表，或问题关于结构化数据的

只返回一个 JSON 对象：{"output_format": "text|chart|report|data_table"}
不要返回其他内容。"""

_FORMAT_DETECT_USER = """用户问题: {query}"""


class BaseAgent(ABC):
    """所有输出智能体的基类。"""

    agent_type: str = "base"

    @abstractmethod
    def execute(self, query: str, context: list[dict]) -> dict:
        """执行智能体，返回结构化输出。

        Args:
            query: 用户问题
            context: 检索到的上下文 chunks [{content, source, ...}]

        Returns:
            dict: {type, content: {text, ...}}
        """
        ...

    def _extract_json(self, text: str) -> dict:
        """从 LLM 回答中提取 JSON（处理 markdown 代码块包裹）。"""
        text = text.strip()
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            text = text[start:end].strip()
        elif "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            text = text[start:end].strip()
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"[agent] JSON 解析失败: {e}, raw={text[:200]}")
            return {}

    def _build_context_text(self, context: list[dict]) -> str:
        """把检索 chunks 拼成纯文本上下文。兼容 source / source_file 两种字段名。"""
        parts = []
        for c in context:
            source = c.get("source") or c.get("source_file") or "未知"
            content = c.get("content", "")
            parts.append(f"[{source}] {content}")
        return "\n\n".join(parts)


def detect_output_format(query: str, model: str | None = None) -> str:
    """检测用户问题最适合的输出格式。

    优先用关键词规则前置匹配（省 API + 避免误判），规则未命中再走 glm-4-flash LLM。
    """
    # 1) 规则前置匹配：命中直接返回
    rule_fmt = _rule_match_format(query)
    if rule_fmt:
        return rule_fmt

    from langchain_openai import ChatOpenAI
    from ..config import API_KEY as OPENAI_API_KEY, BASE_URL as OPENAI_BASE_URL

    user_msg = _FORMAT_DETECT_USER.format(query=query)
    messages = [
        {"role": "system", "content": _FORMAT_DETECT_SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    try:
        llm = ChatOpenAI(
            model=model or "glm-4-flash",
            api_key=OPENAI_API_KEY or "placeholder",
            base_url=OPENAI_BASE_URL,
            temperature=0.0,
            max_tokens=64,
            streaming=False,
        )
        resp = llm.invoke(messages)
        raw = (resp.content or "").strip()

        # 去掉可能的 markdown 包裹
        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw = "\n".join(lines).strip()

        data = json.loads(raw)
        fmt = data.get("output_format", "").strip().lower()
        if fmt in VALID_OUTPUTS:
            logger.info(f"[agent_router] query='{query[:40]}' → output_format={fmt}")
            return fmt
        logger.warning(f"[agent_router] 未知格式: {fmt}，回退到 text")
        return OUTPUT_TEXT
    except Exception as e:
        logger.warning(f"[agent_router] 格式检测失败，回退到 text: {e}")
        return OUTPUT_TEXT
