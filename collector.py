# -*- coding: utf-8 -*-
"""
PC28 多源采集器
============================================================
支持 4 个 API 数据源，自动故障切换，增量/全量更新。

数据源:
  1. pc28.help CSV    (最多30000期, 有限流, 适合全量回补)
  2. www.pc28.help    (pc28.help 镜像, 限流策略不同)
  3. wh28.com history (最新100期, 无限流, 适合增量更新)
  4. wh28.com trend   (最新30期, 无限流, 备用)

用法:
  python collector.py                 # 增量更新 (优先 wh28, 失败切 pc28)
  python collector.py --full 5000     # 全量拉取 5000 期 (pc28.help)
  python collector.py --full 30000    # 全量拉取最大 30000 期
  python collector.py --source wh28   # 指定数据源
  python collector.py --test          # 测试所有数据源连通性
  python collector.py --verify        # 校验数据库
"""
import argparse
import csv
import io
import json
import os
import sqlite3
import ssl
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(os.environ.get("DB_DIR", BASE), "pc28_history.db")
CN_TZ = timezone(timedelta(hours=8))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

COLUMNS = ["draw_nbr", "draw_date", "draw_time", "c1", "c2", "c3", "draw_num",
           "size_type", "parity_type", "combination_type"]


def http_get(url, timeout=15):
    """HTTP GET，自动重试 1 次。"""
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return r.read().decode("utf-8-sig", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(3)
                continue
            raise
        except Exception as e:
            if attempt == 0:
                time.sleep(1)
                continue
            raise


def calc_meta(s):
    """根据和值计算大小/单双/组合。"""
    size = "大" if s >= 14 else "小"
    parity = "双" if s % 2 == 0 else "单"
    return size, parity, size + parity


# ============================================================
# 数据源 1 & 2: pc28.help (CSV)
# ============================================================

def _parse_csv(text):
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        parts = r["draw_number"].split("+")
        s = int(r["draw_num"])
        size, parity, combo = calc_meta(s)
        rows.append({
            "draw_nbr": int(r["draw_nbr"]),
            "draw_date": r["draw_date"],
            "draw_time": r["draw_time"],
            "c1": int(parts[0]), "c2": int(parts[1]), "c3": int(parts[2]),
            "draw_num": s,
            "size_type": size, "parity_type": parity, "combination_type": combo,
        })
    return rows


def fetch_pc28help(nbr, host="pc28.help"):
    """从 pc28.help 拉取最近 nbr 期 (CSV)。"""
    url = f"https://{host}/api/history/kj.csv?nbr={nbr}"
    text = http_get(url)
    if text.lstrip().startswith("{"):
        err = json.loads(text)
        raise RuntimeError(f"{host}: {err.get('message', text[:100])}")
    return _parse_csv(text)


# ============================================================
# 数据源 3: wh28.com history (JSON)
# ============================================================

def fetch_wh28_history(date_str=None):
    """从 wh28.com 拉取最新 100 期 (或指定日期的 100 期)。"""
    url = "https://wh28.com/api/lottery/history?code=jnd28"
    if date_str:
        url += f"&date={date_str}"
    data = json.loads(http_get(url))
    if data.get("code") != 1:
        raise RuntimeError(f"wh28 history: {data.get('message', 'unknown')}")
    rows = []
    for item in data.get("data", []):
        dt = datetime.fromtimestamp(int(item["time"]), tz=CN_TZ)
        nums = item["open_numbers"]
        s = int(item["open_sum"])
        size, parity, combo = calc_meta(s)
        rows.append({
            "draw_nbr": int(item["issue"]),
            "draw_date": dt.strftime("%Y-%m-%d"),
            "draw_time": dt.strftime("%H:%M:%S"),
            "c1": int(nums[0]), "c2": int(nums[1]), "c3": int(nums[2]),
            "draw_num": s,
            "size_type": size, "parity_type": parity, "combination_type": combo,
        })
    return rows


# ============================================================
# 数据源 4: wh28.com trend (JSON)
# ============================================================

def fetch_wh28_trend():
    """从 wh28.com trend 接口拉取最新 30 期。"""
    url = "https://wh28.com/api/lottery/trend?code=jnd28"
    data = json.loads(http_get(url))
    if data.get("code") != 200:
        raise RuntimeError(f"wh28 trend: {data.get('message', 'unknown')}")
    rows = []
    for item in data.get("data", []):
        dt = datetime.fromtimestamp(int(item["drawTime"]), tz=CN_TZ)
        nums = item["drawCode"]
        s = int(item["drawSum"])
        size, parity, combo = calc_meta(s)
        rows.append({
            "draw_nbr": int(item["drawIssue"]),
            "draw_date": dt.strftime("%Y-%m-%d"),
            "draw_time": dt.strftime("%H:%M:%S"),
            "c1": int(nums[0]), "c2": int(nums[1]), "c3": int(nums[2]),
            "draw_num": s,
            "size_type": size, "parity_type": parity, "combination_type": combo,
        })
    return rows


# ============================================================
# 多源调度
# ============================================================

SOURCES = {
    "pc28":    {"name": "pc28.help",       "fn": lambda: fetch_pc28help(2000, "pc28.help")},
    "pc28www": {"name": "www.pc28.help",   "fn": lambda: fetch_pc28help(2000, "www.pc28.help")},
    "wh28":    {"name": "wh28 history",    "fn": lambda: fetch_wh28_history()},
    "wh28t":   {"name": "wh28 trend",      "fn": fetch_wh28_trend},
}

# 增量更新优先级: 无限流源优先
INCREMENTAL_ORDER = ["wh28", "wh28t", "pc28", "pc28www"]
# 全量更新优先级: 大批量源优先
FULL_ORDER = ["pc28", "pc28www"]


def fetch_with_failover(order, verbose=True):
    """按优先级依次尝试数据源，返回第一个成功的结果。"""
    for key in order:
        src = SOURCES[key]
        try:
            if verbose:
                print(f"  尝试 [{src['name']}]...", end=" ", flush=True)
            rows = src["fn"]()
            if verbose:
                print(f"成功 {len(rows)} 期")
            return rows, src["name"]
        except Exception as e:
            if verbose:
                print(f"失败 ({e})")
            time.sleep(0.5)
    return None, None


# ============================================================
# 入库
# ============================================================

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
    conn = sqlite3.connect(DB_PATH)
    try:
        ensure_schema(conn)
        before = conn.execute("SELECT COUNT(*) FROM draws").fetchone()[0]
        old_max = conn.execute("SELECT MAX(draw_nbr) FROM draws").fetchone()[0] or 0
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
            "FROM draws WHERE draw_nbr > ? ORDER BY draw_nbr DESC LIMIT 5",
            (old_max,)).fetchall()
    finally:
        conn.close()
    added = after - before
    print(f"库内: {before:,} -> {after:,}  (新增 {added})  期号 {old_max} -> {new_max}")
    for r in new_rows[:5]:
        print(f"  最新: {r[0]} {r[1]} {r[2]} {r[3]}+{r[4]}+{r[5]}={r[6]}")
    return added


