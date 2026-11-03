#!/usr/bin/env python3
"""Superstore 公开数据集预处理（177 赛题·智慧零售经营分析）。

原始文件是 Tableau Superstore 工作簿导出的多表拼接 CSV，包含三张表：
  1. Orders  —— 9994 行订单明细（字段 21 列）
  2. Returns —— 退货订单号清单（表头 `Returned | Order ID`）
  3. People  —— 区域负责人（表头 `Person | Region`）

本脚本把三张表拆开、把 Orders 的日期从混合格式（`MM-DD-YYYY` / `M/D/YYYY`）
统一成 ISO `YYYY-MM-DD`，输出到 ``data/`` 下三个干净 CSV，供分析场景装载。

用法：
    python scripts/prepare_superstore.py [原始CSV路径]

原始文件来源（公开数据集，仅用于学习/竞赛演示）：
    https://github.com/AnalystHub-Hub/Superstore-Sales-Analysis
"""

from __future__ import annotations

import csv
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# Orders 表的标准列（Tableau Superstore 工作簿顺序）
ORDER_COLS = [
    "Row ID", "Order ID", "Order Date", "Ship Date", "Ship Mode",
    "Customer ID", "Customer Name", "Segment", "Country", "City", "State",
    "Postal Code", "Region", "Product ID", "Category", "Sub-Category",
    "Product Name", "Sales", "Quantity", "Discount", "Profit",
]

_DATE_DASH = re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$")
_DATE_SLASH = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def norm_date(raw: str) -> str:
    """把 `MM-DD-YYYY` / `M/D/YYYY` 统一成 `YYYY-MM-DD`；空值保持空。"""
    text = (raw or "").strip()
    if not text:
        return ""
    m = _DATE_DASH.match(text)
    if m:
        mm, dd, yyyy = m.groups()
    else:
        m = _DATE_SLASH.match(text)
        if not m:
            return text
        mm, dd, yyyy = m.groups()
    return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"


def split_sheets(rows: list) -> tuple:
    """按 Row ID 是否为纯数字切出 Orders 段；其余按表头行切 Returns / People。"""
    orders: list = []
    returns: list = []
    people: list = []
    section = "orders"
    for row in rows:
        rid = (row.get("Row ID") or "").strip()
        if rid.isdigit():
            section = "orders"
            orders.append(row)
            continue
        if rid == "Returned":  # Returns 表头
            section = "returns"
            continue
        if rid == "Person":  # People 表头
            section = "people"
            continue
        if section == "returns":
            returns.append(row)
        elif section == "people":
            people.append(row)
    return orders, returns, people


def write_csv(path: Path, cols: list, rows: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in cols})


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DATA_DIR / "superstore_raw.csv"
    if not src.is_file():
        print(f"找不到原始文件：{src}", file=sys.stderr)
        return 2

    with open(src, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    orders, returns, people = split_sheets(rows)
    if not orders:
        print("解析失败：没有切出 Orders 段", file=sys.stderr)
        return 1

    for row in orders:
        row["Order Date"] = norm_date(row.get("Order Date", ""))
        row["Ship Date"] = norm_date(row.get("Ship Date", ""))

    write_csv(DATA_DIR / "superstore_orders.csv", ORDER_COLS, orders)

    # Returns / People 两段沿用 Orders 表头，需要按列义映射：
    #   Returns 段：Row ID="Yes"，Order ID=订单号
    #   People  段：Row ID=负责人姓名，Order ID=区域
    returns_mapped = [
        {"Order ID": r.get("Order ID", ""), "Returned": r.get("Row ID", "")} for r in returns
    ]
    people_mapped = [
        {"Person": r.get("Row ID", ""), "Region": r.get("Order ID", "")} for r in people
    ]
    write_csv(DATA_DIR / "superstore_returns.csv", ["Order ID", "Returned"], returns_mapped)
    write_csv(DATA_DIR / "superstore_people.csv", ["Person", "Region"], people_mapped)

    years = Counter(r["Order Date"][:4] for r in orders if r["Order Date"])
    print(f"Orders : {len(orders)} 行 → data/superstore_orders.csv（年份 {dict(sorted(years.items()))}）")
    print(f"Returns: {len(returns)} 行 → data/superstore_returns.csv")
    print(f"People : {len(people)} 行 → data/superstore_people.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
