"""DataAgent —— 参考 echarts-agent：数据理解 → 摘要 / 表格 / 洞察 / 图表。

与 ChartAgent 对齐：xlsx/csv 时直接读取上传目录下的原始文件，避免只靠检索 chunks 截断。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from .base_agent import BaseAgent

logger = logging.getLogger(__name__)

_DATA_ANALYSIS_SYSTEM = """你是数据分析专家。基于给定的结构化数据（列定义 + 行数据），返回一个合法 JSON。

强约束（用 ```json ``` 包裹）：
{
  "summary": "<中文分析摘要，3-6 句话，给出具体数字>",
  "data_table": {"headers": ["col1","col2",...], "rows": [[...], [...]] — 最多 50 行},
  "chart_spec": <ECharts option JSON；如果数据适合图表就填，否则不填或填 null>,
  "insights": ["<带具体数字的洞察 1>", "<洞察 2>", "……"],
  "chart_type": "<bar/line/pie/... 之一，若 chart_spec 存在必填>"
}

禁止编造数据：所有数字必须严格来源于 rows。"""

_DATA_ANALYSIS_USER = """用户意图: {query}

【数据列定义】
{columns_def}

【数据行数】{rows_count}
【原始文件】{data_source}

【最多 50 行预览】
{rows_json}

【列统计】
{stats_json}

