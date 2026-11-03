"""智能信贷风控场景工具层（180 赛题 · 金融智能体方案赛）。

数据来自 UCI 公开数据集「Default of Credit Card Clients」（台湾地区某银行
2005 年 4–9 月信用卡授信客户，30,000 户、25 个字段），装载进内存 SQLite 后由
``sql_query`` 真执行只读 SELECT。与差旅、经营分析、智慧零售共用同一套引擎原语
（Tool Registry 的 Schema 校验/风险分级、EventBus/Trace 事件溯源、确定性校验器、
plan/trace/report/result 产物布局），只换工具集与数据源。

所有派生指标（额度使用率、最长逾期月数、逾期次数、还款覆盖率等）都在 SQL 里
现算，不在数据装载阶段固化——保证报告里的每个数字都能回溯到同一条 SQL。
"""

from __future__ import annotations

import csv
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .tools import Tool

_ROOT = Path(__file__).resolve().parent.parent
_DATA_DIR = _ROOT / "data"
_TABLE = "clients"
_CSV = _DATA_DIR / "credit_clients.csv"

# 全部为整型列（UCI 原始数据字典：额度/账单/还款金额、年龄、状态码、标签）
_INT_COLS = {
    "id", "limit_bal", "sex", "education", "marriage", "age",
    "pay_0", "pay_2", "pay_3", "pay_4", "pay_5", "pay_6",
    "bill_amt1", "bill_amt2", "bill_amt3", "bill_amt4", "bill_amt5", "bill_amt6",
    "pay_amt1", "pay_amt2", "pay_amt3", "pay_amt4", "pay_amt5", "pay_amt6",
    "is_default",
}

# 还款状态码（UCI 数据字典）
PAY_STATUS_DESC = {
    -2: "循环信用/未消费",
    -1: "正常还款",
    0: "最低还款",
    1: "逾期 1 个月",
    2: "逾期 2 个月",
    3: "逾期 3 个月",
    4: "逾期 4 个月",
    5: "逾期 5 个月",
    6: "逾期 6 个月",
    7: "逾期 7 个月",
    8: "逾期 8 个月",
    9: "逾期 9 个月及以上",
}


def _read_csv(path: Path) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_finance_db(data_dir: Optional[Path] = None) -> sqlite3.Connection:
    """把信贷数据集装进内存 SQLite（单表 clients，30,000 户）。"""
    base = Path(data_dir) if data_dir else _DATA_DIR
    csv_path = base / "credit_clients.csv"
    if not csv_path.exists():
        raise FileNotFoundError(
            f"数据文件不存在：{csv_path}（先运行 scripts/prepare_credit.py）")

    rows = _read_csv(csv_path)
    if not rows:
        raise ValueError(f"数据文件为空：{csv_path}")

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # default 是 SQLite 保留字，装载时重命名为 is_default
    cols = ["is_default" if c == "default" else c for c in rows[0].keys()]
    col_defs = ", ".join(f'"{c}" INTEGER' for c in cols)
    conn.execute(f"CREATE TABLE {_TABLE} ({col_defs})")
    placeholders = ", ".join("?" for _ in cols)
    conn.executemany(
        f"INSERT INTO {_TABLE} VALUES ({placeholders})",
        [[int(float(r[c])) if (r[c] or "").strip() else None for c in rows[0].keys()]
         for r in rows],
    )
    conn.commit()
    return conn


class FinanceState:
    """分析运行期共享状态：一个连接 + clients 表。

    DAG 同层步骤在不同线程并发查询，sqlite3 连接必须允许跨线程并串行化访问。
    """

    def __init__(self, data_dir: Optional[Path] = None) -> None:
        self.conn: Optional[sqlite3.Connection] = None
        self.data_dir = str(data_dir) if data_dir else str(_DATA_DIR)
        self.row_counts: Dict[str, int] = {}
        # RLock：query() 持锁期间会再调 ensure_loaded()，需要可重入。
        self._lock = threading.RLock()

    def ensure_loaded(self, data_dir: Optional[str] = None) -> sqlite3.Connection:
        with self._lock:
            if self.conn is None or (data_dir and data_dir != self.data_dir):
                if self.conn is not None:
                    self.conn.close()
                self.conn = load_finance_db(Path(data_dir) if data_dir else None)
                self.data_dir = data_dir or self.data_dir
                self.row_counts = {
                    t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in (_TABLE,)
                }
            return self.conn

    def query(self, sql: str) -> Tuple[List[str], List[Dict[str, Any]]]:
        """加锁执行只读查询，返回 (列名, 字典行)；跨线程串行化。"""
        with self._lock:
            conn = self.ensure_loaded()
            cursor = conn.execute(sql)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            return columns, [dict(r) for r in cursor.fetchmany(2000)]


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


