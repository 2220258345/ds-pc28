# -*- coding: utf-8 -*-
"""
28数据分析 · 轻量 API 服务器 (不采集)
============================================================
- 只从数据库读取数据, 不启动采集线程
- 适合与 server.py (采集服务器) 分开部署
- 路由由 core.api_routes 提供, 与 server.py 完全一致
- SSE 推送通过轮询数据库检测新数据 (由 server.py 写入)

用法:
  python api_server.py              # 默认 8000 端口
  python api_server.py --port 9000  # 指定端口
"""
import argparse
import threading
import time
from http.server import HTTPServer
from socketserver import ThreadingMixIn

from core import db, time_sync, sse, api_routes

# ========== 数据库轮询线程 (检测新数据 -> SSE 推送) ==========
_last_pushed_nbr = 0
_poll_lock = threading.Lock()


def db_poll_loop():
    """后台轮询数据库, 检测到新数据时通过 SSE 推送给前端。

    不做采集, 只读数据库 (由 server.py 采集服务器写入)。
    """
    global _last_pushed_nbr
    # 初始化: 记录当前最新期号
    row = db.get_latest_draw()
    if row:
        _last_pushed_nbr = row[0]
    while True:
        try:
            row = db.get_latest_draw()
            if row and row[0] > _last_pushed_nbr:
                with _poll_lock:
                    _last_pushed_nbr = row[0]
                latest = db.row_to_latest(row)
                now_ts = time_sync.get_synced_ts()
                period, remaining = time_sync.calc_countdown(now_ts)
                sse.sse_broadcast("new_draw", {
                    "latest": latest,
                    "added": 1,
                    "current_period": period,
                    "countdown": remaining,
                    "server_time": now_ts,
                })
                print(f"[poll->sse] 检测到新数据 #{latest['draw_nbr']}, 已推送")
        except Exception as e:
            print(f"[poll->sse] {e}")
        time.sleep(0.5)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(description="28数据分析 · 轻量 API 服务器 (不采集)")
    parser.add_argument("--port", type=int, default=8000, help="端口 (默认8000)")
    args = parser.parse_args()

    n, mx_nbr, mx_date = db.get_db_rows()
    print(f"28数据分析 · 轻量 API 服务器")
    print(f"模式: 仅 API (不启动采集线程, 数据由 server.py 写入数据库)")
    print(f"数据库: {n:,} 期, 最新期号 {mx_nbr} ({mx_date})")

    # 同步时钟
    print("同步时钟...")
    time_sync.sync_time_offset()
    time_sync.start_sync_loop(300)

    # 启动数据库轮询线程 (检测新数据 -> SSE 推送)
    threading.Thread(target=db_poll_loop, daemon=True).start()
    print("数据库轮询线程已启动 (检测新数据后通过 SSE 推送)")

    # 不注入采集回调, /api/update 和 /api/toggle-auto 将返回 not_supported
    Handler = api_routes.make_handler()

    server = ThreadedHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"服务器已启动: http://localhost:{args.port}/")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
