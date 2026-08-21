"""ChartAgent —— 参考 echarts-agent 的 3 阶段 pipeline：数据理解 → 图表选型 → 图表生成。

对 xlsx/csv：直接从上传目录读原始文件（pandas）拿完整数据，不用检索 chunk 截断片段，
这样画图数据 100% 完整，检索相关性=0% 也能出图。
其它类型文件：走检索 chunks + 正则/pandas 取数据。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from .base_agent import BaseAgent

logger = logging.getLogger(__name__)
# =============== Prompt 模板 ===============

_PICK_CHART_TYPE_SYSTEM = """你是图表选型专家。根据用户意图和数据结构，推荐最合适的一种图表类型。

可选类型（只返回这 10 种之一）：
- bar        柱状图：分类对比 / 排名
- line       折线图：时间趋势 / 走势
- pie        饼图：占比 / 构成（分类数≤8）
- scatter    散点图：相关性 / 二维分布
- radar      雷达图：多维度对比
- funnel     漏斗图：转化流程
- gauge      仪表盘：单指标进度 / 达成率
- histogram  直方图：数值分布
- area       面积图：趋势+构成堆叠
- heatmap    热力图：二维密度 / 交叉分布

只返回 JSON：{"chart_type": "..."}"""

_PICK_CHART_TYPE_USER = """用户意图: {query}
数据列概览: {columns_summary}
行数: {rows_count}"""
# 主生成 prompt（对齐 echarts-agent style: 列定义 + 预览 + 图表类型）
_CHART_GENERATION_SYSTEM = """你是 ECharts 专家。基于提供的数据，生成合法的 ECharts option JSON。

