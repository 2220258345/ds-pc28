# -*- coding: utf-8 -*-
"""
28数据分析 · 轻量 API 服务器
============================================================
- 只从数据库读取数据, 不启动采集线程
- 适合与 server.py (采集服务器) 分开部署
- API: /api/time /api/latest /api/history /api/trend
        /api/unopened /api/sum-unopened /api/draws /api/status
- SSE:  /api/events (推送 new_draw, 需采集服务器入库后触发)

用法:
  python api_server.py              # 默认 8000 端口
  python api_server.py --port 9000  # 指定端口
"""
import argparse
import json
import os
import queue
import sqlite3
import threading
import time
import urllib.request
import ssl
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(os.environ.get("DB_DIR", BASE), "pc28_history.db")
CN_TZ = timezone(timedelta(hours=8))

# 开奖周期参数 (与 server.py 保持一致)
CYCLE = 210
BASE_EPOCH = 1058114851

# 本地时钟偏移 (从参考站同步)
_time_offset = 0.0
_offset_lock = threading.Lock()

# ========== SSE 客户端管理 ==========
_sse_clients = set()
_sse_lock = threading.Lock()


def sse_register():
    q = queue.Queue()
    with _sse_lock:
        _sse_clients.add(q)
    return q


def sse_unregister(q):
    with _sse_lock:
        _sse_clients.discard(q)


def sse_broadcast(event, data):
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


def get_synced_ts():
    return time.time() + _time_offset


def sync_time_offset():
    """从参考站同步时钟偏移 (与 server.py 同算法)。"""
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
    best = min(samples)
    with _offset_lock:
        _time_offset = best
    print(f"[time-sync] offset={best:+.3f}s (min of {len(samples)} samples)")


def calc_countdown(ts):
    elapsed = int(ts) - BASE_EPOCH
    if elapsed < 0:
        return 0, 0
    current_period = elapsed // CYCLE
    remaining = CYCLE - (elapsed % CYCLE)
    if remaining == CYCLE:
        remaining = 0
    return current_period, remaining


# ========== 数据库查询 (与 server.py 一致) ==========

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
    conn = sqlite3.connect(DB_PATH)
    try:
        return conn.execute(
            "SELECT draw_nbr, draw_date, draw_time, c1, c2, c3, draw_num, "
            "size_type, parity_type, combination_type "
            "FROM draws ORDER BY draw_nbr DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()


def get_history(page=1, size=30):
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
            "total": total, "page": page, "size": size,
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
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            "SELECT draw_nbr, draw_num, size_type, parity_type "
            "FROM draws ORDER BY draw_nbr DESC LIMIT ?", (limit,)
        ).fetchall()
        rows.reverse()
        return [{"draw_nbr": r[0], "draw_num": r[1], "size_type": r[2], "parity_type": r[3]} for r in rows]
    finally:
        conn.close()


def get_unopened_stats():
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
            result[t] = (latest - row[0]) if row and latest else 0
        return result
    finally:
        conn.close()


SUM_ODDS = {
    0: 920, 27: 920, 1: 300, 26: 300, 2: 150, 25: 150,
    3: 90, 24: 90, 4: 60, 23: 60, 5: 38, 22: 38,
    6: 30, 21: 30, 7: 24, 20: 24, 8: 19, 19: 19,
    9: 16, 18: 16, 10: 15, 17: 15, 11: 14, 16: 14,
    12: 13.2, 15: 13.2, 13: 13.2, 14: 13.2,
}


def get_sum_unopened_stats():
    conn = sqlite3.connect(DB_PATH)
    try:
        latest = conn.execute("SELECT MAX(draw_nbr) FROM draws").fetchone()[0] or 0
        last_seen = {}
        for s in range(28):
            row = conn.execute(
                "SELECT draw_nbr FROM draws WHERE draw_num=? ORDER BY draw_nbr DESC LIMIT 1", (s,)
            ).fetchone()
            last_seen[s] = row[0] if row else 0
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


