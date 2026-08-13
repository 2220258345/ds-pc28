# -*- coding: utf-8 -*-
"""数据库查询模块 — 所有 SQLite 访问的统一入口。

所有函数直接操作本地 pc28_history.db, 返回原生 tuple 或 dict。
server.py 与 api_server.py 共用本模块, 避免查询逻辑重复。
"""
import os
import sqlite3
import json

# 项目根目录 (app/core/db.py → app/core/ → app/ → 项目根)
BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(os.environ.get("DB_DIR", BASE), "pc28_history.db")

# 特码 (和值) 赔率表: 和值 -> 倍数
SUM_ODDS = {
    0: 920, 27: 920,
    1: 300, 26: 300,
    2: 150, 25: 150,
    3: 90,  24: 90,
    4: 60,  23: 60,
    5: 38,  22: 38,
    6: 30,  21: 30,
    7: 24,  20: 24,
    8: 19,  19: 19,
    9: 16,  18: 16,
    10: 15, 17: 15,
    11: 14, 16: 14,
    12: 13.2, 15: 13.2,
    13: 13.2, 14: 13.2,
}


def _conn():
    """创建一个新连接 (SQLite 连接不可跨线程, 每次查询都新建)。"""
    return sqlite3.connect(DB_PATH)


def get_db_rows():
    """返回 (总行数, 最大期号, 最大日期)。"""
    conn = _conn()
    try:
        n = conn.execute("SELECT COUNT(*) FROM draws").fetchone()[0]
        mx = conn.execute("SELECT MAX(draw_nbr), MAX(draw_date) FROM draws").fetchone()
        return n, mx[0], mx[1]
    except Exception:
        return 0, None, None
    finally:
        conn.close()


def get_latest_draw():
    """获取最新一期 (返回 tuple 或 None)。"""
    conn = _conn()
    try:
        return conn.execute(
            "SELECT draw_nbr, draw_date, draw_time, c1, c2, c3, draw_num, "
            "size_type, parity_type, combination_type "
            "FROM draws ORDER BY draw_nbr DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()


def row_to_latest(row):
    """将原生 tuple 转为 JSON 友好的 dict。"""
    if not row:
        return None
    return {
        "draw_nbr": row[0], "draw_date": row[1], "draw_time": row[2],
        "c1": row[3], "c2": row[4], "c3": row[5], "draw_num": row[6],
        "size_type": row[7], "parity_type": row[8], "combination_type": row[9],
    }


def get_history(page=1, size=30):
    """分页获取历史数据 (按期号倒序)。"""
    conn = _conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM draws").fetchone()[0]
        offset = (page - 1) * size
        rows = conn.execute(
            "SELECT draw_nbr, draw_date, draw_time, c1, c2, c3, draw_num, "
            "size_type, parity_type, combination_type "
            "FROM draws ORDER BY draw_nbr DESC LIMIT ? OFFSET ?",
            (size, offset)
        ).fetchall()
        return {
            "total": total,
            "page": page,
            "size": size,
            "pages": (total + size - 1) // size,
            "list": [{
                "draw_nbr": r[0], "draw_date": r[1], "draw_time": r[2],
                "c1": r[3], "c2": r[4], "c3": r[5], "draw_num": r[6],
                "size_type": r[7], "parity_type": r[8], "combination_type": r[9],
            } for r in rows]
        }
    finally:
        conn.close()


def get_range(start, end):
    """按期号范围查询 (含两端), 返回升序列表。供弹窗显示最大间隔区间用。"""
    if start > end:
        start, end = end, start
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT draw_nbr, draw_date, draw_time, c1, c2, c3, draw_num, "
            "size_type, parity_type, combination_type "
            "FROM draws WHERE draw_nbr >= ? AND draw_nbr <= ? "
            "ORDER BY draw_nbr ASC", (start, end)
        ).fetchall()
        return [{
            "draw_nbr": r[0], "draw_date": r[1], "draw_time": r[2],
            "c1": r[3], "c2": r[4], "c3": r[5], "draw_num": r[6],
            "size_type": r[7], "parity_type": r[8], "combination_type": r[9]
        } for r in rows]
    finally:
        conn.close()


