# -*- coding: utf-8 -*-
"""存储层公共定义：字段、抽水赔率表、抽象接口。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


COLUMNS = [
    "draw_nbr", "draw_date", "draw_time", "c1", "c2", "c3", "draw_num",
    "size_type", "parity_type", "combination_type",
]


# 特码 (和值) 赔率表，与 db.py 历史实现保持一致。
SUM_ODDS = {
    0: 920, 27: 920,
    1: 300, 26: 300,
    2: 150, 25: 150,
    3: 90, 24: 90,
    4: 60, 23: 60,
    5: 38, 22: 38,
    6: 30, 21: 30,
    7: 24, 20: 24,
    8: 19, 19: 19,
    9: 16, 18: 16,
    10: 15, 17: 15,
    11: 14, 16: 14,
    12: 13.2, 15: 13.2,
    13: 13.2, 14: 13.2,
}


@dataclass
class VerifyResult:
    ok: bool
    rows: int
    duplicates: int
    bad_sum: int
    min_nbr: int | None
    max_nbr: int | None
    max_date: str | None


class Storage(ABC):
    """所有存储后端的统一接口。"""

    @abstractmethod
    def ensure_schema(self) -> None: ...

    @abstractmethod
    def rows_info(self) -> tuple[int, int | None, str | None]: ...

    @abstractmethod
    def latest(self) -> tuple | None: ...

    @abstractmethod
    def history(self, page: int, size: int) -> dict: ...

    @abstractmethod
    def range_rows(self, start: int, end: int) -> list[dict]: ...

    @abstractmethod
    def find_period_page(self, period: int, size: int) -> dict | None: ...

    @abstractmethod
    def trend(self, limit: int) -> list[dict]: ...

    @abstractmethod
    def unopened(self) -> dict: ...

    @abstractmethod
    def unopened_v2(self) -> dict: ...

    @abstractmethod
    def unopened_by_date_range(
        self,
        *,
        days: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict: ...

    @abstractmethod
    def sum_unopened(self) -> dict: ...

    @abstractmethod
    def all_draws(self) -> list[tuple]: ...

    @abstractmethod
    def export_rows(self) -> list[tuple]: ...

    @abstractmethod
    def load_draws(self, filter_date: str | None = None) -> list[tuple]: ...

    @abstractmethod
    def insert(self, rows: list[dict]) -> int: ...

    @abstractmethod
    def verify(self) -> VerifyResult: ...
