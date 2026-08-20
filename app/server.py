# -*- coding: utf-8 -*-
"""
28数据分析服务器 (采集入口)
============================================================
- 提供 HTTP 服务 (静态文件 + API + SSE), 路由由 core.api_routes 提供
- 后台线程定时采集最新数据 (默认每3.5分钟, 与开奖周期同步)
- 采集到新数据时通过 SSE 推送给所有前端

用法:
  python server.py              # 默认 8000 端口, 3.5分钟采集间隔
  python server.py --port 9000  # 指定端口
  python server.py --interval 2 # 2分钟采集间隔
"""
import argparse
import socket
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer
from socketserver import ThreadingMixIn

from collector import (
    fetch_with_failover,
    INCREMENTAL_ORDER,
    FULL_ORDER,
    insert_rows,
    verify,
)
from core import db, time_sync, sse, api_routes

CN_TZ = timezone(timedelta(hours=8))

# ========== 采集状态 (仅 server.py 维护) ==========
_status = {
    "last_update": None,
    "last_result": None,
    "last_count": 0,
    "auto_update": True,
    "interval_min": 4,
    "total_rows": 0,
}
_lock = threading.Lock()


def status_provider():
    """供 api_routes 注入的状态查询函数。"""
    with _lock:
        return dict(_status)


def toggle_auto():
    """切换自动采集开关。"""
    with _lock:
        _status["auto_update"] = not _status["auto_update"]
        return _status["auto_update"]


def do_update():
    """执行一次增量采集。采集到新数据时, 通过 SSE 推送给前端。"""
    with _lock:
        if _status["last_result"] == "running":
            return "already_running"
        _status["last_result"] = "running"

    # 记录采集前的最新期号, 用于检测是否有新数据
    pre_max = db.get_db_rows()[1] or 0

    try:
        rows, src = fetch_with_failover(INCREMENTAL_ORDER, verbose=False)
        if not rows:
            with _lock:
                _status["last_result"] = "failed"
                _status["last_update"] = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
            print(f"[do_update] 采集失败 pre_max={pre_max}")
            return "failed"

        added = insert_rows(rows)
        ok = verify()
        n, mx_nbr, mx_date = db.get_db_rows()
        with _lock:
            _status["last_update"] = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
            _status["last_result"] = "ok" if ok else "verify_failed"
            _status["last_count"] = added
            _status["total_rows"] = n

        # 采集到新数据, 立即推送给所有前端
        if added > 0:
            row = db.get_latest_draw()
            if row:
                latest = db.row_to_latest(row)
                # 更新参考点 (保证期号计算始终基于最新数据)
                from datetime import datetime as _dt
                d = _dt.strptime(f"{row[1]} {row[2]}", '%Y-%m-%d %H:%M:%S')
                ref_ts = d.replace(tzinfo=time_sync.CN_TZ).timestamp()
                time_sync.set_reference(row[0], ref_ts)
                now_ts = time_sync.get_synced_ts()
                period, remaining = time_sync.calc_countdown(now_ts)
                sse.sse_broadcast("new_draw", {
                    "latest": latest,
                    "added": added,
                    "current_period": period,
                    "countdown": remaining,
                    "server_time": now_ts,
                })
                print(f"[sse] 已推送 new_draw #{latest['draw_nbr']} 给 {sse.client_count()} 个客户端 (added={added}, pre_max={pre_max}, new_max={mx_nbr})")
        else:
            print(f"[do_update] added=0 不推送 pre_max={pre_max} new_max={mx_nbr} src={src}")
        return "ok"
    except Exception as e:
        with _lock:
            _status["last_result"] = f"error: {e}"
            _status["last_update"] = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[do_update] 异常: {e}")
        return f"error: {e}"


