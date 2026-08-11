# -*- coding: utf-8 -*-
"""
28数据分析服务器
============================================================
- 提供 HTTP 服务 (静态文件 + API)
- 后台线程定时采集最新数据 (默认每3.5分钟)
- API: /api/time /api/latest /api/history /api/trend /api/unopened
        /api/draws /api/status /api/update

用法:
  python server.py              # 默认 8000 端口, 3.5分钟采集间隔
  python server.py --port 9000  # 指定端口
  python server.py --interval 2 # 2分钟采集间隔
"""
import argparse
import json
import os
import queue
import ssl
import sqlite3
import threading
import time
import urllib.request
import urllib.error
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs

from collector import fetch_with_failover, INCREMENTAL_ORDER, insert_rows, verify

# ========== SSE 客户端管理 ==========
_sse_clients = set()
_sse_lock = threading.Lock()


def sse_register():
    """注册一个 SSE 客户端, 返回 (client_id, queue)。"""
    q = queue.Queue()
    cid = id(q)
    with _sse_lock:
        _sse_clients.add(q)
    return cid, q


def sse_unregister(q):
    """注销 SSE 客户端。"""
    with _sse_lock:
        _sse_clients.discard(q)


def sse_broadcast(event, data):
    """向所有 SSE 客户端推送事件。"""
    msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    dead = []
    with _sse_lock:
        clients = list(_sse_clients)
    for q in clients:
        try:
            q.put_nowait(msg)
        except queue.Full:
            dead.append(q)
    if dead:
        with _sse_lock:
            for q in dead:
                _sse_clients.discard(q)

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(os.environ.get("DB_DIR", BASE), "pc28_history.db")
CN_TZ = timezone(timedelta(hours=8))

# 开奖周期参数
CYCLE = 210            # 每期 210 秒 (3.5 分钟)
BASE_EPOCH = 1058114851  # 期号 0 对应的 Unix 时间戳 (北京时间 2003-07-14 00:47:31)

# 本地时钟与参考站时钟的偏移 (秒), >0 表示本地慢
_time_offset = 0.0
_offset_lock = threading.Lock()


def get_synced_ts():
    """返回校正后的时间戳 (本地时间 + offset)。"""
    return time.time() + _time_offset


def sync_time_offset():
    """从参考站 api.php 获取服务器时间, 计算本地时钟偏移。
    多次采样取最小 offset, 消除网络延迟带来的正向偏差 (本地被算得偏慢 -> offset 偏大)。
    """
    global _time_offset
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    samples = []
    for i in range(5):
        try:
            t1 = time.time()
            req = urllib.request.Request(
                "https://www.jndpc.net/api.php?t=" + str(int(t1 * 1000)),
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
            r = urllib.request.urlopen(req, timeout=8, context=ctx)
            t2 = time.time()
            data = json.loads(r.read().decode("utf-8", errors="replace"))
            ref_ts = data.get("server_time") or data.get("timestamp") or 0
            if ref_ts:
                # 用 mid=(t1+t2)/2 估算服务器生成响应的时刻 (假设网络上下行对称)
                # 减 2.0s 抵消 server_time 生成到响应发出的延迟 (实测让倒计时与参考站对齐)
                mid = (t1 + t2) / 2
                est_offset = ref_ts - mid - 2.0
                samples.append(est_offset)
        except Exception as e:
            print(f"[time-sync] 第{i+1}次失败: {e}")
        if i < 4:
            time.sleep(0.5)
    if not samples:
        print("[time-sync] 全部失败, 保持原 offset")
        return
    # 取最小值: 网络延迟只会让 offset 偏大, 真实值 <= 所有测量值
    best = min(samples)
    avg = sum(samples) / len(samples)
    with _offset_lock:
        _time_offset = best
    print(f"[time-sync] offset={best:+.3f}s (min of {len(samples)} samples, avg={avg:+.3f})")


def calc_countdown(ts):
    """根据时间戳计算当前期号和距下期更新秒数。"""
    elapsed = int(ts) - BASE_EPOCH
    if elapsed < 0:
        return 0, 0
    current_period = elapsed // CYCLE
    remaining = CYCLE - (elapsed % CYCLE)
    if remaining == CYCLE:
        remaining = 0
    return current_period, remaining

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


def get_latest_draw():
    """获取最新一期数据。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT draw_nbr, draw_date, draw_time, c1, c2, c3, draw_num, "
            "size_type, parity_type, combination_type "
            "FROM draws ORDER BY draw_nbr DESC LIMIT 1"
        ).fetchone()
        return row
    finally:
        conn.close()


def get_history(page=1, size=30):
    """分页获取历史数据。"""
    conn = sqlite3.connect(DB_PATH)
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


def get_trend(limit=100):
    """获取最近 limit 期走势数据 (按期号升序)。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT draw_nbr, draw_num, size_type, parity_type "
            "FROM draws ORDER BY draw_nbr DESC LIMIT ?",
            (limit,)
        ).fetchall()
        rows.reverse()
        return [{
            "draw_nbr": r[0], "draw_num": r[1],
            "size_type": r[2], "parity_type": r[3],
        } for r in rows]
    finally:
        conn.close()