def file_read(state: FinanceState, path: str = "", **_: Any) -> Dict[str, Any]:
    """读取信贷数据集：行数、字段、缺失值统计、样例与整体风险概览。"""
    if path:
        state.ensure_loaded(path if Path(path).is_dir() else None)
    else:
        state.ensure_loaded()

    cols, rows = state.query(f"SELECT * FROM {_TABLE} LIMIT 0")
    count = state.row_counts.get(_TABLE, 0)
    sample_cols = ["id", "limit_bal", "age", "education", "marriage",
                   "pay_0", "bill_amt1", "pay_amt1", "is_default"]
    existing = [c for c in sample_cols if c in cols]
    col_list = ", ".join('"' + c + '"' for c in existing)
    _, sample = (state.query(f"SELECT {col_list} FROM {_TABLE} LIMIT 3")
                 if existing else ([], []))

    # 缺失值（按列统计）
    null_sql = "SELECT " + ", ".join(
        f'SUM(CASE WHEN "{c}" IS NULL OR "{c}"=\'\' THEN 1 ELSE 0 END)' for c in cols
    ) + f" FROM {_TABLE}"
    _, null_rows = state.query(null_sql)
    missing = {c: (null_rows[0].get(c) or 0) if null_rows else 0 for c in cols}

    # 整体风险概览
    _, ov = state.query(
        f"SELECT COUNT(*) AS clients, "
        f"SUM(is_default) AS defaults, "
        f"ROUND(SUM(is_default)*100.0/COUNT(*),2) AS default_rate, "
        f"MIN(limit_bal) AS limit_min, MAX(limit_bal) AS limit_max, "
        f"ROUND(AVG(limit_bal)) AS limit_avg, "
        f"MIN(age) AS age_min, MAX(age) AS age_max, "
        f"ROUND(AVG(age),1) AS age_avg, "
        f"ROUND(AVG(CAST(bill_amt1 AS REAL)/NULLIF(limit_bal,0)),4) AS util_avg "
        f"FROM {_TABLE}"
    )
    overview = ov[0] if ov else {}

    return {
        "data_dir": state.data_dir,
        "tables": {_TABLE: {"rows": count, "columns": cols, "sample": sample}},
        "clients_missing_total": sum(missing.values()),
        "overview": overview,
        "pay_status_desc": PAY_STATUS_DESC,
    }


def sql_query(state: FinanceState, sql: str = "", **_: Any) -> Dict[str, Any]:
    cleaned = (sql or "").strip().rstrip(";")
    if not cleaned.upper().startswith("SELECT"):
        raise ValueError("只允许 SELECT 查询（风控数据源只读）")
    if ";" in cleaned:
        raise ValueError("不允许多语句查询")
    upper = cleaned.upper()
    for kw in ("ATTACH", "DETACH", "PRAGMA", "INSERT", "UPDATE", "DELETE", "DROP", "CREATE"):
        if kw in upper:
            raise ValueError(f"检测到受限关键字：{kw}")
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


_AGGOPS = {
    "min": lambda values: min(values),
    "max": lambda values: max(values),
    "sum": lambda values: sum(values),
    "first": lambda values: values[0],
    "last": lambda values: values[-1],
}


