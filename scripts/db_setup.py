# -*- coding: utf-8 -*-
"""PC28 数据维护：CSV 导入 / 校验 / 导出。

所有数据库操作统一走 app/storage 存储层，因此 SQLite / PostgreSQL / MySQL
共用同一套脚本，无需关心底层引擎。
"""
import argparse
import csv
import os
import sys


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from storage import get_storage


BASE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE, "pc28_history_29999.csv")


def _meta(s: int):
    size = "大" if s >= 14 else "小"
    parity = "双" if s % 2 == 0 else "单"
    return size, parity, size + parity


def read_csv_rows():
    rows = []
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            parts = r["draw_number"].split("+")
            s = int(r["draw_num"])
            size, parity, combo = _meta(s)
            rows.append({
                "draw_nbr": int(r["draw_nbr"]),
                "draw_date": r["draw_date"],
                "draw_time": r["draw_time"],
                "c1": int(parts[0]),
                "c2": int(parts[1]),
                "c3": int(parts[2]),
                "draw_num": s,
                "size_type": size,
                "parity_type": parity,
                "combination_type": combo,
            })
    return rows


def migrate():
    if not os.path.exists(CSV_PATH):
        print(f"CSV 不存在: {CSV_PATH} (已使用数据库存储, 无需迁移)")
        return False
    rows = read_csv_rows()
    print(f"CSV 读取: {len(rows)} 期")
    added = get_storage().insert(rows)
    print(f"已写入存储: 新增 {added} 期")
    return True


def verify():
    r = get_storage().verify()
    print(f"行数: {r.rows:,}  重复主键: {r.duplicates}  和值错误: {r.bad_sum}")
    print(f"期号范围: {r.min_nbr} ~ {r.max_nbr}  最新日期: {r.max_date}")
    print("数据库校验:", "通过" if r.ok else "失败")
    return r.ok


def export_csv(out_path=None):
    out_path = out_path or CSV_PATH
    rows = get_storage().export_rows()
    with open(out_path, "w", encoding="utf-8-sig", newline="\n") as f:
        w = csv.writer(f)
        w.writerow([
            "draw_nbr", "draw_date", "draw_time", "draw_number", "draw_num",
            "size_type", "parity_type", "combination_type",
        ])
        for r in rows:
            w.writerow([
                r[0], r[1], r[2], f"{r[3]}+{r[4]}+{r[5]}", r[6], r[7], r[8], r[9],
            ])
    print(f"已导出: {out_path} ({len(rows)} 期)")


def main():
    parser = argparse.ArgumentParser(description="PC28 数据维护: CSV -> 数据库")
    parser.add_argument("--verify", action="store_true", help="仅校验数据库")
    parser.add_argument("--export", action="store_true", help="数据库导出为 CSV")
    args = parser.parse_args()

    if args.export:
        export_csv()
        return
    if not args.verify:
        migrate()
    verify()


if __name__ == "__main__":
    main()