def row_to_latest(row):
    if not row:
        return None
    return {
        "draw_nbr": row[0], "draw_date": row[1], "draw_time": row[2],
        "c1": row[3], "c2": row[4], "c3": row[5], "draw_num": row[6],
        "size_type": row[7], "parity_type": row[8], "combination_type": row[9],
    }


# ========== 后台监听新数据 (轮询数据库, 通过 SSE 推送) ==========
_last_pushed_nbr = 0
_poll_lock = threading.Lock()


def db_poll_loop():
    """后台轮询数据库, 检测到新数据时通过 SSE 推送给前端。
    不做采集, 只读数据库 (由 server.py 采集服务器写入)。"""
    global _last_pushed_nbr
    # 初始化: 记录当前最新期号
    row = get_latest_draw()
    if row:
        _last_pushed_nbr = row[0]
    while True:
        try:
            row = get_latest_draw()
            if row and row[0] > _last_pushed_nbr:
                with _poll_lock:
                    _last_pushed_nbr = row[0]
                latest = row_to_latest(row)
                now_ts = get_synced_ts()
                period, remaining = calc_countdown(now_ts)
                sse_broadcast("new_draw", {
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
                "server_time": now_ts, "base_epoch": BASE_EPOCH, "cycle": CYCLE,
                "current_period": period, "countdown": remaining, "time_offset": _time_offset,
            }, ensure_ascii=False))

        elif path == "/api/events":
            self._sse_stream()
            return

        elif path == "/api/latest":
            now_ts = get_synced_ts()
            period, remaining = calc_countdown(now_ts)
            latest = row_to_latest(get_latest_draw())
            self._json(200, json.dumps({
                "latest": latest, "current_period": period,
                "countdown": remaining, "server_time": now_ts,
            }, ensure_ascii=False))

        elif path == "/api/history":
            page = int(qs.get("page", ["1"])[0])
            size = min(int(qs.get("size", ["30"])[0]), 100)
            self._json(200, json.dumps(get_history(page, size), ensure_ascii=False))

        elif path == "/api/trend":
            limit = min(int(qs.get("limit", ["100"])[0]), 500)
            self._json(200, json.dumps(get_trend(limit), ensure_ascii=False))

        elif path == "/api/unopened":
            self._json(200, json.dumps(get_unopened_stats(), ensure_ascii=False))

        elif path == "/api/sum-unopened":
            self._json(200, json.dumps(get_sum_unopened_stats(), ensure_ascii=False))

        elif path == "/api/draws":
            # 复用 server.py 的 get_draws_json 需要 backtest_e9, 这里简化
            from server import get_draws_json
            self._json(200, get_draws_json(), raw=True)

        elif path == "/api/status":
            n, mx_nbr, mx_date = get_db_rows()
            self._json(200, json.dumps({
                "total_rows": n, "max_nbr": mx_nbr, "max_date": mx_date,
                "server_time": datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                "mode": "api-only (no collector)",
            }, ensure_ascii=False))

        elif path == "/" or path == "/index.html":
            self.path = "/index.html"
            super().do_GET()

        elif path == "/backtest":
            # 回测图表仪表盘
            self.path = "/backtest_chart.html"
            super().do_GET()

        elif path == "/api-doc":
            # API 文档 (HTML 渲染 markdown)
            self._render_markdown()

        else:
            super().do_GET()

    def _json(self, code, body, raw=False):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def _render_markdown(self):
        """读取 API.md 并用 HTML + marked.js 渲染, 浏览器直接查看。"""
        md_path = os.path.join(BASE, "API.md")
        try:
            with open(md_path, "r", encoding="utf-8") as f:
                md_content = f.read()
        except FileNotFoundError:
            self._json(404, json.dumps({"error": "API.md not found"}))
            return
        # 转义反引号等, 安全嵌入到 JS 字符串
        import html as _html
        md_escaped = md_content.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")
        html_page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>API 对接文档 · 28数据分析</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:opsz,wght@9..40,400;9..40,500;9..40,600;9..40,700&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.0/marked.min.js"></script>
