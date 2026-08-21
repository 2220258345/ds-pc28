# -*- coding: utf-8 -*-
"""SQLAlchemy 存储实现。

一个实现同时支持 SQLite / PostgreSQL / MySQL，方言差异集中在建表和 upsert 两处。
上层 (core.db 门面、collector、backtest) 只依赖 Storage 接口，不再直接触碰 DBAPI。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import (
    BigInteger,
    Column,
    Index,
    MetaData,
    SmallInteger,
    String,
    Table,
    create_engine,
    event,
    text,
)

from .base import COLUMNS, SUM_ODDS, Storage, VerifyResult


DRAW_SELECT = (
    "draw_nbr, draw_date, draw_time, c1, c2, c3, draw_num, "
    "size_type, parity_type, combination_type"
)


def _sum_probability_table():
    """3d10 和值真实概率：sum 的组合数 / 1000。"""
    from collections import defaultdict

    ways = {0: 1}
    for _ in range(3):
        nxt = defaultdict(int)
        for s, cnt in ways.items():
            for d in range(10):
                nxt[s + d] += cnt
        ways = nxt
    return {s: ways.get(s, 0) / 1000.0 for s in range(28)}


SUM_PROB = _sum_probability_table()


def _is_maintenance(date_str: str, time_str: str) -> bool:
    """维护时段判定 (与历史 db.py 行为保持一致)。"""
    parts = time_str.split(":")
    minutes = int(parts[0]) * 60 + int(parts[1])
    return (19 * 60 <= minutes < 19 * 60 + 33) or (20 * 60 <= minutes < 20 * 60 + 33)


class SQLAlchemyStorage(Storage):
    def __init__(self, config):
        self.config = config
        url = config.sqlalchemy_url()

        engine_kwargs: dict = {}
        connect_args: dict = {}
        if config.backend == "sqlite":
            from sqlalchemy.pool import NullPool

            connect_args = {"check_same_thread": False, "timeout": 30}
            engine_kwargs["poolclass"] = NullPool
        else:
            engine_kwargs["pool_pre_ping"] = True

        self.engine = create_engine(url, connect_args=connect_args, **engine_kwargs)

        if config.backend == "sqlite":
            @event.listens_for(self.engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()

        self.metadata = MetaData()
        self.draws = Table(
            "draws",
            self.metadata,
            Column("draw_nbr", BigInteger, primary_key=True),
            Column("draw_date", String(10), nullable=False),
            Column("draw_time", String(8), nullable=False),
            Column("c1", SmallInteger, nullable=False),
            Column("c2", SmallInteger, nullable=False),
            Column("c3", SmallInteger, nullable=False),
            Column("draw_num", SmallInteger, nullable=False),
            Column("size_type", String(8), nullable=False),
            Column("parity_type", String(8), nullable=False),
            Column("combination_type", String(8), nullable=False),
            Index("idx_draws_date", "draw_date"),
        )
        self.migrations = Table(
            "schema_migrations",
            self.metadata,
            Column("version", BigInteger, primary_key=True),
        )
        self.ensure_schema()

    # ---- 基础执行工具 ----
    def _fetchall(self, sql: str, params: dict | None = None):
        with self.engine.connect() as conn:
            return [tuple(r) for r in conn.execute(text(sql), params or {})]

    def _scalar(self, sql: str, params: dict | None = None):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params or {}).scalar()

    @staticmethod
    def _scalar_conn(conn, sql: str, params: dict | None = None):
        return conn.execute(text(sql), params or {}).scalar()

    def ensure_schema(self) -> None:
        self.metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            has = conn.execute(
                text("SELECT 1 FROM schema_migrations WHERE version = :v"),
                {"v": 1},
            ).first()
            if not has:
                conn.execute(self.migrations.insert().values(version=1))

    @staticmethod
    def _draw_dict(row) -> dict:
        return {
            "draw_nbr": row[0],
            "draw_date": row[1],
            "draw_time": row[2],
            "c1": row[3],
            "c2": row[4],
            "c3": row[5],
            "draw_num": row[6],
            "size_type": row[7],
            "parity_type": row[8],
            "combination_type": row[9],
        }

    # ---- 读接口 ----
    def rows_info(self) -> tuple[int, int | None, str | None]:
        n = self._scalar("SELECT COUNT(*) FROM draws") or 0
        row = self._fetchall("SELECT MAX(draw_nbr), MAX(draw_date) FROM draws")[0]
        return n, row[0], row[1]

    def latest(self) -> tuple | None:
        rows = self._fetchall(
            f"SELECT {DRAW_SELECT} FROM draws ORDER BY draw_nbr DESC LIMIT 1"
        )
        return rows[0] if rows else None

    def history(self, page: int, size: int) -> dict:
        total = self._scalar("SELECT COUNT(*) FROM draws") or 0
        offset = (page - 1) * size
        rows = self._fetchall(
            f"SELECT {DRAW_SELECT} FROM draws ORDER BY draw_nbr DESC "
            "LIMIT :size OFFSET :offset",
            {"size": size, "offset": offset},
        )
        return {
            "total": total,
            "page": page,
            "size": size,
            "pages": (total + size - 1) // size,
            "list": [self._draw_dict(r) for r in rows],
        }

    def range_rows(self, start: int, end: int) -> list[dict]:
        if start > end:
            start, end = end, start
        rows = self._fetchall(
            f"SELECT {DRAW_SELECT} FROM draws "
            "WHERE draw_nbr >= :s AND draw_nbr <= :e ORDER BY draw_nbr ASC",
            {"s": start, "e": end},
        )
        return [self._draw_dict(r) for r in rows]

    def find_period_page(self, period: int, size: int) -> dict | None:
        later = self._scalar(
            "SELECT COUNT(*) FROM draws WHERE draw_nbr >= :p", {"p": period}
        )
        if not later:
            return None
        later_count = later - 1
        page = later_count // size + 1
        total = self._scalar("SELECT COUNT(*) FROM draws") or 0
        pages = (total + size - 1) // size
        return {"page": page, "total": total, "pages": pages}

    def trend(self, limit: int) -> list[dict]:
        rows = self._fetchall(
            "SELECT draw_nbr, draw_num, size_type, parity_type "
            "FROM draws ORDER BY draw_nbr DESC LIMIT :l",
            {"l": limit},
        )
        rows.reverse()
        return [
            {"draw_nbr": r[0], "draw_num": r[1], "size_type": r[2], "parity_type": r[3]}
            for r in rows
        ]

    def unopened(self) -> dict:
        latest = self._scalar("SELECT MAX(draw_nbr) FROM draws")
        types = ["大", "小", "单", "双", "大单", "大双", "小单", "小双"]
        result = {}
        for t in types:
            if len(t) == 1:
                rows = self._fetchall(
                    "SELECT draw_nbr FROM draws WHERE size_type = :t OR parity_type = :t "
                    "ORDER BY draw_nbr DESC LIMIT 1",
                    {"t": t},
                )
            else:
                sz, pa = t[0], t[1]
                rows = self._fetchall(
                    "SELECT draw_nbr FROM draws WHERE size_type = :s AND parity_type = :p "
                    "ORDER BY draw_nbr DESC LIMIT 1",
                    {"s": sz, "p": pa},
                )
            result[t] = latest - rows[0][0] if rows and latest is not None else 0
        return result

    def unopened_v2(self) -> dict:
        latest = self._scalar("SELECT MAX(draw_nbr) FROM draws") or 0
        rows = self._fetchall(
            "SELECT draw_nbr, draw_date, draw_time, size_type, parity_type, draw_num "
            "FROM draws ORDER BY draw_nbr ASC"
        )
        if not rows:
            return {"latest_nbr": latest, "items": []}

        gaps = {t: [] for t in ["大", "小", "单", "双", "大单", "大双", "小单", "小双"]}
        totals = {t: 0 for t in ["大", "小", "单", "双", "大单", "大双", "小单", "小双"]}
        last_seen = {}
        for nbr, date, time_str, sz, pa, _sm in rows:
            if _is_maintenance(date, time_str):
                last_seen = {}
                continue
            for t in [sz, pa]:
                totals[t] += 1
                if t in last_seen:
                    gaps[t].append(nbr - last_seen[t])
                last_seen[t] = nbr
            combo = f"{sz}{pa}"
            totals[combo] += 1
            if combo in last_seen:
                gaps[combo].append(nbr - last_seen[combo])
            last_seen[combo] = nbr

        def cur_unopened(t: str) -> int:
            sz, pa = t[0], t[1] if len(t) > 1 else None
            if pa is None:
                rows = self._fetchall(
                    "SELECT draw_nbr FROM draws WHERE size_type = :t OR parity_type = :t "
                    "ORDER BY draw_nbr DESC LIMIT 1",
                    {"t": t},
                )
            else:
                rows = self._fetchall(
                    "SELECT draw_nbr FROM draws WHERE size_type = :s AND parity_type = :p "
                    "ORDER BY draw_nbr DESC LIMIT 1",
                    {"s": sz, "p": pa},
                )
            return latest - rows[0][0] if rows else 0

        items = []
        for t in ["大", "小", "单", "双", "大单", "大双", "小单", "小双"]:
            g = sorted(gaps[t])
            n = len(g)
            current = cur_unopened(t)
            if n == 0:
                items.append({
                "type": t, "current": current, "max": 0, "avg": 0,
                "med": 0, "p95": 0, "p99": 0, "ratio": 0, "status": "normal",
                "total": totals[t],
            })
                continue
            mx = max(g)
            avg = sum(g) / n
            med = g[n // 2]
            p95 = g[int(n * 0.95)]
            p99 = g[min(n - 1, int(n * 0.99))]
            ratio = current / avg if avg > 0 else 0
            if ratio >= 5:
                status = "extreme"      # 极冷
            elif ratio >= 3:
                status = "very_cold"    # 冷
            elif ratio >= 1.5:
                status = "cold"         # 偏冷
            else:
                status = "normal"
            items.append({
                "type": t, "current": current, "max": mx,
                "avg": round(avg, 2), "med": med,
                "p95": p95, "p99": p99,
                "ratio": round(ratio, 2), "status": status,
                "total": totals[t],
            })
        return {"latest_nbr": latest, "items": items}

    def unopened_by_date_range(
        self,
        *,
        days: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        latest_row = self._fetchall(
            "SELECT draw_date FROM draws ORDER BY draw_nbr DESC LIMIT 1"
        )
        if not latest_row:
            return {"days": 0, "start_date": "", "end_date": "", "items": []}

        if start_date is None or end_date is None:
            end_date = latest_row[0][0]
            if not days or days <= 0:
                start_date = "1970-01-01"
                days = 0
            else:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                start_dt = end_dt - timedelta(days=days - 1)
                start_date = start_dt.strftime("%Y-%m-%d")

        rows = self._fetchall(
            "SELECT draw_nbr, draw_date, draw_time, size_type, parity_type, draw_num "
            "FROM draws WHERE draw_date >= :s AND draw_date <= :e ORDER BY draw_nbr ASC",
            {"s": start_date, "e": end_date},
        )

        intervals = {t: [] for t in ["大", "小", "单", "双", "大单", "大双", "小单", "小双"]}
        max_info = {t: {"max": 0, "start": 0, "end": 0, "date": "", "time": ""}
                    for t in intervals}
        last_seen = {}
        row_count = 0
        for nbr, date, time_str, sz, pa, _sm in rows:
            row_count += 1
            if _is_maintenance(date, time_str):
                last_seen = {}
                continue
            for t in [sz, pa]:
                if t in last_seen:
                    prev_nbr = last_seen[t]["nbr"]
                    rows_between = row_count - last_seen[t]["rows"] - 1
                    nbr_diff = nbr - prev_nbr - 1
                    if rows_between == nbr_diff and nbr_diff > 0:
                        intervals[t].append({"g": nbr_diff, "s": prev_nbr, "e": nbr, "d": date})
                        if nbr_diff > max_info[t]["max"]:
                            max_info[t] = {
                                "max": nbr_diff, "start": prev_nbr, "end": nbr,
                                "date": date, "time": time_str,
                            }
                last_seen[t] = {"nbr": nbr, "rows": row_count}
            combo = f"{sz}{pa}"
            if combo in last_seen:
                prev_nbr = last_seen[combo]["nbr"]
                rows_between = row_count - last_seen[combo]["rows"] - 1
                nbr_diff = nbr - prev_nbr - 1
                if rows_between == nbr_diff and nbr_diff > 0:
                    intervals[combo].append({"g": nbr_diff, "s": prev_nbr, "e": nbr, "d": date})
                    if nbr_diff > max_info[combo]["max"]:
                        max_info[combo] = {
                            "max": nbr_diff, "start": prev_nbr, "end": nbr,
                            "date": date, "time": time_str,
                        }
            last_seen[combo] = {"nbr": nbr, "rows": row_count}

        items = []
        for t in ["大", "小", "单", "双", "大单", "大双", "小单", "小双"]:
            g = intervals[t]
            mi = max_info[t]
            items.append({
                "type": t,
                "count": len(g),
                "avg": round(sum(x["g"] for x in g) / len(g), 2) if g else 0,
                "max": mi["max"],
                "max_start": mi["start"],
                "max_end": mi["end"],
                "max_date": mi["date"],
                "max_time": mi["time"],
                "intervals": list(g),
            })
        return {
            "days": days or 0,
            "start_date": start_date,
            "end_date": end_date,
            "items": items,
        }

    def sum_unopened(self) -> dict:
        latest = self._scalar("SELECT MAX(draw_nbr) FROM draws") or 0
        last_seen = {}
        for s in range(28):
            rows = self._fetchall(
                "SELECT draw_nbr FROM draws WHERE draw_num = :s "
                "ORDER BY draw_nbr DESC LIMIT 1",
                {"s": s},
            )
            last_seen[s] = rows[0][0] if rows else 0

        groups = []
        seen = set()
        for s in range(14):
            pair = (s, 27 - s)
            if pair in seen:
                continue
            seen.add(pair)
            prob = SUM_PROB[s]
            groups.append({
                "sums": list(pair),
                "odds": SUM_ODDS[s],
                "probability": prob,
                "theoretical_period": round(1 / prob, 1) if prob else None,
                "unopened": [
                    latest - last_seen[s] if last_seen[s] else -1,
                    latest - last_seen[27 - s] if last_seen[27 - s] else -1,
                ],
            })
        return {"latest_nbr": latest, "groups": groups}

    def all_draws(self) -> list[tuple]:
        return self._fetchall(
            "SELECT draw_nbr, draw_date, draw_time, c1, c2, c3, draw_num "
            "FROM draws ORDER BY draw_nbr ASC"
        )

    def export_rows(self) -> list[tuple]:
        """导出完整 10 列数据 (按期号倒序)，供 CSV 备份/校验使用。"""
        return self._fetchall(f"SELECT {DRAW_SELECT} FROM draws ORDER BY draw_nbr DESC")

    def load_draws(self, filter_date: str | None = None) -> list[tuple]:
        if filter_date:
            return self._fetchall(
                "SELECT draw_nbr, draw_date, draw_time, c1, c2, c3, draw_num "
                "FROM draws WHERE draw_date = :d ORDER BY draw_nbr ASC",
                {"d": filter_date},
            )
        return self.all_draws()

    # ---- 写接口 ----
    def insert(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        with self.engine.begin() as conn:
            before = self._scalar_conn(conn, "SELECT COUNT(*) FROM draws") or 0
            conn.execute(self._upsert_stmt(), rows)
            after = self._scalar_conn(conn, "SELECT COUNT(*) FROM draws") or 0
        return after - before

    def _upsert_stmt(self):
        cols = [c for c in COLUMNS if c != "draw_nbr"]
        if self.config.backend == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            stmt = sqlite_insert(self.draws)
            set_ = {c: getattr(stmt.excluded, c) for c in cols}
            return stmt.on_conflict_do_update(index_elements=["draw_nbr"], set_=set_)

        if self.config.backend == "postgres":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(self.draws)
            set_ = {c: getattr(stmt.excluded, c) for c in cols}
            return stmt.on_conflict_do_update(index_elements=["draw_nbr"], set_=set_)

        if self.config.backend == "mysql":
            from sqlalchemy.dialects.mysql import insert as mysql_insert

            stmt = mysql_insert(self.draws)
            upd = {c: getattr(stmt.inserted, c) for c in cols}
            return stmt.on_duplicate_key_update(**upd)

        raise ValueError(f"不支持的数据库后端: {self.config.backend}")

    def verify(self) -> VerifyResult:
        n = self._scalar("SELECT COUNT(*) FROM draws") or 0
        dup = self._scalar(
            "SELECT COUNT(*) FROM (SELECT draw_nbr FROM draws GROUP BY draw_nbr "
            "HAVING COUNT(*) > 1) AS t"
        ) or 0
        bad = self._scalar(
            "SELECT COUNT(*) FROM draws WHERE c1 + c2 + c3 != draw_num"
        ) or 0
        rng = self._fetchall(
            "SELECT MIN(draw_nbr), MAX(draw_nbr), MAX(draw_date) FROM draws"
        )[0]

        # 期号连续性检测: 用自连接找相邻期号之间的缺口
        gaps_rows = self._fetchall(
            "SELECT t.draw_nbr + 1, nxt.draw_nbr - 1 "
            "FROM draws t "
            "JOIN draws nxt ON nxt.draw_nbr = ("
            "    SELECT MIN(x.draw_nbr) FROM draws x WHERE x.draw_nbr > t.draw_nbr"
            ") "
            "WHERE nxt.draw_nbr - t.draw_nbr > 1 "
            "ORDER BY t.draw_nbr"
        )
        gaps = [(int(g[0]), int(g[1])) for g in gaps_rows]
        missing = sum(end - start + 1 for start, end in gaps)

        return VerifyResult(
            ok=(dup == 0 and bad == 0 and missing == 0),
            rows=n,
            duplicates=dup,
            bad_sum=bad,
            min_nbr=rng[0],
            max_nbr=rng[1],
            max_date=rng[2],
            missing=missing,
            gaps=gaps,
        )
