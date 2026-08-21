"""ChartAgent — 让 LLM 生成 ECharts JSON 配置，前端直接渲染图表。"""

import json
import logging

from .base_agent import BaseAgent

logger = logging.getLogger(__name__)

CHART_PROMPT = """基于以下参考资料，为用户的问题生成一个 ECharts 图表配置。

要求：
1. 返回合法的 JSON 格式的 ECharts option 对象
2. 选择最合适的图表类型（bar/line/pie/scatter/radar）
3. 包含 title, tooltip, legend, xAxis/yAxis（如需要）, series
4. 数据从参考资料中提取，不要编造
5. 使用中文标签
6. 如果资料中没有足够的数据画图，返回空 series

参考资料：
{context}

用户问题：{query}

请仅返回 ECharts option 的 JSON 对象，用 ```json ``` 包裹。"""


class ChartAgent(BaseAgent):
    agent_type = "chart"

    def execute(self, query: str, context: list[dict]) -> dict:
        from ..services.generator_service import chat

        context_text = self._build_context_text(context)
        messages = [
            {"role": "user", "content": CHART_PROMPT.format(context=context_text, query=query)},
        ]

        # 用主模型生成（非流式），chat 返回 (answer, thinking_text)
        result, _ = chat(messages, model=None)
        chart_spec = self._extract_json(result)

        # 兜底：如果 JSON 解析失败，给一个空图
        if not chart_spec or "series" not in chart_spec:
            chart_spec = {
                "title": {"text": "数据不足，无法生成图表"},
                "series": [],
            }

        return {
            "type": "chart",
            "content": {
                "text": "📊 根据知识库数据为您生成了以下图表：",
                "chart_spec": chart_spec,
            },
        }


chart_agent = ChartAgent()
