# -*- coding: utf-8 -*-
"""
PC28 回测仪表盘服务器
============================================================
- 提供 HTTP 服务 (静态文件 + API)
- 后台线程定时采集最新数据 (默认每4分钟)
- API: /api/draws  /api/status  /api/update

用法:
  python server.py              # 默认 8000 端口, 4分钟采集间隔
  python server.py --port 9000  # 指定端口
  python server.py --interval 2 # 2分钟采集间隔
"""
import argparse
import json
import os
import sqlite3
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

from backtest_e9 import (
    LADDER, BIG_THRESHOLD, COMMISSION_SUMS, COMMISSION_RATE,
    HIGH_BET_THRESHOLD, HIGH_BET_RATE,
)
from collector import fetch_with_failover, INCREMENTAL_ORDER, insert_rows, verify

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(os.environ.get("DB_DIR", BASE), "pc28_history.db")
CN_TZ = timezone(timedelta(hours=8))

# 状态
_status = {
    "last_update": None,
    "last_result": None,
    "last_count": 0,
    "auto_update": True,
    "interval_min": 4,
    "total_rows": 0,
}
_lock = threading.Lock()


def get_db_rows():
    conn = sqlite3.connect(DB_PATH)
    try:
        n = conn.execute("SELECT COUNT(*) FROM draws").fetchone()[0]
        mx = conn.execute("SELECT MAX(draw_nbr), MAX(draw_date) FROM draws").fetchone()
        return n, mx[0], mx[1]
    except:
        return 0, None, None
    finally:
        conn.close()


def get_draws_json():
    """从数据库读取全部开奖数据，返回 JSON 字符串。"""
    conn = sqlite3.connect(DB_PATH)
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


def do_update():
    """执行一次增量采集。"""
    with _lock:
        if _status["last_result"] == "running":
            return "already_running"
        _status["last_result"] = "running"

    try:
        rows, src = fetch_with_failover(INCREMENTAL_ORDER, verbose=False)
        if not rows:
            with _lock:
                _status["last_result"] = "failed"
                _status["last_update"] = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
            return "failed"

        added = insert_rows(rows)
        ok = verify()
        n, mx_nbr, mx_date = get_db_rows()
        with _lock:
            _status["last_update"] = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
            _status["last_result"] = "ok" if ok else "verify_failed"
            _status["last_count"] = added
            _status["total_rows"] = n
        return "ok"
    except Exception as e:
        with _lock:
            _status["last_result"] = f"error: {e}"
            _status["last_update"] = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
        return f"error: {e}"


def auto_update_loop(interval_min):
    """后台线程：定时采集。"""
    while True:
        with _lock:
            running = _status["auto_update"]
        if running:
            try:
                do_update()
            except Exception as e:
                print(f"[auto-update] {e}")
        time.sleep(interval_min * 60)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE, **kwargs)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/api/draws":
            data = get_draws_json()
            self._json(200, data, raw=True)
        elif path == "/api/status":
            n, mx_nbr, mx_date = get_db_rows()
            with _lock:
                s = dict(_status)
            s["total_rows"] = n
            s["max_nbr"] = mx_nbr
            s["max_date"] = mx_date
            s["server_time"] = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
            self._json(200, json.dumps(s, ensure_ascii=False))
        elif path == "/api/update":
            result = do_update()
            self._json(200, json.dumps({"result": result}, ensure_ascii=False))
        elif path == "/" or path == "/index.html":
            self.path = "/backtest_chart.html"
            super().do_GET()
        else:
            super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/toggle-auto":
            with _lock:
                _status["auto_update"] = not _status["auto_update"]
                val = _status["auto_update"]
            self._json(200, json.dumps({"auto_update": val}))
        else:
            self._json(404, '{"error":"not found"}')

    def _json(self, code, body, raw=False):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body.encode("utf-8") if not raw else body.encode("utf-8"))

    def log_message(self, fmt, *args):
        pass  # 静默日志


def main():
    parser = argparse.ArgumentParser(description="PC28 回测仪表盘服务器")
    parser.add_argument("--port", type=int, default=8000, help="端口 (默认8000)")
    parser.add_argument("--interval", type=float, default=4, help="自动采集间隔分钟 (默认4)")
    args = parser.parse_args()

    # 初始化状态
    n, mx_nbr, mx_date = get_db_rows()
    with _lock:
        _status["total_rows"] = n
        _status["interval_min"] = args.interval

    # 空库或数据过少时, 先执行一次全量采集 (pc28.help 2000期, 覆盖更多历史)
    if n < 500:
        print(f"库内仅 {n} 期, 执行全量采集...")
        from collector import fetch_with_failover, FULL_ORDER
        try:
            rows, src = fetch_with_failover(FULL_ORDER, verbose=True)
            if rows:
                added = insert_rows(rows)
                verify()
                n, mx_nbr, mx_date = get_db_rows()
                with _lock:
                    _status["total_rows"] = n
                print(f"全量采集完成: 新增 {added} 期, 当前共 {n:,} 期")
        except Exception as e:
            print(f"全量采集失败: {e}, 将由增量线程继续尝试")

    # 启动后台采集线程
    t = threading.Thread(target=auto_update_loop, args=(args.interval,), daemon=True)
    t.start()
    print(f"自动采集线程已启动 (每{args.interval}分钟)")

    # 启动 HTTP 服务
    server = HTTPServer(("0.0.0.0", args.port), Handler)
    print(f"服务器已启动: http://localhost:{args.port}/")
    print(f"数据库: {n:,} 期, 最新期号 {mx_nbr} ({mx_date})")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
