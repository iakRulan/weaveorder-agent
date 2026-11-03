#!/usr/bin/env python3
"""给申报 PDF 加页码（底部居中，跳过封面）。

用法：
    python3 scripts/add_page_numbers.py <输入.pdf> <输出.pdf> [起始页码]

依赖 PyMuPDF（`pip install pymupdf`）。Chrome 无头打印不支持 CSS 页码盒，
因此用 PyMuPDF 在渲染后盖章，保证每页页码位置一致。
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    start = int(sys.argv[3]) if len(sys.argv) > 3 else 2

    try:
        import fitz
    except ImportError:
        print("[error] 需要 PyMuPDF：pip install pymupdf", file=sys.stderr)
        return 2

    doc = fitz.open(src)
    total = doc.page_count
    for index, page in enumerate(doc):
        if index + 1 < start:
            continue
        rect = page.rect
        label = str(index + 1)
        # 底部居中，距下边距约 14pt
        width = fitz.get_text_length(label, fontname="helv", fontsize=9)
        x = (rect.width - width) / 2
        page.insert_text((x, rect.height - 14), label,
                         fontname="helv", fontsize=9, color=(0.45, 0.45, 0.45))
    doc.save(dst, garbage=3, deflate=True)
    doc.close()
    print(f"[ok] {dst}（{dst.stat().st_size:,} B，共 {total} 页，页码 {start}-{total}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
