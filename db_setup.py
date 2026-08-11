# -*- coding: utf-8 -*-
"""
PC28 历史开奖数据存储: CSV -> SQLite 迁移 / 校验 / 导出
============================================================

用法:
  python db_setup.py            # 从 CSV 导入/刷新数据库 (CSV 存在时)
  python db_setup.py --verify   # 仅校验数据库完整性
  python db_setup.py --export   # 数据库导出为 CSV

数据库: pc28_history.db, 表 draws:
  draw_nbr        期号 (主键)
  draw_date       日期 YYYY-MM-DD
  draw_time       时间 HH:MM:SS
  c1/c2/c3        三个开奖数字
  draw_num        和值
  size_type       大小 (大/小)
  parity_type     单双 (单/双)
  combination_type 组合 (大双/小单/...)
"""
import argparse
import csv
import hashlib
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(BASE, "pc28_history_29999.csv")
DB_PATH = os.path.join(BASE, "pc28_history.db")

SCHEMA = """
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
"""

COLUMNS = ["draw_nbr", "draw_date", "draw_time", "c1", "c2", "c3", "draw_num",
           "size_type", "parity_type", "combination_type"]


def connect():
    conn = sqlite3.connect(DB_PATH)
    return conn


def read_csv_rows():
    """读取 CSV, 返回与 COLUMNS 一致的字典列表 (新->旧)。"""
    rows = []
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
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


def migrate():
    if not os.path.exists(CSV_PATH):
        print(f"CSV 不存在: {CSV_PATH} (已使用数据库存储, 无需迁移)")
        return False
    rows = read_csv_rows()
    print(f"CSV 读取: {len(rows)} 期")
    conn = connect()
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT OR REPLACE INTO draws (" + ", ".join(COLUMNS) + ") "
        "VALUES (" + ", ".join("?" for _ in COLUMNS) + ")",
        [tuple(r[c] for c in COLUMNS) for r in rows],
    )
    conn.commit()
    conn.close()
    print(f"已写入数据库: {DB_PATH}")
    return True


def verify():
    """校验数据库: 数量、主键唯一、期号连续、和值正确、CSV 一致性。"""
    conn = connect()
    n = conn.execute("SELECT COUNT(*) FROM draws").fetchone()[0]
    dup = conn.execute("SELECT COUNT(*) FROM (SELECT draw_nbr FROM draws GROUP BY draw_nbr HAVING COUNT(*)>1)").fetchone()[0]
    bad_sum = conn.execute(
        "SELECT COUNT(*) FROM draws WHERE c1+c2+c3 != draw_num").fetchone()[0]
    gaps = conn.execute("""
        SELECT COUNT(*) FROM (
            SELECT draw_nbr, draw_nbr - LAG(draw_nbr) OVER (ORDER BY draw_nbr) AS d
            FROM draws
        ) WHERE d != 1
    """).fetchone()[0]
    rng = conn.execute("SELECT MIN(draw_nbr), MAX(draw_nbr), MIN(draw_date), MAX(draw_date) FROM draws").fetchone()
    conn.close()
    print(f"行数: {n:,}  重复主键: {dup}  和值错误: {bad_sum}  期号缺口: {gaps}")
    print(f"期号范围: {rng[0]} ~ {rng[1]}  日期: {rng[2]} ~ {rng[3]}")
    ok = dup == 0 and bad_sum == 0
    print("数据库校验:", "通过" if ok else "失败")
    return ok


def db_checksum():
    """数据库全部行的 md5 (用于与 CSV 对比)。"""
    conn = connect()
    rows = conn.execute(
        "SELECT " + ", ".join(COLUMNS) + " FROM draws ORDER BY draw_nbr DESC").fetchall()
    conn.close()
    return rows_checksum(rows)


def rows_checksum(rows):
    h = hashlib.md5()
    for r in rows:
        h.update(("|".join(str(x) for x in r) + "\n").encode("utf-8"))
    return h.hexdigest()


def compare_csv_db():
    if not os.path.exists(CSV_PATH):
        print("CSV 不存在, 跳过对比")
        return
    csv_rows = read_csv_rows()
    csv_hash = rows_checksum([tuple(r[c] for c in COLUMNS) for r in csv_rows])
    db_hash = db_checksum()
    print(f"CSV md5: {csv_hash}")
    print(f"DB  md5: {db_hash}")
    print("CSV 与数据库完全一致" if csv_hash == db_hash else "!! CSV 与数据库不一致")


def export_csv(out_path=None):
    out_path = out_path or CSV_PATH
    conn = connect()
    rows = conn.execute(
        "SELECT draw_nbr, draw_date, draw_time, c1, c2, c3, draw_num, "
        "size_type, parity_type, combination_type FROM draws ORDER BY draw_nbr DESC").fetchall()
    conn.close()
    with open(out_path, "w", encoding="utf-8-sig", newline="\n") as f:
        w = csv.writer(f)
        w.writerow(["draw_nbr", "draw_date", "draw_time", "draw_number", "draw_num",
                    "size_type", "parity_type", "combination_type"])
        for r in rows:
            w.writerow([r[0], r[1], r[2], f"{r[3]}+{r[4]}+{r[5]}", r[6], r[7], r[8], r[9]])
    print(f"已导出: {out_path} ({len(rows)} 期)")


def main():
    parser = argparse.ArgumentParser(description="PC28 数据存储: CSV -> SQLite")
    parser.add_argument("--verify", action="store_true", help="仅校验数据库")
    parser.add_argument("--export", action="store_true", help="数据库导出为 CSV")
    args = parser.parse_args()

    if args.export:
        export_csv()
        return
    if not args.verify:
        migrate()
    verify()
    compare_csv_db()


if __name__ == "__main__":
    main()
