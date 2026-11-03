"""智慧零售经营分析场景工具层（177 赛题）。

数据来自公开的 Tableau Superstore 零售数据集（订单 / 退货 / 区域负责人三张表），
装载进内存 SQLite 后由 ``sql_query`` 真执行只读 SELECT。与差旅、经营分析共用
同一套引擎原语（Tool Registry 的 Schema 校验/风险分级、EventBus/Trace 事件
溯源、确定性校验器、plan/trace/report/result 产物布局），只换工具集与数据源。

跨表贯通：``orders`` 与 ``returns`` 通过 ``order_id`` 关联，可计算退货率；
``people`` 提供区域负责人维度。
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

# 原始列 → 分析用列（snake_case，避免 SQL 里反复引号）
COLUMN_MAP = {
    "Row ID": "row_id",
    "Order ID": "order_id",
    "Order Date": "order_date",
    "Ship Date": "ship_date",
    "Ship Mode": "ship_mode",
    "Customer ID": "customer_id",
    "Customer Name": "customer_name",
    "Segment": "segment",
    "Country": "country",
    "City": "city",
    "State": "state",
    "Postal Code": "postal_code",
    "Region": "region",
    "Product ID": "product_id",
    "Category": "category",
    "Sub-Category": "sub_category",
    "Product Name": "product_name",
    "Sales": "sales",
    "Quantity": "quantity",
    "Discount": "discount",
    "Profit": "profit",
}

NUMERIC_COLS = {"row_id": "INTEGER", "sales": "REAL", "quantity": "INTEGER",
                "discount": "REAL", "profit": "REAL"}

TABLES = {
    "orders": _DATA_DIR / "superstore_orders.csv",
    "returns": _DATA_DIR / "superstore_returns.csv",
    "people": _DATA_DIR / "superstore_people.csv",
}


def _read_csv(path: Path) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_retail_db(data_dir: Optional[Path] = None) -> sqlite3.Connection:
    """把三张公开数据表装进内存 SQLite（orders / returns / people）。"""
    base = Path(data_dir) if data_dir else _DATA_DIR
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row

    orders = _read_csv(base / "superstore_orders.csv")
    if not orders:
        raise FileNotFoundError(f"数据文件不存在或为空：{base / 'superstore_orders.csv'}")

    cols = list(COLUMN_MAP.values())
    col_defs = ", ".join(f'"{c}" {NUMERIC_COLS.get(c, "TEXT")}' for c in cols)
    conn.execute(f"CREATE TABLE orders ({col_defs})")
    placeholders = ", ".join("?" for _ in cols)
    for row in orders:
        values = []
        for raw_name, clean_name in COLUMN_MAP.items():
            raw = (row.get(raw_name) or "").strip()
            if clean_name in NUMERIC_COLS and raw == "":
                values.append(None)
            elif clean_name == "row_id" or NUMERIC_COLS.get(clean_name) == "INTEGER":
                values.append(int(float(raw)) if raw else None)
            elif NUMERIC_COLS.get(clean_name) == "REAL":
                values.append(float(raw) if raw else None)
            else:
                values.append(raw)
        conn.execute(f"INSERT INTO orders VALUES ({placeholders})", values)

    returns = _read_csv(base / "superstore_returns.csv")
    conn.execute("CREATE TABLE returns (order_id TEXT, returned TEXT)")
    for r in returns:
        conn.execute("INSERT INTO returns VALUES (?, ?)",
                     ((r.get("Order ID") or "").strip(), (r.get("Returned") or "").strip()))

    people = _read_csv(base / "superstore_people.csv")
    conn.execute("CREATE TABLE people (person TEXT, region TEXT)")
    for r in people:
        conn.execute("INSERT INTO people VALUES (?, ?)",
                     ((r.get("Person") or "").strip(), (r.get("Region") or "").strip()))

    conn.commit()
    return conn


class RetailState:
    """分析运行期共享状态：一个连接 + 三张表。

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
                self.conn = load_retail_db(Path(data_dir) if data_dir else None)
                self.data_dir = data_dir or self.data_dir
                self.row_counts = {
                    t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                    for t in ("orders", "returns", "people")
                }
            return self.conn

    def query(self, sql: str) -> Tuple[List[str], List[Dict[str, Any]]]:
        """加锁执行只读查询，返回 (列名, 字典行)；跨线程串行化。"""
        with self._lock:
            conn = self.ensure_loaded()
            cursor = conn.execute(sql)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            return columns, [dict(r) for r in cursor.fetchmany(500)]


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------