强约束：
1. 只返回一个 JSON，用 ```json 代码块包裹（不要多余说明文字）
2. title.text 必须是中文，简洁描述图表主旨，top/left 合理
3. tooltip: 必须包含 trigger
4. legend: series 多于 1 条时才需要
5. xAxis/yAxis: 二维图必须完整（type, data, name）
6. series: 数据直接从给定 rows 中取值，不准编造
7. pie 的 name/value 必须一一对应
8. 颜色用 ECharts 16 进制色即可，颜色数组不超过 6 个
9. 对于饼图/条形图，按数值排序后再渲染
10. 轴标签：中文太长时 rotate=-30 或 interval=0 避免重叠
11. formatter 只能写字符串模板，不要写 JS 回调函数（如 '{b}: {c}'）"""

_CHART_GENERATION_USER = """图表类型: {chart_type}
用户意图: {query}

【数据列定义】
{columns_def}

【数据预览（最多 50 行）】
{rows_json}

【已有的统计量】
{stats_json}

请输出 ECharts option JSON："""

_EMPTY_CHART = {
    "title": {"text": "知识库中暂无足够数据", "left": "center", "top": "middle"},
    "series": [],
}


class ChartAgent(BaseAgent):
    agent_type = "chart"

    # ---------- 主入口 ----------
    def execute(self, query: str, context: list[dict]) -> dict:
        from ..services.generator_service import chat

        # 1) 尝试从 xlsx/csv 原始文件拿完整数据（优先，最准）
        file_hint = self._find_data_source_file(context)
        raw_df = None
        data_source_info = "检索上下文 chunks"
        if file_hint:
            raw_df = self._try_read_source_file(file_hint)
            if raw_df is not None and len(raw_df) > 0:
                data_source_info = f"原始文件 {os.path.basename(file_hint)}"
                logger.info(f"[chart_agent] 已加载 {data_source_info}: shape={raw_df.shape}")

        # 2) 统一成 columns + rows + preview（max 50）+ stats
        structured = self._to_structured_data(context, raw_df)
        columns = structured["columns"]
        rows = structured["rows"]

        # 数据不足兜底
        if len(rows) == 0 or len(columns) == 0:
            logger.warning("[chart_agent] 结构化后无数据，返回空图")
            return self._build_result(
                text="⚠️ 知识库中没有找到可用的结构化数据来画图。如果是表格数据，请确认已上传 xlsx/csv 文件。",
                chart_spec=_EMPTY_CHART,
            )

        # 3) 阶段 A：LLM 选型
        columns_summary = "; ".join(
            f"{c['name']}({c['type']}/{c.get('role','?')})" for c in columns
        )
        pick_msgs = [
            {"role": "system", "content": _PICK_CHART_TYPE_SYSTEM},
            {
                "role": "user",
                "content": _PICK_CHART_TYPE_USER.format(
                    query=query,
                    columns_summary=columns_summary,
                    rows_count=len(rows),
                ),
            },
        ]
        chart_type = "bar"
        try:
            pick_result, _ = chat(pick_msgs, model=None)
            pick_data = self._extract_json(pick_result)
            if pick_data and pick_data.get("chart_type"):
                chart_type = pick_data["chart_type"]
        except Exception as _e:
            logger.warning(f"[chart_agent] 选型失败，默认 bar: {_e}")

        logger.info(f"[chart_agent] 选型 chart_type={chart_type}")
        # 4) 阶段 B：主生成（option JSON）
        columns_def = "\n".join(
            f"- {c['name']}: type={c['type']}, role={c.get('role', '?')}, example={c.get('example', '')}"
            for c in columns
        )
        rows_preview = rows[:50]
        stats = self._compute_stats(rows, columns)

        gen_msgs = [
            {"role": "system", "content": _CHART_GENERATION_SYSTEM},
            {
                "role": "user",
                "content": _CHART_GENERATION_USER.format(
                    chart_type=chart_type,
                    query=query,
                    columns_def=columns_def,
                    rows_json=json.dumps(rows_preview, ensure_ascii=False, indent=2),
                    stats_json=json.dumps(stats, ensure_ascii=False),
                ),
            },
        ]

        logger.info(
            f"[chart_agent] 开始生成：rows={len(rows)} cols={len(columns)} chart_type={chart_type}"
        )
        result, _ = chat(gen_msgs, model=None)
        chart_spec = self._extract_json(result)
        if not chart_spec or not chart_spec.get("series"):
            logger.warning(
                f"[chart_agent] 主生成无效 chart_spec，兜底空图。raw[:200]={(result or '')[:200]}"
            )
            chart_spec = _EMPTY_CHART

        has_data = any(
            isinstance(s, dict) and (s.get("data") or s.get("type") == "gauge")
            for s in (chart_spec.get("series") or [])
        )

        return self._build_result(
            text=(
                f"📊 基于{data_source_info}（{len(rows)} 行数据）为您生成「{self._chart_cn(chart_type)}」："
                if has_data
                else f"⚠️ 基于{data_source_info}没生成出有数据的图表，请换一种问法试试～"
            ),
            chart_spec=chart_spec,
            has_data=has_data,
            meta={
                "rows_count": len(rows),
                "columns_count": len(columns),
                "chart_type": chart_type,
                "data_source": data_source_info,
            },
        )
    # ---------- 工具：找数据源原始文件 ----------
    def _find_data_source_file(self, context: list[dict]) -> str | None:
        """从检索 chunks 的 source_file 中找 xlsx/csv 文件名，再拼出上传目录真实路径。"""
        from ..config import UPLOAD_DIR
        candidates: list[str] = []
        for c in context:
            src = (
                c.get("source_file")
                or c.get("source")
                or (c.get("metadata") or {}).get("source")
                or ""
            )
            if src.lower().endswith((".xlsx", ".xls", ".csv")):
                if src not in candidates:
                    candidates.append(src)
        if not candidates:
            return None

        # 优先取第一个；直接去上传目录找 file_id_xxx.xlsx 这种文件
        base = os.path.abspath(UPLOAD_DIR)
        if not os.path.isdir(base):
            return None
        for cand in candidates:
            basename = os.path.basename(cand)
            # 直接同名
            full = os.path.join(base, basename)
            if os.path.isfile(full):
                return full
            # 前缀匹配（{file_id}_{name}）
            for fn in os.listdir(base):
                if fn.endswith(basename) and os.path.isfile(os.path.join(base, fn)):
                    return os.path.join(base, fn)
        return None
    def _try_read_source_file(self, path: str) -> Any | None:
        try:
            import pandas as pd
        except Exception:
            logger.warning("[chart_agent] pandas 未安装，无法读原始文件")
            return None
        try:
            ext = os.path.splitext(path)[1].lower()
            if ext == ".csv":
                return pd.read_csv(path)
            if ext in (".xlsx", ".xls"):
                return pd.read_excel(path)
        except Exception as _e:
            logger.warning(f"[chart_agent] 读原始文件失败: {_e}")
        return None

    # ---------- 工具：统一成结构化数据 ----------
    def _to_structured_data(
        self, context: list[dict], raw_df: Any | None
    ) -> dict[str, list]:
        if raw_df is not None:
            try:
                return self._df_to_structured(raw_df)
            except Exception as _e:
                logger.warning(f"[chart_agent] df→structured 失败，回退 chunks: {_e}")
        return self._chunks_to_structured(context)
    def _df_to_structured(self, df) -> dict[str, list]:
        import numpy as np
        import pandas as pd

        df = df.fillna("")
        # 列分类：int/float -> number，datetime -> time，其他 string
        columns: list[dict] = []
        for col in df.columns:
            series = df[col]
            col_type = "string"
            role = "label"
            if pd.api.types.is_numeric_dtype(series):
                col_type = "number"
                role = "value"
            elif pd.api.types.is_datetime64_any_dtype(series):
                col_type = "date"
                role = "time"
            example = ""
            non_null = [x for x in series.head(5).tolist() if x != "" or x == 0]
            if non_null:
                example = str(non_null[0])[:30]
            columns.append(
                {
                    "name": str(col),
                    "type": col_type,
                    "role": role,
                    "example": example,
                }
            )

        # 保证至少一个 value 列 role=value
        has_value = any(c["role"] == "value" for c in columns)
        if not has_value and len(columns) >= 2:
            # 最后一列默认当作 value
            columns[-1]["role"] = "value"
            # 其它如果是数字也补 value
            for c in columns[:-1]:
                if c["type"] == "number":
                    c["role"] = "value"
        rows = df.astype(object).where(df.notna(), None).to_dict(orient="records")
        # 把 numpy 类型转成原生
        def _to_py(v):
            if isinstance(v, (np.integer,)):
                return int(v)
            if isinstance(v, (np.floating,)):
                if np.isnan(v):
                    return None
                return float(v)
            if isinstance(v, (pd.Timestamp,)):
                return v.isoformat()
            return v

        rows = [
            {str(k): _to_py(v) for k, v in r.items()}
            for r in rows
        ]
        return {"columns": columns, "rows": rows}

    def _chunks_to_structured(self, context: list[dict]) -> dict[str, list]:
        """纯正则从 chunks 里挖 CSV/TSV/markdown 表格。兜底用。"""
        rows: list[dict] = []
        columns: list[dict] = []
        text = self._build_context_text(context)

        # 1) markdown 表格
        m = re.search(
            r"\|(.+)\|\s*\|\s*[:\- ]+[:\-| ]*\|\s*\n((?:\|.+\|\s*\n?)+)",
            text,
        )
        if m:
            headers = [h.strip() for h in m.group(1).split("|") if h.strip()]
            body_lines = [
                ln for ln in m.group(2).split("\n") if ln.strip().startswith("|")
            ]
            rows = []
            for ln in body_lines:
                cells = [c.strip() for c in ln.strip("|").split("|")]
                if len(cells) != len(headers):
                    continue
                rows.append(dict(zip(headers, cells)))
            columns = self._infer_columns_from_rows(headers, rows)
            if rows:
                return {"columns": columns, "rows": rows}
        # 2) CSV 块（以 \n 分隔逗号，包含多数字）
        lines = [ln for ln in text.split("\n") if ln.strip()]
        for i, ln in enumerate(lines):
            if "," in ln and len(ln.split(",")) >= 3:
                parts = [p.strip() for p in ln.split(",")]
                # 看下面 2 行是否也类似
                nxt = [p.strip().split(",") for p in lines[i + 1 : i + 4] if "," in p]
                if nxt and all(len(p) == len(parts) for p in nxt):
                    headers = parts
                    rows = [
                        dict(zip(headers, p)) for p in nxt
                    ]
                    columns = self._infer_columns_from_rows(headers, rows)
                    return {"columns": columns, "rows": rows}
        return {"columns": columns, "rows": rows}

    def _infer_columns_from_rows(
        self, headers: list[str], rows: list[dict]
    ) -> list[dict]:
        columns = []
        for h in headers:
            vals = [r.get(h, "") for r in rows if r.get(h, "") != ""]
            col_type = "string"
            role = "label"
            if vals:
                nums = []
                for v in vals[:10]:
                    try:
                        nums.append(float(str(v).replace(",", "")))
                    except Exception:
                        pass
                if len(nums) >= max(1, len(vals) // 2):
                    col_type = "number"
                    role = "value"
            columns.append(
                {
                    "name": h,
                    "type": col_type,
                    "role": role,
                    "example": str(vals[0]) if vals else "",
                }
            )
        # 首列默认 label
        if columns:
            columns[0]["role"] = "label"
        return columns
    # ---------- 工具：基础统计 ----------
    def _compute_stats(self, rows: list[dict], columns: list[dict]) -> dict:
        stats: dict = {"count": len(rows)}
        value_cols = [c["name"] for c in columns if c["role"] == "value"]
        label_cols = [c["name"] for c in columns if c["role"] in ("label", "time", "category")]
        stats["value_cols"] = value_cols
        stats["label_cols"] = label_cols
        for col in value_cols:
            vals = []
            for r in rows:
                try:
                    vals.append(float(r[col]))
                except Exception:
                    pass
            if vals:
                stats[f"{col}_sum"] = round(sum(vals), 4)
                stats[f"{col}_avg"] = round(sum(vals) / len(vals), 4)
                stats[f"{col}_max"] = round(max(vals), 4)
                stats[f"{col}_min"] = round(min(vals), 4)
        if label_cols:
            stats["first_label"] = str(rows[0].get(label_cols[0], "")) if rows else ""
            stats["last_label"] = str(rows[-1].get(label_cols[0], "")) if rows else ""
        return stats

    # ---------- 工具：结果组装 ----------
    def _build_result(
        self,
        text: str,
        chart_spec: dict,
        has_data: bool = True,
        meta: dict | None = None,
    ) -> dict:
        content: dict = {"text": text, "chart_spec": chart_spec}
        if meta:
            content["meta"] = meta
        return {"type": "chart", "content": content}
    def _chart_cn(self, chart_type: str) -> str:
        return {
            "bar": "柱状图", "line": "折线图", "pie": "饼图",
            "scatter": "散点图", "radar": "雷达图", "funnel": "漏斗图",
            "gauge": "仪表盘", "histogram": "直方图", "area": "面积图",
            "heatmap": "热力图",
        }.get(chart_type, chart_type or "图表")


chart_agent = ChartAgent()