def verify():
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM draws").fetchone()[0]
    dup = conn.execute("SELECT COUNT(*) FROM (SELECT draw_nbr FROM draws GROUP BY draw_nbr HAVING COUNT(*)>1)").fetchone()[0]
    bad = conn.execute("SELECT COUNT(*) FROM draws WHERE c1+c2+c3 != draw_num").fetchone()[0]
    rng = conn.execute("SELECT MIN(draw_nbr), MAX(draw_nbr), MAX(draw_date) FROM draws").fetchone()
    conn.close()
    ok = dup == 0 and bad == 0
    print(f"校验: 行数 {n:,}  重复 {dup}  和值错误 {bad}  期号 {rng[0]}~{rng[1]}  最新 {rng[2]}")
    print("结果:", "通过" if ok else "失败")
    return ok


# ============================================================
# 主入口
# ============================================================

def cmd_incremental(source=None):
    """增量更新: 拉取最新数据，补充数据库中缺失的期。"""
    print("=== 增量更新 ===")
    order = [source] if source else INCREMENTAL_ORDER
    rows, src_name = fetch_with_failover(order)
    if not rows:
        print("所有数据源均失败!")
        return False
    print(f"来源: {src_name}")
    insert_rows(rows)
    verify()
    return True


def cmd_full(nbr, source=None):
    """全量拉取: 从 pc28.help 拉取大量历史数据。"""
    print(f"=== 全量拉取 {nbr} 期 ===")
    if nbr > 30000:
        print("最大 30000 期")
        nbr = 30000

    # pc28.help 限流策略: 大批量一次请求
    for host in ["pc28.help", "www.pc28.help"]:
        if source and source != ("pc28" if host == "pc28.help" else "pc28www"):
            continue
        try:
            print(f"  尝试 [{host}] nbr={nbr}...", end=" ", flush=True)
            rows = fetch_pc28help(nbr, host)
            print(f"成功 {len(rows)} 期")
            insert_rows(rows)
            verify()
            return True
        except Exception as e:
            print(f"失败 ({e})")
            time.sleep(2)

    print("pc28.help 全部失败，尝试 wh28 补充...")
    # wh28 按天拉取
    rows = []
    for i in range(30):  # 最多 30 天
        d = (datetime.now(CN_TZ).date() - timedelta(days=i)).isoformat()
        try:
            day_rows = fetch_wh28_history(d)
            rows.extend(day_rows)
            print(f"  [{d}] {len(day_rows)} 期")
        except Exception as e:
            print(f"  [{d}] 失败: {e}")
    if rows:
        insert_rows(rows)
        verify()
        return True
    print("全部失败!")
    return False


