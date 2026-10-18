"""经营分析场景工具层：file_read / sql_query / report_render。

与差旅共用同一套引擎原语（Tool Registry 的 JSON Schema 校验、风险分级、
Trace 事件溯源），只换工具集——这是技术方案 §5.2 “双场景复用”的实现。

- ``sql_query`` 用标准库 ``sqlite3`` 真执行 SQL（仅允许 SELECT），
  不引入任何数据库依赖；
- ``report_render`` 从 SQL 结果中解析指标引用（``$step`` 查找）并生成
  Markdown 报告；每个数字都可由校验器独立重算比对（见 analysis_agent）。
"""

from __future__ import annotations

import csv
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .tools import Tool

_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _ROOT / "data"


# ---------------------------------------------------------------------------
# 数据装载
# ---------------------------------------------------------------------------


def load_csv_to_sqlite(path: Optional[Path] = None) -> sqlite3.Connection:
    """把 CSV 装进内存 SQLite；这是分析场景的确定性数据通道。"""
    csv_path = path or _DATA_DIR / "sales_2026Q3.csv"
    if not csv_path.is_file():
        raise FileNotFoundError(f"数据文件不存在：{csv_path}")
    # check_same_thread=False：DAG 同层步骤跨线程并发查询，串行化由 AnalysisState 锁保证。
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        cols = ", ".join(f'"{c}"' for c in header)
        conn.execute(f"CREATE TABLE sales ({cols})")
        ph = ", ".join("?" for _ in header)
        for row in reader:
            if not row:
                continue
            conn.execute(f"INSERT INTO sales VALUES ({ph})", row)
    return conn


# ---------------------------------------------------------------------------
# 工具实现（纯函数，接收 registry 传入的共享状态）
# ---------------------------------------------------------------------------


class AnalysisState:
    """分析运行期共享状态：一个连接 + 已注册文件。

    DAG 同层步骤在不同线程并发查询，sqlite3 连接必须允许跨线程并串行化访问。
    """

    def __init__(self) -> None:
        self.conn: Optional[sqlite3.Connection] = None
        self.loaded_path: str = ""
        # RLock：query() 持锁期间会再调 ensure_loaded()，需要可重入。
        self._lock = threading.RLock()

    def ensure_loaded(self, path: Optional[str] = None) -> sqlite3.Connection:
        with self._lock:
            if self.conn is None or (path and path != self.loaded_path):
                if self.conn is not None:
                    self.conn.close()
                self.conn = load_csv_to_sqlite(Path(path) if path else None)
                self.loaded_path = path or str(_DATA_DIR / "sales_2026Q3.csv")
            return self.conn

    def query(self, sql: str) -> List[Dict[str, Any]]:
        """加锁执行只读查询，返回字典行（跨线程串行化）。"""
        with self._lock:
            conn = self.ensure_loaded()
            cursor = conn.execute(sql)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            return columns, [dict(r) for r in cursor.fetchmany(200)]


def file_read(state: AnalysisState, path: str = "", **_: Any) -> Dict[str, Any]:
    state.ensure_loaded(path or None)
    table = "sales"
    _, schema_rows = state.query(
        f"SELECT * FROM ("
        f"  SELECT 1, 'month' AS name, 'TEXT' AS type,"
        f"         SUM(CASE WHEN \"month\" IS NULL OR \"month\"='' THEN 1 ELSE 0 END) AS null_count,"
        f"         COUNT(DISTINCT \"month\") AS distinct_count FROM {table}"
        f"  UNION ALL SELECT 1,'region','TEXT',"
        f"         SUM(CASE WHEN \"region\" IS NULL OR \"region\"='' THEN 1 ELSE 0 END),"
        f"         COUNT(DISTINCT \"region\") FROM {table}"
        f"  UNION ALL SELECT 1,'category','TEXT',"
        f"         SUM(CASE WHEN \"category\" IS NULL OR \"category\"='' THEN 1 ELSE 0 END),"
        f"         COUNT(DISTINCT \"category\") FROM {table}"
        f"  UNION ALL SELECT 1,'units','INTEGER',"
        f"         SUM(CASE WHEN \"units\" IS NULL OR \"units\"='' THEN 1 ELSE 0 END),"
        f"         COUNT(DISTINCT \"units\") FROM {table}"
        f"  UNION ALL SELECT 1,'revenue','REAL',"
        f"         SUM(CASE WHEN \"revenue\" IS NULL OR \"revenue\"='' THEN 1 ELSE 0 END),"
        f"         COUNT(DISTINCT \"revenue\") FROM {table}"
        f")"
    )
    schema = [
        {"name": r["name"], "type": r["type"], "null_count": r["null_count"], "distinct": r["distinct_count"]}
        for r in schema_rows
    ]
    _, range_rows = state.query('SELECT MIN("month") AS lo, MAX("month") AS hi, COUNT(*) AS n FROM sales')
    row = range_rows[0]
    _, preview_rows = state.query("SELECT * FROM sales LIMIT 5")
    return {
        "path": state.loaded_path,
        "table": table,
        "row_count": row["n"],
        "month_range": [row["lo"], row["hi"]],
        "columns": schema,
        "missing_total": sum(c["null_count"] for c in schema),
        "preview": preview_rows,
    }


