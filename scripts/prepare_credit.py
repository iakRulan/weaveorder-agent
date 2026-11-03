#!/usr/bin/env python3
"""准备信贷风控数据集（180 赛题 · 智能信贷风控场景）。

数据源：UCI Machine Learning Repository
  「Default of Credit Card Clients」https://archive.ics.uci.edu/dataset/350/
  台湾地区某银行 2005 年 4–9 月信用卡授信客户，30,000 户、25 个字段。
  原始文件为 .xls（default of credit card clients.xls），需 pandas + xlrd 读取。

产出：``data/credit_clients.csv``（列名 snake_case，原值不变，不做任何派生，
派生指标全部留给 SQL 现算，保证「报告数字 100% 来自数据源」）。

用法：
    python3 scripts/prepare_credit.py [原始 xls 路径]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_XLS = "/tmp/credit180/default of credit card clients.xls"
OUT_CSV = ROOT / "data" / "credit_clients.csv"

# 原始列名 → snake_case（与 UCI 官方数据字典一致）
COLUMN_MAP = {
    "ID": "id",
    "LIMIT_BAL": "limit_bal",
    "SEX": "sex",
    "EDUCATION": "education",
    "MARRIAGE": "marriage",
    "AGE": "age",
    "PAY_0": "pay_0",
    "PAY_2": "pay_2",
    "PAY_3": "pay_3",
    "PAY_4": "pay_4",
    "PAY_5": "pay_5",
    "PAY_6": "pay_6",
    "BILL_AMT1": "bill_amt1",
    "BILL_AMT2": "bill_amt2",
    "BILL_AMT3": "bill_amt3",
    "BILL_AMT4": "bill_amt4",
    "BILL_AMT5": "bill_amt5",
    "BILL_AMT6": "bill_amt6",
    "PAY_AMT1": "pay_amt1",
    "PAY_AMT2": "pay_amt2",
    "PAY_AMT3": "pay_amt3",
    "PAY_AMT4": "pay_amt4",
    "PAY_AMT5": "pay_amt5",
    "PAY_AMT6": "pay_amt6",
    "default payment next month": "default",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="准备信贷风控数据集")
    parser.add_argument("xls", nargs="?", default=DEFAULT_XLS,
                        help="UCI 原始 xls 路径（默认 %(default)s）")
    parser.add_argument("--out", default=str(OUT_CSV), help="输出 CSV 路径")
    args = parser.parse_args()

    xls = Path(args.xls)
    if not xls.exists():
        print(f"[error] 找不到原始数据：{xls}", file=sys.stderr)
        print("  下载：https://archive.ics.uci.edu/static/public/350/"
              "default+of+credit+card+clients.zip", file=sys.stderr)
        return 2

    try:
        import pandas as pd
    except ImportError:
        print("[error] 需要 pandas 读取 xls：pip install pandas xlrd", file=sys.stderr)
        return 2

    raw = pd.ExcelFile(xls).parse("Data", header=1)
    missing = set(COLUMN_MAP) - set(raw.columns)
    if missing:
        print(f"[error] 原始列缺失：{sorted(missing)}", file=sys.stderr)
        return 2

    df = raw[list(COLUMN_MAP)].rename(columns=COLUMN_MAP)
    df = df.dropna(how="any").astype({c: "int64" for c in COLUMN_MAP.values()})
    df = df.sort_values("id").reset_index(drop=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False, encoding="utf-8")

    print(f"[ok] 已写出 {out}  ({len(df)} 行 × {len(df.columns)} 列)")
    print(f"     整体违约率：{df['default'].mean() * 100:.2f}%  "
          f"({int(df['default'].sum())}/{len(df)})")
    print(f"     字段：{', '.join(df.columns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
