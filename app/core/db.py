# -*- coding: utf-8 -*-
"""数据库门面 — 保持历史 API 不变，内部委托给 storage 存储层。

server.py / api_server.py / collector.py / backtest_e9.py 都通过这里访问数据，
实际后端由 storage.get_storage() 按配置选择 (SQLite / PostgreSQL / MySQL)。
"""
from __future__ import annotations

import json

from storage import SUM_ODDS, get_storage


def _store():
    return get_storage()


def get_db_rows():
    """返回 (总行数, 最大期号, 最大日期)。"""
    return _store().rows_info()


def get_latest_draw():
    """获取最新一期 (返回 tuple 或 None)。"""
    return _store().latest()


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
    return _store().history(page, size)


def get_range(start, end):
    """按期号范围查询 (含两端)，返回升序列表。"""
    return _store().range_rows(start, end)


def find_period_page(period, size=30):
    """根据期号计算在分页列表中所在的页码。"""
    return _store().find_period_page(period, size)


def get_trend(limit=100):
    """获取最近 limit 期走势数据 (按期号升序返回)。"""
    return _store().trend(limit)


def get_unopened_stats():
    """计算大小单双及组合形态的未开期数。"""
    return _store().unopened()


def get_unopened_stats_v2():
    """增强版未开期数统计。"""
    return _store().unopened_v2()


def get_unopened_by_date_range(days=None, start_date=None, end_date=None):
    """按日期范围统计各形态的最大/平均间隔。"""
    return _store().unopened_by_date_range(
        days=days, start_date=start_date, end_date=end_date
    )


def get_sum_unopened_stats():
    """计算每个和值 (0-27) 的未出期数 + 赔率。"""
    return _store().sum_unopened()


def get_draws_json():
    """读取全部开奖数据并打包为回测前端需要的 JSON 字符串。"""
    from backtest_e9 import (
        LADDER, BIG_THRESHOLD, COMMISSION_SUMS, COMMISSION_RATE,
        HIGH_BET_THRESHOLD, HIGH_BET_RATE,
    )
    rows = [
        [r[0], r[1], r[2][:8], r[3], r[4], r[5]]
        for r in _store().all_draws()
    ]
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


def load_draws(filter_date=None):
    """读取开奖数据，返回 [(期号, 日期, 时间, c1, c2, c3, 和值), ...] 旧->新。"""
    return _store().load_draws(filter_date)


def insert_rows(rows):
    """写入/更新开奖数据，返回新增行数。"""
    return _store().insert(rows)


def verify():
    """校验数据库完整性，返回是否通过。"""
    r = _store().verify()
    print(
        f"校验: 行数 {r.rows:,}  重复 {r.duplicates}  和值错误 {r.bad_sum}  "
        f"缺失期 {r.missing}  "
        f"期号 {r.min_nbr}~{r.max_nbr}  最新 {r.max_date}"
    )
    if r.gaps:
        for s, e in r.gaps:
            print(f"  缺口: {s} ~ {e}  (共 {e - s + 1} 期)")
    print("结果:", "通过" if r.ok else "失败")
    return r.ok