def sql_query(state: AnalysisState, sql: str = "", **_: Any) -> Dict[str, Any]:
    cleaned = re.sub(r"--.*$", "", sql or "", flags=re.MULTILINE).strip().rstrip(";")
    if not cleaned.upper().startswith("SELECT"):
        raise ValueError("只允许 SELECT 查询（分析场景数据源只读）")
    if re.search(r"\b(ATTACH|DETACH|PRAGMA)\b", cleaned.upper()):
        raise ValueError("检测到受限关键字")
    try:
        columns, rows = state.query(cleaned)
    except sqlite3.Error as exc:
        raise ValueError(f"SQL 执行失败：{exc}") from exc
    return {"sql": cleaned, "columns": columns, "rows": rows, "row_count": len(rows)}


def _lookup(rows: List[Dict[str, Any]], where: Dict[str, Any], column: str) -> float:
    for row in rows:
        if all(str(row.get(k)) == str(v) for k, v in (where or {}).items()):
            value = row.get(column)
            if value is None:
                raise ValueError(f"引用列 {column} 为空")
            return float(value)
    raise KeyError(f"未找到满足 {where} 的行（列 {column}）")


def report_render(
    state: AnalysisState,
    title: str = "经营分析报告",
    metrics: Optional[List[Dict[str, Any]]] = None,
    findings: Optional[List[str]] = None,
    conclusion: str = "",
    refs: Optional[Dict[str, Dict[str, Any]]] = None,
    inject_error: bool = False,
    **_: Any,
) -> Dict[str, Any]:
    """生成 Markdown 分析报告。

    ``metrics`` 每项：{name, current:{step,where,column}, baseline:{...}|null,
    unit:"元"|"%"}。值在工具内从 ``refs``（$ref 解析后的步骤结果）中查表计算，
    校验器会用同一套查找逻辑独立重算比对——数字与数据源不一致就会被拦截。
    """
    refs = refs or {}
    lines: List[str] = [f"# {title}", ""]
    resolved: List[Dict[str, Any]] = []

    def _resolve(ref: Dict[str, Any]) -> float:
        step = refs.get(ref.get("step") or "")
        if not isinstance(step, dict):
            raise KeyError(f"引用的步骤结果不存在：{ref.get('step')}")
        return _lookup(step.get("rows") or [], ref.get("where") or {}, ref.get("column") or "")

    for m in metrics or []:
        current = _resolve(m["current"])
        baseline = _resolve(m["baseline"]) if m.get("baseline") else None
        unit = m.get("unit", "元")
        if baseline is None:
            value = current
            text = f"¥{value:,.0f}"
        elif unit == "%":
            value = (current - baseline) / baseline * 100 if baseline else 0.0
            text = f"{value:+.1f}%"
        else:
            value = current - baseline
            text = f"¥{value:+,.0f}"
        if inject_error and unit == "%" and "数码" in str(m.get("name", "")):
            value = value * 0.5  # 注入：把最关键指标算错一半，校验器必须拦截
            text = f"{value:+.1f}%"
        resolved.append(
            {
                "name": m.get("name", ""),
                "value": round(value, 4),
                "text": text,
                "unit": unit,
                "current": current,
                "baseline": baseline,
            }
        )
        lines.append(f"- **{m.get('name', '')}**：{text}")

    lines += ["", "## 发现", ""]
    for item in findings or []:
        lines.append(f"- {item}")
    lines += ["", "## 结论", "", conclusion or "（无）", ""]
    markdown = "\n".join(lines)
    return {
        "title": title,
        "markdown": markdown,
        "metrics": resolved,
        "facts": {"metric_count": len(resolved), "title": title},
    }


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


def build_analysis_registry(state: Optional[AnalysisState] = None):
    from .tools import ToolRegistry

    state = state or AnalysisState()
    registry = ToolRegistry()
    for tool in (
        Tool(
            name="file_read",
            description="读取 CSV 数据文件，返回字段、行数、缺失值统计与预览",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "CSV 路径，默认内置销售数据"}},
            },
            fn=lambda **kw: file_read(state, **kw),
            risk=0,
            category="analysis",
            returns="path/table/row_count/month_range/columns/missing_total/preview",
        ),
        Tool(
            name="sql_query",
            description="对已加载数据执行只读 SELECT 聚合查询（sqlite3）",
            parameters={
                "type": "object",
                "properties": {"sql": {"type": "string", "description": "SELECT 语句"}},
                "required": ["sql"],
            },
            fn=lambda **kw: sql_query(state, **kw),
            risk=0,
            category="analysis",
            returns="sql/columns/rows/row_count",
        ),
        Tool(
            name="report_render",
            description="从 SQL 结果解析指标引用并生成 Markdown 分析报告",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "metrics": {"type": "array"},
                    "findings": {"type": "array", "items": {"type": "string"}},
                    "conclusion": {"type": "string"},
                    "refs": {"type": "object"},
                    "inject_error": {
                        "type": "boolean",
                        "description": "演示/消融开关：渲染时故意算错关键指标，用于验证确定性校验器",
                    },
                },
                "required": ["title"],
            },
            fn=lambda **kw: report_render(state, **kw),
            risk=0,
            category="analysis",
            returns="title/markdown/metrics/facts",
        ),
    ):
        registry.register(tool)
    return registry
