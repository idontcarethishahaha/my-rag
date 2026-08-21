"""DataAgent — 让 LLM 生成结构化数据表格 + 可选图表 + 数据洞察。"""

import logging

from .base_agent import BaseAgent

logger = logging.getLogger(__name__)

DATA_ANALYSIS_PROMPT = """基于以下参考资料，对用户的问题进行数据分析。

要求返回一个 JSON 对象，包含：
1. "summary": 分析摘要文字
2. "data_table": {{ "headers": [...], "rows": [[...], ...] }} — 结构化表格数据
3. "chart_spec": ECharts option 对象（可选，如果数据适合可视化）
4. "insights": 数据洞察列表 ["insight1", "insight2", ...]

所有数据从参考资料中提取，不要编造。
如果资料中没有足够的数据，data_table 的 rows 可以为空数组。

参考资料：
{context}

用户问题：{query}

请仅返回 JSON 对象，用 ```json ``` 包裹。"""


class DataAgent(BaseAgent):
    agent_type = "data_table"

    def execute(self, query: str, context: list[dict]) -> dict:
        from ..services.generator_service import chat

        context_text = self._build_context_text(context)
        messages = [
            {"role": "user", "content": DATA_ANALYSIS_PROMPT.format(context=context_text, query=query)},
        ]

        result, _ = chat(messages, model=None)
        analysis = self._extract_json(result)

        # 兜底
        if not analysis:
            analysis = {
                "summary": "数据不足，无法进行有效分析",
                "data_table": {"headers": [], "rows": []},
                "insights": [],
            }

        return {
            "type": "data_table",
            "content": {
                "text": analysis.get("summary", "数据分析完成"),
                "data_table": analysis.get("data_table", {"headers": [], "rows": []}),
                "chart_spec": analysis.get("chart_spec"),
                "insights": analysis.get("insights", []),
            },
        }


data_agent = DataAgent()