def get_unopened_stats():
    """计算各形态未开期数。"""
    conn = sqlite3.connect(DB_PATH)
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


def get_sum_unopened_stats():
    """计算每个和值 (0-27) 的未出期数 + 赔率, 按赔率分组返回。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        latest = conn.execute("SELECT MAX(draw_nbr) FROM draws").fetchone()[0] or 0
        # 一次性查出每个和值最后一次出现的期号
        last_seen = {}
        for s in range(28):
            row = conn.execute(
                "SELECT draw_nbr FROM draws WHERE draw_num=? ORDER BY draw_nbr DESC LIMIT 1",
                (s,)
            ).fetchone()
            last_seen[s] = row[0] if row else 0
        # 按赔率对分组 (0/27, 1/26, ..., 13/14)
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
                "unopened": [latest - last_seen[s] if last_seen[s] else -1,
                             latest - last_seen[27 - s] if last_seen[27 - s] else -1],
            })
        return {"latest_nbr": latest, "groups": groups}
    finally:
        conn.close()


def get_draws_json():
    """从数据库读取全部数据，返回 JSON 字符串。"""
    from backtest_e9 import (
        LADDER, BIG_THRESHOLD, COMMISSION_SUMS, COMMISSION_RATE,
        HIGH_BET_THRESHOLD, HIGH_BET_RATE,
    )
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
    """执行一次增量采集。采集到新数据时, 通过 SSE 推送给前端。"""
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

        # 采集到新数据, 立即推送给所有前端
        if added > 0:
            row = get_latest_draw()
            if row:
                latest = {
                    "draw_nbr": row[0], "draw_date": row[1], "draw_time": row[2],
                    "c1": row[3], "c2": row[4], "c3": row[5], "draw_num": row[6],
                    "size_type": row[7], "parity_type": row[8], "combination_type": row[9],
                }
                now_ts = get_synced_ts()
                period, remaining = calc_countdown(now_ts)
                sse_broadcast("new_draw", {
                    "latest": latest,
                    "added": added,
                    "current_period": period,
                    "countdown": remaining,
                    "server_time": now_ts,
                })
                print(f"[sse] 已推送 new_draw #{latest['draw_nbr']} 给 {len(_sse_clients)} 个客户端")
        return "ok"
    except Exception as e:
        with _lock:
            _status["last_result"] = f"error: {e}"
            _status["last_update"] = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
        return f"error: {e}"


def auto_update_loop(interval_min):
    """后台线程：与开奖周期同步采集。
    在周期边界前 2 秒开始预热 (jndpc 可能提前发布), 归零后每 0.3 秒重试, 直到拿到新数据。"""
    while True:
        with _lock:
            running = _status["auto_update"]
        if running:
            # 等到周期边界前 2 秒 (预热: jndpc 有时提前几秒发布新期)
            ts = get_synced_ts()
            _, remaining = calc_countdown(ts)
            sleep_sec = max(0, remaining - 2)
            if sleep_sec > 1:
                print(f"[auto-update] 等待 {sleep_sec:.1f}s 后开始预热采集...")
                time.sleep(sleep_sec)
            # 采集: 每0.3秒重试, 直到拿到新数据 (最多 90 次 = 27 秒)
            old_max = get_db_rows()[1] or 0
            print(f"[auto-update] 开始采集, old_max={old_max}")
            for attempt in range(90):
                try:
                    do_update()
                    new_max = get_db_rows()[1] or 0
                    if new_max > old_max:
                        print(f"[auto-update] 成功: {old_max} -> {new_max} (第{attempt+1}次尝试)")
                        break
                    time.sleep(0.3)
                except Exception as e:
                    print(f"[auto-update] {e}")
                    time.sleep(0.3)
        else:
            time.sleep(10)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """多线程 HTTP 服务器，避免单请求阻塞。"""
    daemon_threads = True


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE, **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/time":
            now_ts = get_synced_ts()
            period, remaining = calc_countdown(now_ts)
            self._json(200, json.dumps({
                "server_time": now_ts,
                "base_epoch": BASE_EPOCH,
                "cycle": CYCLE,
                "current_period": period,
                "countdown": remaining,
                "time_offset": _time_offset,
            }, ensure_ascii=False))

        elif path == "/api/events":
            # SSE: 服务器推送, 采集到新数据时立即通知前端
            self._sse_stream()
            return

        elif path == "/api/latest":
            now_ts = get_synced_ts()
            period, remaining = calc_countdown(now_ts)
            row = get_latest_draw()
            if row:
                latest = {
                    "draw_nbr": row[0], "draw_date": row[1], "draw_time": row[2],
                    "c1": row[3], "c2": row[4], "c3": row[5], "draw_num": row[6],
                    "size_type": row[7], "parity_type": row[8], "combination_type": row[9],
                }
            else:
                latest = None
            self._json(200, json.dumps({
                "latest": latest,
                "current_period": period,
                "countdown": remaining,
                "server_time": now_ts,
            }, ensure_ascii=False))

        elif path == "/api/poll":
            # 长轮询: 等待新期数据入库后返回, 最多等 timeout 秒
            after = int(qs.get("after", ["0"])[0])
            timeout = int(qs.get("timeout", ["30"])[0])
            deadline = time.time() + timeout
            while time.time() < deadline:
                row = get_latest_draw()
                if row and row[0] > after:
                    now_ts = get_synced_ts()
                    period, remaining = calc_countdown(now_ts)
                    latest = {
                        "draw_nbr": row[0], "draw_date": row[1], "draw_time": row[2],
                        "c1": row[3], "c2": row[4], "c3": row[5], "draw_num": row[6],
                        "size_type": row[7], "parity_type": row[8], "combination_type": row[9],
                    }
                    self._json(200, json.dumps({
                        "latest": latest,
                        "current_period": period,
                        "countdown": remaining,
                        "server_time": now_ts,
                    }, ensure_ascii=False))
                    return
                time.sleep(2)
            # 超时, 返回当前状态
            now_ts = get_synced_ts()
            period, remaining = calc_countdown(now_ts)
            row = get_latest_draw()
            latest = None
            if row:
                latest = {
                    "draw_nbr": row[0], "draw_date": row[1], "draw_time": row[2],
                    "c1": row[3], "c2": row[4], "c3": row[5], "draw_num": row[6],
                    "size_type": row[7], "parity_type": row[8], "combination_type": row[9],
                }
            self._json(200, json.dumps({
                "latest": latest,
                "current_period": period,
                "countdown": remaining,
                "server_time": now_ts,
                "timeout": True,
            }, ensure_ascii=False))

        elif path == "/api/history":
            page = int(qs.get("page", ["1"])[0])
            size = int(qs.get("size", ["30"])[0])
            size = min(size, 100)
            data = get_history(page, size)
            self._json(200, json.dumps(data, ensure_ascii=False))

        elif path == "/api/trend":
            limit = int(qs.get("limit", ["100"])[0])
            limit = min(limit, 500)
            data = get_trend(limit)
            self._json(200, json.dumps(data, ensure_ascii=False))

        elif path == "/api/unopened":
            data = get_unopened_stats()
            self._json(200, json.dumps(data, ensure_ascii=False))

        elif path == "/api/sum-unopened":
            data = get_sum_unopened_stats()
            self._json(200, json.dumps(data, ensure_ascii=False))

        elif path == "/api/draws":
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
            self.path = "/index.html"
            super().do_GET()

        elif path == "/backtest":
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

    def _sse_stream(self):
        """SSE 长连接: 推送 new_draw 事件, 每 15 秒发心跳保活。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        cid, q = sse_register()
        try:
            # 连接建立时先发一个 hello, 顺便同步服务器时间
            now_ts = get_synced_ts()
            period, remaining = calc_countdown(now_ts)
            hello = f"event: hello\ndata: {json.dumps({'server_time': now_ts, 'current_period': period, 'countdown': remaining}, ensure_ascii=False)}\n\n"
            self.wfile.write(hello.encode("utf-8"))
            self.wfile.flush()

            last_heartbeat = time.time()
            while True:
                try:
                    msg = q.get(timeout=1.0)
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    # 每 15 秒发心跳, 防止代理超时断开
                    now = time.time()
                    if now - last_heartbeat >= 15:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        last_heartbeat = now
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            sse_unregister(q)

    def log_message(self, fmt, *args):
        pass  # 静默日志


def main():
    parser = argparse.ArgumentParser(description="28数据分析服务器")
    parser.add_argument("--port", type=int, default=8000, help="端口 (默认8000)")
    parser.add_argument("--interval", type=float, default=3.5, help="自动采集间隔分钟 (默认3.5)")
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

    # 同步时钟 (与参考站对齐, 消除本地时钟偏差)
    print("同步时钟...")
    sync_time_offset()

    def time_sync_loop():
        """每5分钟同步一次时钟"""
        while True:
            time.sleep(300)
            sync_time_offset()

    t_sync = threading.Thread(target=time_sync_loop, daemon=True)
    t_sync.start()

    # 启动后台采集线程
    t = threading.Thread(target=auto_update_loop, args=(args.interval,), daemon=True)
    t.start()
    print(f"自动采集线程已启动 (每{args.interval}分钟)")

    # 启动 HTTP 服务
    server = ThreadedHTTPServer(("127.0.0.1", args.port), Handler)
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