def find_period_page(period, size=30):
    """根据期号计算在分页列表中所在的页码 (按期号倒序, 从1开始)。"""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM draws WHERE draw_nbr >= ?", (period,)
        ).fetchone()
        if not row or row[0] == 0:
            return None
        later_count = row[0] - 1
        page = (later_count // size) + 1
        total = conn.execute("SELECT COUNT(*) FROM draws").fetchone()[0]
        pages = (total + size - 1) // size
        return {"page": page, "total": total, "pages": pages}
    finally:
        conn.close()


def get_trend(limit=100):
    """获取最近 limit 期走势数据 (按期号升序返回)。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT draw_nbr, draw_num, size_type, parity_type "
            "FROM draws ORDER BY draw_nbr DESC LIMIT ?", (limit,)
        ).fetchall()
        rows.reverse()
        return [{
            "draw_nbr": r[0], "draw_num": r[1],
            "size_type": r[2], "parity_type": r[3],
        } for r in rows]
    finally:
        conn.close()


def get_unopened_by_date_range(days=None, start_date=None, end_date=None):
    """按日期范围统计各形态的最大/平均间隔, 并返回最大间隔的期号 (用于跳转查看历史数据)。

    间隔 = (下一期时间戳 - 上一期时间戳) / 210秒
    即使数据库有缺失期, 也按真实时间计算 (避免 138 跨日假象)。

    参数:
      days: 最近 N 天 (从最新一天向前推); 与 start_date/end_date 互斥
      start_date / end_date: 自定义日期范围 'YYYY-MM-DD'

    返回: {
      "days": 7, "start_date": "2026-08-07", "end_date": "2026-08-13",
      "items": [
        {"type": "大", "count": 365, "avg": 4.0, "max": 41,
         "max_start": 3465000, "max_end": 3465040,
         "max_date": "2026-08-12", "max_time": "12:30:00"},
        ...
      ]
    }
    """
    from datetime import datetime

    def to_ts(date_str, time_str):
        # 把 "YYYY-MM-DD HH:MM:SS" 转成 unix 时间戳
        return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S").timestamp()

    conn = _conn()
    try:
        # 计算日期范围
        latest_row = conn.execute(
            "SELECT draw_date FROM draws ORDER BY draw_nbr DESC LIMIT 1"
        ).fetchone()
        if not latest_row:
            return {"days": 0, "start_date": "", "end_date": "", "items": []}

        if start_date is None or end_date is None:
            end_date = latest_row[0]
            if days is None or days <= 0:
                start_date = "1970-01-01"
                days = 0
            else:
                from datetime import datetime, timedelta
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                start_dt = end_dt - timedelta(days=days - 1)
                start_date = start_dt.strftime("%Y-%m-%d")

        # 查范围内的期数
        rows = conn.execute(
            "SELECT draw_nbr, draw_date, draw_time, size_type, parity_type, draw_num "
            "FROM draws WHERE draw_date >= ? AND draw_date <= ? "
            "ORDER BY draw_nbr ASC", (start_date, end_date)
        ).fetchall()

        def is_maint(date_str, time_str):
            hm = time_str.split(":")
            minutes = int(hm[0]) * 60 + int(hm[1])
            return (19 * 60 <= minutes < 19 * 60 + 33) or (20 * 60 <= minutes < 20 * 60 + 33)

        # 收集每个形态的间隔 + 最大间隔对应的期号区间
        # 仅当数据库里 nbr1+1, nbr1+2, ..., nbr2-1 全部存在 (期号连续) 时,
        # 才把 nbr2 - nbr1 - 1 计入"连续没出". 缺期 (nbr 跳号) 时跳过.
        intervals = {t: [] for t in
                     ["大", "小", "单", "双", "大单", "大双", "小单", "小双"]}
        max_info = {t: {"max": 0, "start": 0, "end": 0,
                         "date": "", "time": ""}
                    for t in intervals.keys()}
        last_seen = {}  # type -> {"nbr": nbr, "rows": 距上一同形态的行数}

        row_count = 0
        for r in rows:
            nbr, date, time, sz, pa, sm = r
            row_count += 1
            if is_maint(date, time):
                last_seen = {}
                continue
            # 单形态
            for t in [sz, pa]:
                if t in last_seen:
                    prev_nbr = last_seen[t]["nbr"]
                    rows_between = row_count - last_seen[t]["rows"] - 1
                    nbr_diff = nbr - prev_nbr - 1
                    # 仅在期号连续 (数据库不缺期) 时才计入
                    if rows_between == nbr_diff and nbr_diff > 0:
                        intervals[t].append(nbr_diff)
                        if nbr_diff > max_info[t]["max"]:
                            max_info[t] = {
                                "max": nbr_diff, "start": prev_nbr, "end": nbr,
                                "date": date, "time": time
                            }
                last_seen[t] = {"nbr": nbr, "rows": row_count}
            # 组合
            combo = f"{sz}{pa}"
            if combo in last_seen:
                prev_nbr = last_seen[combo]["nbr"]
                rows_between = row_count - last_seen[combo]["rows"] - 1
                nbr_diff = nbr - prev_nbr - 1
                if rows_between == nbr_diff and nbr_diff > 0:
                    intervals[combo].append(nbr_diff)
                    if nbr_diff > max_info[combo]["max"]:
                        max_info[combo] = {
                            "max": nbr_diff, "start": prev_nbr, "end": nbr,
                            "date": date, "time": time
                        }
            last_seen[combo] = {"nbr": nbr, "rows": row_count}

        items = []
        for t in ["大", "小", "单", "双", "大单", "大双", "小单", "小双"]:
            g = intervals[t]
            mi = max_info[t]
            items.append({
                "type": t,
                "count": len(g),
                "avg": round(sum(g) / len(g), 2) if g else 0,
                "max": mi["max"],
                "max_start": mi["start"],
                "max_end": mi["end"],
                "max_date": mi["date"],
                "max_time": mi["time"]
            })
        return {"days": days or 0,
                "start_date": start_date, "end_date": end_date, "items": items}
    finally:
        conn.close()


def get_unopened_stats():
    """计算大小单双及组合形态的未开期数。"""
    conn = _conn()
    try:
        types = ["大", "小", "单", "双", "大单", "大双", "小单", "小双"]
        result = {}
        for t in types:
            if len(t) == 1:
                row = conn.execute(
                    "SELECT draw_nbr FROM draws WHERE size_type=? OR parity_type=? "
                    "ORDER BY draw_nbr DESC LIMIT 1", (t, t)
                ).fetchone()
            else:
                sz, pa = t[0], t[1]
                row = conn.execute(
                    "SELECT draw_nbr FROM draws WHERE size_type=? AND parity_type=? "
                    "ORDER BY draw_nbr DESC LIMIT 1", (sz, pa)
                ).fetchone()
            latest = conn.execute("SELECT MAX(draw_nbr) FROM draws").fetchone()[0]
            if row and latest:
                result[t] = latest - row[0]
            else:
                result[t] = 0
        return result
    finally:
        conn.close()


def get_unopened_stats_v2():
    """增强版未开期数: 包含当前未开、历史最大、平均、中位数、P95/P99、状态评估。

    返回结构:
      {
        "latest_nbr": int,
        "items": [
          {"type": "大", "current": 5, "max": 138, "avg": 2.05, "med": 1,
           "p95": 5, "p99": 8, "status": "normal|warm|hot|extreme",
           "ratio": 0.62},  # 当前/平均, 越大越异常
          ...
        ]
      }
    """
    conn = _conn()
    try:
        latest = conn.execute("SELECT MAX(draw_nbr) FROM draws").fetchone()[0] or 0
        # 排除维护时段 (19:00-19:33 / 20:00-20:33) 的连续跨日号段
        rows = conn.execute(
            "SELECT draw_nbr, draw_date, draw_time, size_type, parity_type, draw_num "
            "FROM draws ORDER BY draw_nbr ASC"
        ).fetchall()
        if not rows:
            return {"latest_nbr": latest, "items": []}

        # 维护时段判定 (北京时间, 与 time_sync.py 一致)
        def is_maint(date_str, time_str):
            hm = time_str.split(":")
            minutes = int(hm[0]) * 60 + int(hm[1])
            return (19 * 60 <= minutes < 19 * 60 + 33) or (20 * 60 <= minutes < 20 * 60 + 33)

        # 收集历史间隔 (维护后重置基准)
        gaps = {"大": [], "小": [], "单": [], "双": [],
                "大单": [], "大双": [], "小单": [], "小双": []}
        last_seen = {}
        for r in rows:
            nbr, date, time, sz, pa, sm = r
            if is_maint(date, time):
                # 维护时段: 重置所有 last_seen
                last_seen = {}
                continue
            for t in [sz, pa]:
                if t in last_seen:
                    gaps[t].append(nbr - last_seen[t])
                last_seen[t] = nbr
            combo = f"{sz}{pa}"
            if combo in last_seen:
                gaps[combo].append(nbr - last_seen[combo])
            last_seen[combo] = nbr

        # 计算当前未开期数
        def cur_unopened(t):
            sz, pa = t[0], t[1] if len(t) > 1 else None
            if pa is None:
                row = conn.execute(
                    "SELECT draw_nbr FROM draws WHERE size_type=? OR parity_type=? "
                    "ORDER BY draw_nbr DESC LIMIT 1", (t, t)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT draw_nbr FROM draws WHERE size_type=? AND parity_type=? "
                    "ORDER BY draw_nbr DESC LIMIT 1", (sz, pa)
                ).fetchone()
            return latest - row[0] if row else 0

        # 统计
        items = []
        for t in ["大", "小", "单", "双", "大单", "大双", "小单", "小双"]:
            g = sorted(gaps[t])
            n = len(g)
            current = cur_unopened(t)
            if n == 0:
                items.append({"type": t, "current": current, "max": 0, "avg": 0,
                              "med": 0, "p95": 0, "p99": 0, "ratio": 0, "status": "normal"})
                continue
            mx = max(g)
            avg = sum(g) / n
            med = g[n // 2]
            p95 = g[int(n * 0.95)]
            p99 = g[min(n - 1, int(n * 0.99))]
            ratio = current / avg if avg > 0 else 0
            # 状态评估: 当前/平均
            if ratio >= 5:
                status = "extreme"  # 极冷
            elif ratio >= 3:
                status = "hot"      # 偏冷
            elif ratio >= 1.5:
                status = "warm"     # 偏热
            else:
                status = "normal"   # 正常
            items.append({
                "type": t, "current": current, "max": mx,
                "avg": round(avg, 2), "med": med,
                "p95": p95, "p99": p99,
                "ratio": round(ratio, 2), "status": status
            })
        return {"latest_nbr": latest, "items": items}
    finally:
        conn.close()


def get_sum_unopened_stats():
    """计算每个和值 (0-27) 的未出期数 + 赔率, 按赔率对分组返回。"""
    conn = _conn()
    try:
        latest = conn.execute("SELECT MAX(draw_nbr) FROM draws").fetchone()[0] or 0
        last_seen = {}
        for s in range(28):
            row = conn.execute(
                "SELECT draw_nbr FROM draws WHERE draw_num=? ORDER BY draw_nbr DESC LIMIT 1",
                (s,)
            ).fetchone()
            last_seen[s] = row[0] if row else 0
        groups = []
        seen = set()
        for s in range(14):
            pair = (s, 27 - s)
            if pair in seen:
                continue
            seen.add(pair)
            groups.append({
                "sums": list(pair),
                "odds": SUM_ODDS[s],
                "unopened": [
                    latest - last_seen[s] if last_seen[s] else -1,
                    latest - last_seen[27 - s] if last_seen[27 - s] else -1,
                ],
            })
        return {"latest_nbr": latest, "groups": groups}
    finally:
        conn.close()


def get_draws_json():
    """读取全部开奖数据并打包为回测前端需要的 JSON 字符串。

    依赖 backtest_e9.py 的策略常量 (LADDER/阈值/赔率), 在此函数内延迟 import,
    避免无回测需求时也强制加载 backtest_e9。
    """
    from backtest_e9 import (
        LADDER, BIG_THRESHOLD, COMMISSION_SUMS, COMMISSION_RATE,
        HIGH_BET_THRESHOLD, HIGH_BET_RATE,
    )
    conn = _conn()
    try:
        cur = conn.execute(
            "SELECT draw_nbr, draw_date, draw_time, c1, c2, c3 "
            "FROM draws ORDER BY draw_nbr ASC")
        rows = [[r[0], r[1], r[2][:8], r[3], r[4], r[5]] for r in cur.fetchall()]
    finally:
        conn.close()
    payload = {
        "draws": rows,
        "ladder": LADDER,
        "C": {
            "BIG": BIG_THRESHOLD,
            "COMM_SUMS": list(COMMISSION_SUMS),
            "COMM_RATE": COMMISSION_RATE,
            "HIGH_BET": HIGH_BET_THRESHOLD,
            "HIGH_RATE": HIGH_BET_RATE,
        },
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
