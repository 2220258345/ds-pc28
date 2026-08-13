# -*- coding: utf-8 -*-
"""SQLite 存储层回归测试 (可独立运行，不触碰生产数据库)。"""
import os
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from config import DBConfig
from storage.store import SQLAlchemyStorage


SAMPLE = [
    {
        "draw_nbr": 100, "draw_date": "2026-01-01", "draw_time": "00:00:00",
        "c1": 1, "c2": 2, "c3": 3, "draw_num": 6,
        "size_type": "小", "parity_type": "双", "combination_type": "小双",
    },
    {
        "draw_nbr": 101, "draw_date": "2026-01-01", "draw_time": "00:03:30",
        "c1": 4, "c2": 5, "c3": 6, "draw_num": 15,
        "size_type": "大", "parity_type": "单", "combination_type": "大单",
    },
    {
        "draw_nbr": 102, "draw_date": "2026-01-01", "draw_time": "00:07:00",
        "c1": 7, "c2": 8, "c3": 9, "draw_num": 24,
        "size_type": "大", "parity_type": "双", "combination_type": "大双",
    },
]


class SQLiteStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = SQLAlchemyStorage(DBConfig(backend="sqlite", path=self.tmp.name))

    def tearDown(self):
        self.store.engine.dispose()
        base = self.tmp.name
        for p in (base, base + "-wal", base + "-shm"):
            if os.path.exists(p):
                os.unlink(p)

    def test_insert_and_verify(self):
        self.store.insert(SAMPLE)
        v = self.store.verify()
        self.assertTrue(v.ok)
        self.assertEqual(v.rows, 3)
        self.assertEqual(v.min_nbr, 100)
        self.assertEqual(v.max_nbr, 102)

        n, mx, _ = self.store.rows_info()
        self.assertEqual((n, mx), (3, 102))

    def test_upsert_replaces_same_primary_key(self):
        self.store.insert(SAMPLE)
        updated = dict(
            SAMPLE[0],
            c1=9, c2=9, c3=9, draw_num=27,
            size_type="大", parity_type="单", combination_type="大单",
        )
        self.store.insert([updated])

        v = self.store.verify()
        self.assertTrue(v.ok)
        self.assertEqual(v.rows, 3)

        rows = {r[0]: r for r in self.store.load_draws()}
        self.assertEqual(rows[100][3], 9)
        self.assertEqual(rows[100][6], 27)

    def test_read_apis(self):
        self.store.insert(SAMPLE)

        h = self.store.history(1, 2)
        self.assertEqual(h["total"], 3)
        self.assertEqual(len(h["list"]), 2)
        self.assertEqual(h["list"][0]["draw_nbr"], 102)

        self.assertEqual(self.store.range_rows(100, 101)[0]["draw_nbr"], 100)
        self.assertEqual(self.store.trend(2)[-1]["draw_nbr"], 102)
        self.assertEqual(self.store.find_period_page(101, 2)["page"], 1)

        self.assertEqual(len(self.store.unopened()), 8)
        self.assertEqual(self.store.unopened_v2()["latest_nbr"], 102)
        self.assertEqual(len(self.store.sum_unopened()["groups"]), 14)
        self.assertEqual(len(self.store.export_rows()), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
