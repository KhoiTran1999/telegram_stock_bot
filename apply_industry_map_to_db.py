#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Đọc industry_map_from_topi.csv và cập nhật cột industry
trong bảng stock_value_cache (Postgres).

Chỉ cập nhật những dòng:
    - symbol có trong CSV
    - industry hiện tại là NULL hoặc 'Khác'

Yêu cầu:
    - Đã set DATABASE_URL (giống khi chạy bot)
    - Bảng stock_value_cache đã tồn tại
"""

import csv
from db_utils import get_conn


CSV_PATH = "industry_map_from_topi.csv"


def load_industry_map(path: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return {}

        cols = [c.lower() for c in reader.fieldnames]
        try:
            symbol_idx = cols.index("symbol")
        except ValueError:
            print(f"❌ CSV {path} không có cột 'symbol'.")
            return {}

        if "industry" in cols:
            industry_idx = cols.index("industry")
        elif "industry_raw" in cols:
            industry_idx = cols.index("industry_raw")
        else:
            print(f"❌ CSV {path} không có cột 'industry' hoặc 'industry_raw'.")
            return {}

        for row in reader:
            values = list(row.values())
            sym = (values[symbol_idx] or "").strip().upper()
            ind = (values[industry_idx] or "").strip()
            if not sym or not ind:
                continue
            mapping[sym] = ind

    return mapping


def main():
    print(f"📄 Đang đọc {CSV_PATH} ...")
    mapping = load_industry_map(CSV_PATH)
    print(f"✅ Đọc được {len(mapping)} mã có ngành từ CSV.")

    if not mapping:
        print("❌ Không có dữ liệu mapping, dừng.")
        return

    with get_conn() as conn:
        with conn.cursor() as cur:
            updated = 0
            for sym, ind in mapping.items():
                cur.execute(
                    """
                    UPDATE stock_value_cache
                    SET industry = %s
                    WHERE symbol = %s
                      AND (industry IS NULL OR industry = 'Khác')
                    """,
                    (ind, sym),
                )
                if cur.rowcount > 0:
                    updated += cur.rowcount

        conn.commit()

    print(f"🎉 Hoàn tất. Đã cập nhật industry cho {updated} dòng trong stock_value_cache.")


if __name__ == "__main__":
    main()