def file_read(state: RetailState, path: str = "", **_: Any) -> Dict[str, Any]:
    """读取零售数据集：三张表的行数、字段、缺失值统计与样例。"""
    if path:
        state.ensure_loaded(path if Path(path).is_dir() else None)
    else:
        state.ensure_loaded()

    profile: Dict[str, Any] = {}
    for table in ("orders", "returns", "people"):
        cols, rows = state.query(f"SELECT * FROM {table} LIMIT 0")
        count = state.row_counts.get(table, 0)
        sample_cols = ["order_id", "order_date", "region", "category", "sub_category",
                       "sales", "profit", "discount"] if table == "orders" else cols
        existing = [c for c in sample_cols if c in cols]
        col_list = ", ".join('"' + c + '"' for c in existing)
        _, sample = (state.query(f"SELECT {col_list} FROM {table} LIMIT 3")
                     if existing else ([], []))
        profile[table] = {"rows": count, "columns": cols, "sample": sample}

    # 订单表缺失值（按列统计）
    orders_cols = profile["orders"]["columns"]
    null_sql = "SELECT " + ", ".join(
        f'SUM(CASE WHEN "{c}" IS NULL OR "{c}"=\'\' THEN 1 ELSE 0 END)' for c in orders_cols
    ) + " FROM orders"
    _, null_rows = state.query(null_sql)
    missing = {c: (null_rows[0].get(c) or 0) if null_rows else 0 for c in orders_cols}
    profile["orders"]["missing"] = {k: v for k, v in missing.items() if v}

    # 日期范围与数值概览
    _, range_rows = state.query(
        "SELECT MIN(order_date) AS lo, MAX(order_date) AS hi, "
        "ROUND(SUM(sales), 2) AS sales, ROUND(SUM(profit), 2) AS profit FROM orders"
    )
    overview = range_rows[0] if range_rows else {}
    return {
        "data_dir": state.data_dir,
        "tables": profile,
        "orders_missing_total": sum(profile["orders"]["missing"].values()),
        "overview": overview,
    }


def sql_query(state: RetailState, sql: str = "", **_: Any) -> Dict[str, Any]:
    cleaned = (sql or "").strip().rstrip(";")
    if not cleaned.upper().startswith("SELECT"):
        raise ValueError("只允许 SELECT 查询（分析场景数据源只读）")
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
    state: RetailState,
    title: str = "经营分析报告",
    metrics: Optional[List[Dict[str, Any]]] = None,
    findings: Optional[List[str]] = None,
    conclusion: str = "",
    refs: Optional[Dict[str, Dict[str, Any]]] = None,
    tables: Optional[List[Dict[str, Any]]] = None,
    inject_error: bool = False,
    **_: Any,
) -> Dict[str, Any]:
    """生成 Markdown 分析报告（数据驱动）。

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
            text = f"{value:,.0f}" if unit != "%" else f"{value:+.1f}%"
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


def build_retail_registry(state: Optional[RetailState] = None):
    from .tools import ToolRegistry

    state = state or RetailState()
    registry = ToolRegistry()
    for tool in (
        Tool(
            name="file_read",
            description="读取零售数据集（orders/returns/people 三张表），返回行数、字段、缺失值与样例",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "数据目录，默认内置 Superstore 数据"}},
            },
            fn=lambda **kw: file_read(state, **kw),
            risk=0,
            category="retail",
            returns="tables/orders_missing_total/overview",
        ),
        Tool(
            name="sql_query",
            description="对零售数据执行只读 SELECT 查询（支持 orders/returns/people 跨表 JOIN）",
            parameters={
                "type": "object",
                "properties": {"sql": {"type": "string", "description": "SELECT 语句"}},
                "required": ["sql"],
            },
            fn=lambda **kw: sql_query(state, **kw),
            risk=0,
            category="retail",
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
                    "tables": {"type": "array", "description": "把 SQL 结果渲染成 Markdown 表格"},
                    "inject_error": {"type": "boolean", "description": "演示/消融开关：渲染时故意算错关键指标"},
                },
                "required": ["title"],
            },
            fn=lambda **kw: report_render(state, **kw),
            risk=0,
            category="retail",
            returns="title/markdown/metrics/facts",
        ),
    ):
        registry.register(tool)
    return registry