<style>
  :root {{
    --paper:#f4f5f7; --panel:#ffffff; --panel-2:#eef0f2;
    --ink:#1d1d1f; --ink-2:#5c5c62; --ink-3:#8b8b91;
    --rule:#e2e3e6; --rule-2:#ecedef;
    --accent:#2c5282; --red:#c4444a; --green:#2f855a; --amber:#b7791f;
    --radius:4px;
  }}
  body {{
    font-family:"DM Sans",-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
    background:var(--paper); color:var(--ink);
    line-height:1.6; margin:0; padding:24px 20px 60px;
    -webkit-font-smoothing:antialiased;
  }}
  .container {{ max-width:900px; margin:0 auto; }}
  h1, h2, h3 {{ color:var(--ink); margin-top:32px; letter-spacing:-0.01em; }}
  h1 {{ font-size:26px; font-weight:700; border-bottom:1px solid var(--rule); padding-bottom:10px; }}
  h2 {{ font-size:20px; font-weight:600; border-bottom:1px solid var(--rule-2); padding-bottom:6px; }}
  h3 {{ font-size:16px; font-weight:600; color:var(--accent); }}
  p, li {{ font-size:15px; color:var(--ink-2); }}
  a {{ color:var(--accent); }}
  code {{
    background:var(--panel-2); color:var(--accent);
    padding:2px 6px; border-radius:var(--radius); border:1px solid var(--rule);
    font-family:"JetBrains Mono",monospace; font-size:14px;
  }}
  pre {{
    background:var(--panel); border:1px solid var(--rule);
    border-radius:var(--radius); padding:16px 20px; overflow-x:auto;
  }}
  pre code {{
    background:transparent; border:none; color:var(--ink);
    padding:0; font-size:14px;
  }}
  table {{
    border-collapse:collapse; width:100%; margin:12px 0;
    font-size:14px;
  }}
  th, td {{
    border:1px solid var(--rule); padding:8px 12px; text-align:left;
  }}
  th {{ background:var(--panel-2); color:var(--ink); font-weight:600; }}
  td {{ color:var(--ink-2); }}
  tr:nth-child(even) td {{ background:var(--paper); }}
  blockquote {{
    border-left:3px solid var(--accent);
    margin:12px 0; padding:8px 16px;
    background:var(--panel); border-radius:0 var(--radius) var(--radius) 0;
    color:var(--ink-3);
  }}
  hr {{ border:none; border-top:1px solid var(--rule); margin:24px 0; }}
</style>
</head>
<body>
<div class="container" id="content"></div>
<script>
  const md = `{md_escaped}`;
  document.getElementById('content').innerHTML = marked.parse(md);
</script>
</body>
</html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(html_page.encode("utf-8"))

    def _sse_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q = sse_register()
        try:
            # hello: 同步服务器时间
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
        pass


def main():
    parser = argparse.ArgumentParser(description="28数据分析 · 轻量 API 服务器 (不采集)")
    parser.add_argument("--port", type=int, default=8000, help="端口 (默认8000)")
    args = parser.parse_args()

    n, mx_nbr, mx_date = get_db_rows()
    print(f"28数据分析 · 轻量 API 服务器")
    print(f"模式: 仅 API (不启动采集线程, 数据由 server.py 写入数据库)")
    print(f"数据库: {n:,} 期, 最新期号 {mx_nbr} ({mx_date})")

    # 同步时钟
    print("同步时钟...")
    sync_time_offset()

    def time_sync_loop():
        while True:
            time.sleep(300)
            sync_time_offset()

    threading.Thread(target=time_sync_loop, daemon=True).start()

    # 启动数据库轮询线程 (检测新数据 -> SSE 推送)
    threading.Thread(target=db_poll_loop, daemon=True).start()
    print("数据库轮询线程已启动 (检测新数据后通过 SSE 推送)")

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
