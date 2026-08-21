"""BaseAgent：所有输出格式智能体的抽象基类 + 输出格式检测。"""
from __future__ import annotations

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
            # 中文图表类型（含口语化变体）
            "柱状图", "条形图", "折线图", "饼图", "饼状图", "散点图", "雷达图",
            "热力图", "漏斗图", "仪表盘", "直方图", "面积图",
            "对比图", "趋势图", "分布图", "占比图", "可视化",
            "曲线图", "堆叠图", "环形图", "玫瑰图", "K线图",
            "画个图", "做个图", "生成图", "出个图", "绘图", "画图", "图表",
            # 英文
            "bar chart", "line chart", "pie chart", "chart", "graph", "plot", "visual",
            "diagram", "histogram", "scatter", "radar", "funnel",
        },
        OUTPUT_CHART,
    ),
    (
        {
            "数据表格", "数据表", "表格", "统计表", "对比表", "汇总表",
            "数据对比", "统计一下", "汇总一下", "列个表", "做个表",
            "tabulate", "table", "spreadsheet",
        },
        OUTPUT_DATA,
    ),
    (
        {
            "结构化报表", "报表", "报告", "汇总报告", "分析报告", "调研报告",
            "总结报告", "分章节", "分章节介绍", "report", "summary", "analysis",
        },
        OUTPUT_REPORT,
    ),
]


def _rule_match_format(query: str) -> str | None:
    """关键词匹配：先做精确子串匹配，再做模糊匹配（包含 2 字重叠即命中）。"""
    import re
    q = (query or "").lower()
    # 第一轮：精确子串匹配
    for kw_set, fmt in _KEYWORD_RULES:
        for kw in kw_set:
            if "*" in kw or "." in kw or "+" in kw:
                try:
                    if re.search(kw, q):
                        logger.info(f"[agent_router] 规则命中(正则) kw='{kw}' → {fmt}")
                        return fmt
                except re.error:
                    pass
            else:
                if kw in q:
                    logger.info(f"[agent_router] 规则命中(子串) kw='{kw}' → {fmt}")
                    return fmt
    # 第二轮：模糊匹配——取关键词的 2 字前缀做包含匹配（处理口语化变体如"饼状图"→"饼"）
    _FUZZY_PREFIXES = {
        OUTPUT_CHART: {"饼", "柱", "条", "折", "散", "雷", "热", "漏", "仪", "直", "面", "曲", "堆", "环", "玫", "图", "chart", "graph", "plot"},
        OUTPUT_DATA: {"表", "tab", "tab"},
        OUTPUT_REPORT: {"报", "report", "summ", "analy"},
    }
    for fmt, prefixes in _FUZZY_PREFIXES.items():
        for pfx in prefixes:
            if pfx in q:
                logger.info(f"[agent_router] 规则命中(模糊) pfx='{pfx}' → {fmt}")
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

    优先用关键词规则前置匹配（省 API + 避免误判），规则未命中再走 LLM。
    LLM 调用走 provider_service（与主链路一致），支持多 Provider。
    """
    # 1) 规则前置匹配：命中直接返回
    rule_fmt = _rule_match_format(query)
    if rule_fmt:
        return rule_fmt

    # 2) LLM 检测：复用 generator_service 的配置（支持多 Provider）
    try:
        from ..services.generator_service import chat
        user_msg = _FORMAT_DETECT_USER.format(query=query)
        messages = [
            {"role": "system", "content": _FORMAT_DETECT_SYSTEM},
            {"role": "user", "content": user_msg},
        ]
        # 用指定模型或默认 Provider，温度 0 保证确定性
        result, _ = chat(messages, model=model)
        raw = (result or "").strip()

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
