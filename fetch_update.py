# -*- coding: utf-8 -*-
"""
PC28 开奖数据自动更新: 拉取最新开奖并写入 SQLite
============================================================

用法:
  python fetch_update.py                # 从 pc28.help 拉取最近 2000 期并更新数据库
  python fetch_update.py --nbr 5000     # 指定拉取期数 (最多 30000)
  python fetch_update.py --source wh28  # 备用源 (wh28.com, 每天仅最新100期)
  python fetch_update.py --days 3       # wh28 源拉取最近 N 天
  python fetch_update.py --verify       # 仅校验数据库完整性

数据源:
  主源 pc28.help  /api/history/kj.csv?nbr=N   (最多 30000 期, 小批量不受限)
  备用 wh28.com  /api/lottery/history?code=jnd28&date=YYYY-MM-DD (每天最新 100 期)
"""
import argparse
import csv
import io
import json
import os
import sqlite3
import sys
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "pc28_history.db")

CN_TZ = timezone(timedelta(hours=8))  # 北京时间
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8-sig", errors="replace")


def connect():
    return sqlite3.connect(DB_PATH)


# ============================================================
# 数据源: pc28.help (主)
# ============================================================

def fetch_pc28(nbr):
    """拉取 pc28.help 最近 nbr 期, 返回字典列表 (新->旧)。"""
    url = f"https://pc28.help/api/history/kj.csv?nbr={nbr}"
    text = http_get(url)
    if text.lstrip().startswith("{"):
        try:
            err = json.loads(text)
            raise RuntimeError(f"pc28.help 返回错误: {err.get('message', text[:120])}")
        except json.JSONDecodeError:
            raise RuntimeError(f"pc28.help 返回异常: {text[:120]}")
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        parts = r["draw_number"].split("+")
        rows.append({
            "draw_nbr": int(r["draw_nbr"]),
            "draw_date": r["draw_date"],
            "draw_time": r["draw_time"],
            "c1": int(parts[0]),
            "c2": int(parts[1]),
            "c3": int(parts[2]),
            "draw_num": int(r["draw_num"]),
            "size_type": r["size_type"],
            "parity_type": r["parity_type"],
            "combination_type": r["combination_type"],
        })
    return rows


# ============================================================
# 数据源: wh28.com (备用)
# ============================================================

def fetch_wh28(days):
    """拉取 wh28.com 最近 days 天 (每天最新 100 期), 返回字典列表 (新->旧)。"""
    rows = []
    for i in range(days):
        d = (datetime.now(CN_TZ).date() - timedelta(days=i)).isoformat()
        url = f"https://wh28.com/api/lottery/history?code=jnd28&date={d}"
        data = json.loads(http_get(url))
        if data.get("code") != 1:
            print(f"  [{d}] 返回异常: {data}")
            continue
        for item in data.get("data", []):
            dt = datetime.fromtimestamp(int(item["time"]), tz=CN_TZ)
            nums = item["open_numbers"]
            s = int(item["open_sum"])
            size = "大" if s >= 14 else "小"
            parity = "双" if s % 2 == 0 else "单"
            rows.append({
                "draw_nbr": int(item["issue"]),
                "draw_date": dt.strftime("%Y-%m-%d"),
                "draw_time": dt.strftime("%H:%M:%S"),
                "c1": int(nums[0]),
                "c2": int(nums[1]),
                "c3": int(nums[2]),
                "draw_num": s,
                "size_type": size,
                "parity_type": parity,
                "combination_type": size + parity,
            })
        print(f"  [{d}] 获取 {len(data.get('data', []))} 期")
    return rows


# ============================================================
# 入库与校验
# ============================================================

COLUMNS = ["draw_nbr", "draw_date", "draw_time", "c1", "c2", "c3", "draw_num",
           "size_type", "parity_type", "combination_type"]


def ensure_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS draws (
            draw_nbr INTEGER PRIMARY KEY,
            draw_date TEXT NOT NULL,
            draw_time TEXT NOT NULL,
            c1 INTEGER NOT NULL,
            c2 INTEGER NOT NULL,
            c3 INTEGER NOT NULL,
            draw_num INTEGER NOT NULL,
            size_type TEXT NOT NULL,
            parity_type TEXT NOT NULL,
            combination_type TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_draws_date ON draws(draw_date);
    """)


def insert_rows(rows):
    conn = connect()
    try:
        ensure_schema(conn)
        before = conn.execute("SELECT COUNT(*) FROM draws").fetchone()[0]
        old_max = conn.execute("SELECT MAX(draw_nbr) FROM draws").fetchone()[0]
        conn.executemany(
            "INSERT OR REPLACE INTO draws (" + ", ".join(COLUMNS) + ") "
            "VALUES (" + ", ".join("?" for _ in COLUMNS) + ")",
            [tuple(r[c] for c in COLUMNS) for r in rows],
        )
        conn.commit()
        after = conn.execute("SELECT COUNT(*) FROM draws").fetchone()[0]
        new_max = conn.execute("SELECT MAX(draw_nbr) FROM draws").fetchone()[0]
        new_rows = conn.execute(
            "SELECT draw_nbr, draw_date, draw_time, c1, c2, c3, draw_num "
            "FROM draws WHERE draw_nbr > ? ORDER BY draw_nbr DESC LIMIT 5", (old_max or 0,)).fetchall()
    finally:
        conn.close()
    added = after - before
    print(f"库内原最大期: {old_max or '空库'} -> 新最大期: {new_max}")
    print(f"新增期数: {added}")
    for r in new_rows[:5]:
        print(f"  最新: {r[0]} {r[1]} {r[2]} {r[3]}+{r[4]}+{r[5]}={r[6]}")
    return added


def verify():
    conn = connect()
    n = conn.execute("SELECT COUNT(*) FROM draws").fetchone()[0]
    dup = conn.execute("SELECT COUNT(*) FROM (SELECT draw_nbr FROM draws GROUP BY draw_nbr HAVING COUNT(*)>1)").fetchone()[0]
    bad = conn.execute("SELECT COUNT(*) FROM draws WHERE c1+c2+c3 != draw_num").fetchone()[0]
    rng = conn.execute("SELECT MIN(draw_nbr), MAX(draw_nbr), MAX(draw_date) FROM draws").fetchone()
    conn.close()
    ok = dup == 0 and bad == 0
    print(f"校验: 行数 {n:,}  重复主键 {dup}  和值错误 {bad}  期号 {rng[0]}~{rng[1]}  最新日期 {rng[2]}")
    print("校验结果:", "通过" if ok else "失败")
    return ok


def main():
    parser = argparse.ArgumentParser(description="PC28 开奖数据自动更新")
    parser.add_argument("--nbr", type=int, default=2000, help="pc28 源拉取期数 (默认2000, 最多30000)")
    parser.add_argument("--source", choices=["pc28", "wh28"], default="pc28", help="数据源")
    parser.add_argument("--days", type=int, default=1, help="wh28 源拉取天数")
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
        rows = fetch_pc28(args.nbr)
        print(f"pc28 共获取 {len(rows)} 期")

    if not rows:
        print("未获取到数据")
        sys.exit(1)
    insert_rows(rows)
    ok = verify()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