请输出 JSON："""


class DataAgent(BaseAgent):
    agent_type = "data_table"

    def execute(self, query: str, context: list[dict]) -> dict:
        from ..services.generator_service import chat

        source_file = self._find_source_file(context)
        raw_df = None
        data_source = "检索上下文"
        if source_file:
            raw_df = self._try_read_source(source_file)
            if raw_df is not None and len(raw_df) > 0:
                data_source = os.path.basename(source_file)

        structured = self._to_structured(context, raw_df)
        columns = structured["columns"]
        rows = structured["rows"]

        if not rows or not columns:
            return self._empty_result(
                "⚠️ 没有找到可分析的结构化数据。如果是表格数据，请确认已上传 xlsx/csv。"
            )

        columns_def = "\n".join(
            f"- {c['name']}: type={c['type']}, role={c.get('role', '?')}, example={c.get('example', '')}"
            for c in columns
        )
        stats = self._compute_stats(rows, columns)
        rows_preview = rows[:50]

        messages = [
            {"role": "system", "content": _DATA_ANALYSIS_SYSTEM},
            {
                "role": "user",
                "content": _DATA_ANALYSIS_USER.format(
                    query=query,
                    columns_def=columns_def,
                    rows_count=len(rows),
                    data_source=data_source,
                    rows_json=json.dumps(rows_preview, ensure_ascii=False, indent=2),
                    stats_json=json.dumps(stats, ensure_ascii=False, indent=2),
                ),
            },
        ]

        result, _ = chat(messages, model=None)
        analysis = self._extract_json(result)

        if not analysis:
            logger.warning(
                f"[data_agent] LLM 没返回 JSON。raw[:300]={(result or '')[:300]}"
            )
            return self._fallback(query, columns, rows, data_source)

        summary = analysis.get("summary") or (f"基于 {data_source} 的数据分析结果。")
        dt = analysis.get("data_table") or {"headers": [], "rows": []}
        insights = analysis.get("insights") or []
        chart_spec = analysis.get("chart_spec")

        if not dt.get("headers") or not dt.get("rows"):
            dt = self._to_headers_rows(columns, rows)

        return {
            "type": "data_table",
            "content": {
                "text": summary,
                "data_table": dt,
                "chart_spec": chart_spec,
                "insights": insights,
                "meta": {
                    "rows_count": len(rows),
                    "columns_count": len(columns),
                    "data_source": data_source,
                    "chart_type": analysis.get("chart_type"),
                },
            },
        }

    # ========== 数据源 ==========
    def _find_source_file(self, context: list[dict]) -> str | None:
        from ..config import UPLOAD_DIR

        names = []
        for c in context:
            src = (
                c.get("source_file")
                or c.get("source")
                or (c.get("metadata") or {}).get("source")
                or ""
            )
            if src.lower().endswith((".xlsx", ".xls", ".csv")) and src not in names:
                names.append(os.path.basename(src))
        base = os.path.abspath(UPLOAD_DIR)
        if not os.path.isdir(base) or not names:
            return None
        for name in names:
            full = os.path.join(base, name)
            if os.path.isfile(full):
                return full
            for fn in os.listdir(base):
                if fn.endswith(name) and os.path.isfile(os.path.join(base, fn)):
                    return os.path.join(base, fn)
        return None

    def _try_read_source(self, path: str) -> Any | None:
        try:
            import pandas as pd
        except Exception:
            return None
        try:
            ext = os.path.splitext(path)[1].lower()
            if ext == ".csv":
                return pd.read_csv(path)
            if ext in (".xlsx", ".xls"):
                return pd.read_excel(path)
        except Exception as _e:
            logger.warning(f"[data_agent] 读源文件失败: {_e}")
        return None

    # ========== 结构化 ==========
    def _to_structured(self, context: list[dict], raw_df: Any | None) -> dict:
        if raw_df is not None:
            try:
                return self._df_to_structured(raw_df)
            except Exception as _e:
                logger.warning(f"[data_agent] df→structured 失败: {_e}")
        return self._chunks_to_structured(context)

    def _df_to_structured(self, df) -> dict:
        import numpy as np
        import pandas as pd

        df = df.fillna("")
        columns = []
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
            non_null = [x for x in series.head(5).tolist() if x != "" or x == 0]
            example = str(non_null[0])[:30] if non_null else ""
            columns.append(
                {
                    "name": str(col),
                    "type": col_type,
                    "role": role,
                    "example": example,
                }
            )
        has_value = any(c["role"] == "value" for c in columns)
        if not has_value and len(columns) >= 2:
            columns[-1]["role"] = "value"
            for c in columns[:-1]:
                if c["type"] == "number":
                    c["role"] = "value"

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

        rows_raw = df.astype(object).where(df.notna(), None).to_dict(orient="records")
        rows = [{str(k): _to_py(v) for k, v in r.items()} for r in rows_raw]
        return {"columns": columns, "rows": rows}

    def _chunks_to_structured(self, context: list[dict]) -> dict:
        text = self._build_context_text(context)
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
            columns = self._infer_columns(headers, rows)
            if rows:
                return {"columns": columns, "rows": rows}
        return {"columns": [], "rows": []}

    def _infer_columns(self, headers: list[str], rows: list[dict]) -> list[dict]:
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
        if columns:
            columns[0]["role"] = "label"
        return columns

    # ========== 统计 ==========
    def _compute_stats(self, rows: list[dict], columns: list[dict]) -> dict:
        stats = {"count": len(rows)}
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

    # ========== 兜底 ==========
    def _fallback(
        self, query: str, columns: list[dict], rows: list[dict], data_source: str
    ) -> dict:
        dt = self._to_headers_rows(columns, rows)
        insights = []
        value_cols = [c["name"] for c in columns if c["role"] == "value"]
        stats = self._compute_stats(rows, columns)
        for col in value_cols:
            s_avg = stats.get(f"{col}_avg")
            s_max = stats.get(f"{col}_max")
            s_min = stats.get(f"{col}_min")
            if s_avg is not None:
                insights.append(
                    f"{col} 共 {stats['count']} 项，均值 {s_avg}，区间 {s_min}~{s_max}。"
                )
        return {
            "type": "data_table",
            "content": {
                "text": f"基于 {data_source} 的结构化分析（共 {stats['count']} 行）：",
                "data_table": dt,
                "chart_spec": None,
                "insights": insights or ["暂无明显洞察。"],
                "meta": {
                    "rows_count": len(rows),
                    "columns_count": len(columns),
                    "data_source": data_source,
                },
            },
        }

    def _empty_result(self, text: str) -> dict:
        return {
            "type": "data_table",
            "content": {
                "text": text,
                "data_table": {"headers": [], "rows": []},
                "chart_spec": None,
                "insights": [],
            },
        }

    def _to_headers_rows(self, columns: list[dict], rows: list[dict]) -> dict:
        headers = [c["name"] for c in columns]
        body = [[r.get(h, "") for h in headers] for r in rows[:200]]
        return {"headers": headers, "rows": body}


data_agent = DataAgent()
