# -*- coding: utf-8 -*-
"""PC28 开奖数据更新：拉取最新数据并写入存储 (复用 app/collector 的数据源)。"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from collector import fetch_pc28help, fetch_wh28_history, insert_rows, verify


CN_TZ = timezone(timedelta(hours=8))


def fetch_wh28(days):
    rows = []
    for i in range(days):
        d = (datetime.now(CN_TZ).date() - timedelta(days=i)).isoformat()
        try:
            day_rows = fetch_wh28_history(d)
            rows.extend(day_rows)
            print(f"  [{d}] 获取 {len(day_rows)} 期")
        except Exception as e:
            print(f"  [{d}] 失败: {e}")
    return rows


def main():
    parser = argparse.ArgumentParser(description="PC28 开奖数据更新")
    parser.add_argument("--nbr", type=int, default=2000,
                        help="pc28 源拉取期数 (默认2000, 最多30000)")
    parser.add_argument("--source", choices=["pc28", "wh28"], default="pc28",
                        help="数据源")
    parser.add_argument("--days", type=int, default=1,
                        help="wh28 源拉取天数")
    parser.add_argument("--verify", action="store_true", help="仅校验数据库")
    args = parser.parse_args()

    if args.verify:
        ok = verify()
        sys.exit(0 if ok else 1)

    if args.source == "wh28":
        rows = fetch_wh28(args.days)
        print(f"wh28 共获取 {len(rows)} 期 (每天仅最新100期, 可能不完整)")
    else:
        if args.nbr > 30000:
            print("nbr 最大 30000")
            sys.exit(1)
        rows = fetch_pc28help(args.nbr)
        print(f"pc28 共获取 {len(rows)} 期")

    if not rows:
        print("未获取到数据")
        sys.exit(1)
    insert_rows(rows)
    ok = verify()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