def auto_update_loop(interval_min):
    """后台线程: 与开奖周期同步采集。

    在周期边界前 2 秒开始预热 (jndpc 可能提前发布),
    之后持续轮询直到拿到新期 —— 不设固定次数的放弃上限,
    避免开奖延迟时采集线程提前退出而错过结果, 导致前端无法第一时间显示。
    轮询间隔随尝试次数从 0.3s 逐步增大到 5s, 防止数据源长时间不可用时空转。
    """
    while True:
        with _lock:
            running = _status["auto_update"]
        if running:
            # 等到周期边界前 2 秒 (预热: jndpc 有时提前几秒发布新期)
            ts = time_sync.get_synced_ts()
            _, remaining = time_sync.calc_countdown(ts)
            sleep_sec = max(0, remaining - 2)
            if sleep_sec > 1:
                print(f"[auto-update] 等待 {sleep_sec:.1f}s 后开始预热采集...")
                time.sleep(sleep_sec)
            # 采集: 持续重试直到拿到新期, 不设固定放弃上限
            old_max = db.get_db_rows()[1] or 0
            print(f"[auto-update] 开始采集, old_max={old_max}")
            attempt = 0
            while True:
                try:
                    do_update()
                    new_max = db.get_db_rows()[1] or 0
                    if new_max > old_max:
                        print(f"[auto-update] 成功: {old_max} -> {new_max} (第{attempt+1}次尝试)")
                        break
                except Exception as e:
                    print(f"[auto-update] {e}")
                attempt += 1
                # 降频退避, 避免数据源不可用时高频空转
                time.sleep(min(0.3 + attempt * 0.05, 5.0))
        else:
            time.sleep(10)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """多线程 HTTP 服务器, 避免单请求阻塞。

    使用 IPv6 dual-stack: 监听 '::' 同时接受 IPv4 与 IPv6 连接
    (Windows 默认 V6ONLY=1, 需显式关闭以支持 IPv4 mapped 地址)。
    """
    daemon_threads = True
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            pass
        super().server_bind()

    def handle_error(self, request, client_address):
        """抑制客户端断连的日志噪音 (keep-alive 连接关闭时常见)。"""
        import sys
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError)):
            return
        super().handle_error(request, client_address)


def main():
    parser = argparse.ArgumentParser(description="28数据分析服务器 (采集入口)")
    parser.add_argument("--port", type=int, default=8000, help="端口 (默认8000)")
    parser.add_argument("--interval", type=float, default=3.5, help="自动采集间隔分钟 (默认3.5)")
    args = parser.parse_args()

    # 初始化状态
    n, mx_nbr, mx_date = db.get_db_rows()
    with _lock:
        _status["total_rows"] = n
        _status["interval_min"] = args.interval

    # 空库或数据过少时, 先执行一次全量采集 (pc28.help 2000期, 覆盖更多历史)
    if n < 500:
        print(f"库内仅 {n} 期, 执行全量采集...")
        try:
            rows, src = fetch_with_failover(FULL_ORDER, verbose=True)
            if rows:
                added = insert_rows(rows)
                verify()
                n, mx_nbr, mx_date = db.get_db_rows()
                with _lock:
                    _status["total_rows"] = n
                print(f"全量采集完成: 新增 {added} 期, 当前共 {n:,} 期")
        except Exception as e:
            print(f"全量采集失败: {e}, 将由增量线程继续尝试")

    # 同步时钟 (与参考站对齐, 消除本地时钟偏差)
    print("同步时钟...")
    time_sync.sync_time_offset()

    # 先建立参考点，避免采集线程在参考点未建立时用 BASE_EPOCH 兜底导致倒计时漂移
    row = db.get_latest_draw()
    if row:
        d = datetime.strptime(f"{row[1]} {row[2]}", "%Y-%m-%d %H:%M:%S")
        ref_ts = d.replace(tzinfo=time_sync.CN_TZ).timestamp()
        time_sync.set_reference(row[0], ref_ts)

    time_sync.start_sync_loop(300)

    # 启动后台采集线程
    t = threading.Thread(target=auto_update_loop, args=(args.interval,), daemon=True)
    t.start()
    print(f"自动采集线程已启动 (每{args.interval}分钟)")

    # 注入采集回调, 生成统一 Handler
    Handler = api_routes.make_handler(
        status_provider=status_provider,
        update_callback=do_update,
        toggle_auto_callback=toggle_auto,
    )

    # 启动 HTTP 服务: 优先 IPv6 dual-stack (同时接受 IPv4/IPv6),
    # 容器内不支持 IPv6 时自动回退到 IPv4 (Docker 端口映射仍支持双栈)
    try:
        server = ThreadedHTTPServer(("::", args.port), Handler)
        print(f"服务器已启动: http://localhost:{args.port}/ (IPv4 + IPv6 dual-stack)")
    except OSError:
        ThreadedHTTPServer.address_family = socket.AF_INET
        server = ThreadedHTTPServer(("0.0.0.0", args.port), Handler)
        print(f"服务器已启动: http://localhost:{args.port}/ (IPv4, 容器模式)")
    print(f"数据库: {n:,} 期, 最新期号 {mx_nbr} ({mx_date})")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.shutdown()


if __name__ == "__main__":
    main()