def cmd_test():
    """测试所有数据源连通性。"""
    print("=== 数据源测试 ===")
    results = {}
    for key, src in SOURCES.items():
        try:
            t0 = time.time()
            rows = src["fn"]()
            dt = time.time() - t0
            latest = rows[0] if rows else None
            results[key] = {"ok": True, "count": len(rows), "time": f"{dt:.2f}s",
                            "latest": f"{latest['draw_nbr']} {latest['c1']}+{latest['c2']}+{latest['c3']}={latest['draw_num']}" if latest else "N/A"}
            print(f"  [{src['name']:16s}] OK  {len(rows):5d} 期  {dt:.2f}s  最新: {results[key]['latest']}")
        except Exception as e:
            results[key] = {"ok": False, "err": str(e)}
            print(f"  [{src['name']:16s}] FAIL  {e}")
    ok_count = sum(1 for r in results.values() if r["ok"])
    print(f"\n可用数据源: {ok_count}/{len(SOURCES)}")
    return ok_count > 0


def main():
    parser = argparse.ArgumentParser(description="PC28 多源采集器")
    parser.add_argument("--full", type=int, metavar="N", help="全量拉取 N 期 (最多30000)")
    parser.add_argument("--source", choices=["pc28", "pc28www", "wh28", "wh28t"], help="指定数据源")
    parser.add_argument("--test", action="store_true", help="测试所有数据源")
    parser.add_argument("--verify", action="store_true", help="校验数据库")
    args = parser.parse_args()

    if args.verify:
        ok = verify()
        sys.exit(0 if ok else 1)

    if args.test:
        ok = cmd_test()
        sys.exit(0 if ok else 1)

    if args.full:
        ok = cmd_full(args.full, args.source)
        sys.exit(0 if ok else 1)

    # 默认: 增量更新
    ok = cmd_incremental(args.source)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
