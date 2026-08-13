# -*- coding: utf-8 -*-
"""把 SQLite 数据迁移到当前配置的目标后端 (PostgreSQL/MySQL/SQLite)。

用法:
  python app/migrate_db.py --source-path /path/to/pc28_history.db

目标后端由环境变量 (DB_BACKEND / DB_URI / DB_HOST ...) 决定，与 server.py 一致。
源文件使用 sqlite3 只读模式打开，不会修改源库。
"""
import argparse
import sqlite3

from storage import COLUMNS, get_storage


def read_source(path: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT draw_nbr, draw_date, draw_time, c1, c2, c3, draw_num, "
            "size_type, parity_type, combination_type FROM draws ORDER BY draw_nbr ASC"
        ).fetchall()
    finally:
        conn.close()
    return [dict(zip(COLUMNS, r)) for r in rows]


def main():
    parser = argparse.ArgumentParser(description="迁移 SQLite -> 目标后端")
    parser.add_argument("--source-path", required=True, help="源 SQLite 文件绝对路径")
    args = parser.parse_args()

    rows = read_source(args.source_path)
    print(f"源数据: {len(rows)} 期")
    if not rows:
        print("源库为空, 退出")
        return

    target = get_storage()
    backend = target.config.backend
    print(f"目标后端: {backend}")
    added = target.insert(rows)
    v = target.verify()
    print(f"迁移完成: 目标行数 {v.rows:,}  新增 {added}  "
          f"重复 {v.duplicates}  和值错误 {v.bad_sum}  通过={v.ok}")


if __name__ == "__main__":
    main()