def report_render(
    state: FinanceState,
    title: str = "信贷风险分析报告",
    metrics: Optional[List[Dict[str, Any]]] = None,
    findings: Optional[List[str]] = None,
    conclusion: str = "",
    refs: Optional[Dict[str, Dict[str, Any]]] = None,
    tables: Optional[List[Dict[str, Any]]] = None,
    inject_error: bool = False,
    **_: Any,
) -> Dict[str, Any]:
    """生成 Markdown 风险报告（数据驱动）。

    ``metrics`` 每项支持两种取值方式：

    - 引用：``{name, current:{step,where,column}, baseline:{...}|null, unit}``
    - 聚合：``{name, agg:"min|max|sum|first|last", step, column, unit}``

    值在工具内从 ``refs``（$ref 解析后的步骤结果）中查表计算；校验器会用同一套
    查找逻辑独立重算比对——数字与数据源不一致就会被拦截。``tables`` 直接把 SQL
    结果渲染成 Markdown 表格，保证报告内容 100% 来自数据源。
    """
    refs = refs or {}
    lines: List[str] = [f"# {title}", ""]
    resolved: List[Dict[str, Any]] = []

    def _rows(step_id: str) -> List[Dict[str, Any]]:
        step = refs.get(step_id or "")
        if not isinstance(step, dict):
            raise KeyError(f"引用的步骤结果不存在：{step_id}")
        return step.get("rows") or []

    def _resolve(ref: Dict[str, Any]) -> float:
        return _lookup(_rows(ref.get("step") or ""), ref.get("where") or {},
                       ref.get("column") or "")

    def _aggregate(spec: Dict[str, Any]) -> float:
        rows = _rows(spec.get("step") or "")
        column = spec.get("column") or ""
        values = [float(r[column]) for r in rows if r.get(column) is not None]
        if not values:
            raise ValueError(f"聚合列 {column} 没有可用值")
        op = _AGGOPS.get(spec.get("agg") or "")
        if op is None:
            raise ValueError(f"未知聚合方式：{spec.get('agg')}")
        return op(values)

    for m in metrics or []:
        if m.get("agg"):
            current = _aggregate(m)
            baseline = None
        else:
            current = _resolve(m["current"])
            baseline = _resolve(m["baseline"]) if m.get("baseline") else None
        unit = m.get("unit", "元")
        if baseline is None:
            value = current
            text = f"{value:,.0f}" if unit != "%" else f"{value:.1f}%"
        elif unit == "%":
            value = (current - baseline) / baseline * 100 if baseline else 0.0
            text = f"{value:+.1f}%"
        else:
            value = current - baseline
            text = f"{value:+,.0f}"
        if inject_error and unit == "%":
            value = value * 0.5  # 注入：把关键指标算错一半，校验器必须拦截
            text = f"{value:+.1f}%"
        resolved.append({
            "name": m.get("name", ""), "value": round(value, 4), "text": text,
            "unit": unit, "current": current, "baseline": baseline,
        })
        lines.append(f"- **{m.get('name', '')}**：{text}")

    for t in tables or []:
        rows = _rows(t.get("step") or "")
        columns = t.get("columns") or (list(rows[0].keys()) if rows else [])
        lines += ["", f"## {t.get('title') or '数据明细'}", ""]
        if not rows:
            lines.append("（无数据）")
            continue
        lines.append("| " + " | ".join(str(c) for c in columns) + " |")
        lines.append("| " + " | ".join("---" for _ in columns) + " |")
        for row in rows[: t.get("limit", 50)]:
            lines.append("| " + " | ".join(str(row.get(c, "")) for c in columns) + " |")

    lines += ["", "## 发现", ""]
    for item in findings or []:
        lines.append(f"- {item}")
    lines += ["", "## 结论", "", conclusion or "（无）", ""]
    markdown = "\n".join(lines)
    return {
        "title": title, "markdown": markdown, "metrics": resolved,
        "facts": {"metric_count": len(resolved), "title": title,
                  "table_count": len(tables or [])},
    }


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


def build_finance_registry(state: Optional[FinanceState] = None):
    from .tools import ToolRegistry

    state = state or FinanceState()
    registry = ToolRegistry()
    for tool in (
        Tool(
            name="file_read",
            description="读取信贷风控数据集（clients 表），返回行数、字段、缺失值、样例与整体风险概览",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "数据目录，默认内置 UCI 信贷数据"}},
            },
            fn=lambda **kw: file_read(state, **kw),
            risk=0,
            category="finance",
            returns="tables/clients_missing_total/overview",
        ),
        Tool(
            name="sql_query",
            description="对信贷数据执行只读 SELECT 查询（支持额度/还款状态/账单/还款额任意维度聚合）",
            parameters={
                "type": "object",
                "properties": {"sql": {"type": "string", "description": "SELECT 语句"}},
                "required": ["sql"],
            },
            fn=lambda **kw: sql_query(state, **kw),
            risk=0,
            category="finance",
            returns="sql/columns/rows/row_count",
        ),
        Tool(
            name="report_render",
            description="从 SQL 结果解析指标引用并生成 Markdown 风险报告",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "metrics": {"type": "array"},
                    "findings": {"type": "array", "items": {"type": "string"}},
                    "conclusion": {"type": "string"},
                    "refs": {"type": "object"},
                    "tables": {"type": "array", "description": "把 SQL 结果渲染成 Markdown 表格"},
                    "inject_error": {"type": "boolean", "description": "演示/消融开关：渲染时故意算错关键指标"},
                },
                "required": ["title"],
            },
            fn=lambda **kw: report_render(state, **kw),
            risk=0,
            category="finance",
            returns="title/markdown/metrics/facts",
        ),
    ):
        registry.register(tool)
    return registry
