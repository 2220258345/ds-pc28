# -*- coding: utf-8 -*-
"""统一 HTTP 路由处理器 — 所有 API 与静态页面路由的单一来源。

通过 make_handler() 工厂函数创建 Handler 类, 注入 status_provider 和 update_callback:
  - status_provider(): 返回当前服务状态的 dict (含 last_update/auto_update 等)
  - update_callback(): 执行一次采集 (仅 server.py 提供, api_server.py 不提供)

两个入口 (server.py / api_server.py) 共用同一套路由, 避免分支逻辑漂移。
"""
import gzip
import json
import os
import queue
import time
from datetime import datetime, timezone, timedelta
from http.server import SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

from . import db, time_sync, sse

# 项目根目录 (app/core/api_routes.py → app/core/ → app/ → 项目根)
BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATIC_DIR = os.path.join(BASE, "static")
CN_TZ = timezone(timedelta(hours=8))


def make_handler(status_provider=None, update_callback=None, toggle_auto_callback=None):
    """生成一个 Handler 类。

    参数:
      status_provider:    无参函数, 返回状态 dict (包含 last_update/last_result/
                          last_count/auto_update/interval_min/total_rows)。若为 None
                          则走"仅API"模式 (api_server.py 用)。
      update_callback:    无参函数, 触发一次采集, 返回结果字符串。若为 None 则
                          /api/update 返回 not_supported。
      toggle_auto_callback: 无参函数, 切换自动采集开关, 返回新的 auto_update 值。
                          若为 None 则 /api/toggle-auto 返回 not_supported。
    """

    class Handler(SimpleHTTPRequestHandler):
        # HTTP/1.1 必需: 支持 keep-alive 长连接, SSE 才能保持不断开
        protocol_version = "HTTP/1.1"

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=STATIC_DIR, **kwargs)

        # ============ 路由入口 ============
        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            if path == "/api/time":
                self._handle_time()
            elif path == "/api/events":
                self._sse_stream()
            elif path == "/api/latest":
                self._handle_latest()
            elif path == "/api/poll":
                self._handle_poll(qs)
            elif path == "/api/history":
                page = int(qs.get("page", ["1"])[0])
                size = min(int(qs.get("size", ["30"])[0]), 100)
                self._json(200, json.dumps(db.get_history(page, size), ensure_ascii=False))
            elif path == "/api/trend":
                limit = min(int(qs.get("limit", ["100"])[0]), 500)
                self._json(200, json.dumps(db.get_trend(limit), ensure_ascii=False))
            elif path == "/api/unopened":
                self._json(200, json.dumps(db.get_unopened_stats(), ensure_ascii=False))
            elif path == "/api/unopened-v2":
                self._json(200, json.dumps(db.get_unopened_stats_v2(), ensure_ascii=False))
            elif path == "/api/sum-unopened":
                self._json(200, json.dumps(db.get_sum_unopened_stats(), ensure_ascii=False))
            elif path == "/api/draws":
                self._json(200, db.get_draws_json(), raw=True)
            elif path == "/api/backtest":
                self._handle_backtest(qs)
            elif path == "/api/status":
                self._handle_status()
            elif path == "/api/update":
                self._handle_update()
            elif path == "/" or path == "/index.html":
                self._serve_html("index.html")
            elif path == "/api-doc":
                self._render_markdown()
            else:
                super().do_GET()

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/api/toggle-auto":
                if toggle_auto_callback:
                    val = toggle_auto_callback()
                    self._json(200, json.dumps({"auto_update": val}))
                else:
                    self._json(200, json.dumps({"auto_update": None, "supported": False}))
            else:
                self._json(404, '{"error":"not found"}')

        # ============ API 处理函数 ============
        def _serve_html(self, filename):
            """返回 HTML 文件, 禁用缓存确保浏览器始终获取最新版本。"""
            filepath = os.path.join(STATIC_DIR, filename)
            try:
                with open(filepath, "rb") as f:
                    content = f.read()
            except FileNotFoundError:
                self.send_error(404, "Not Found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(content)

        def _handle_time(self):
            now_ts = time_sync.get_synced_ts()
            # 自动设置参考点 (若未设置, 从数据库读取最新一期)
            ref_nbr, ref_ts = time_sync.get_reference()
            if ref_nbr is None:
                row = db.get_latest_draw()
                if row:
                    from datetime import datetime as _dt
                    d = _dt.strptime(f"{row[1]} {row[2]}", '%Y-%m-%d %H:%M:%S')
                    ref_ts = d.replace(tzinfo=time_sync.CN_TZ).timestamp()
                    time_sync.set_reference(row[0], ref_ts)
            period, remaining = time_sync.calc_countdown(now_ts)
            self._json(200, json.dumps({
                "server_time": now_ts,
                "base_epoch": time_sync.BASE_EPOCH,
                "cycle": time_sync.CYCLE,
                "current_period": period,
                "countdown": remaining,
                "time_offset": time_sync.get_offset(),
                "maintenance": time_sync.in_maintenance(now_ts),
            }, ensure_ascii=False))

        def _handle_latest(self):
            now_ts = time_sync.get_synced_ts()
            period, remaining = time_sync.calc_countdown(now_ts)
            latest = db.row_to_latest(db.get_latest_draw())
            self._json(200, json.dumps({
                "latest": latest,
                "current_period": period,
                "countdown": remaining,
                "server_time": now_ts,
            }, ensure_ascii=False))

        def _handle_poll(self, qs):
            """长轮询: 等待新期数据入库后返回, 最多等 timeout 秒。"""
            after = int(qs.get("after", ["0"])[0])
            timeout = int(qs.get("timeout", ["30"])[0])
            deadline = time.time() + timeout
            while time.time() < deadline:
                row = db.get_latest_draw()
                if row and row[0] > after:
                    self._handle_latest()
                    return
                time.sleep(2)
            # 超时, 返回当前状态
            self._handle_latest()

        def _handle_backtest(self, qs):
            """E9 策略回测 (后端 Python 计算, 比前端 JS 快 10 倍)。
            参数: stop_profit (float|null), stop_loss (float|null), reverse (0|1)
            """
            import importlib.util
            bt_path = os.path.join(BASE, "app", "backtest_e9.py")
            try:
                spec = importlib.util.spec_from_file_location("backtest_e9", bt_path)
                bt = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(bt)
            except Exception as e:
                self._json(500, json.dumps({"error": f"backtest module import failed: {e}"}))
                return

            # 解析参数
            sp_raw = qs.get("stop_profit", [""])[0]
            sl_raw = qs.get("stop_loss", [""])[0]
            rev = qs.get("reverse", ["0"])[0] == "1"
            stop_profit = float(sp_raw) if sp_raw not in ("", "null", "0") else None
            stop_loss = float(sl_raw) if sl_raw not in ("", "null", "0") else None
            if stop_loss is not None:
                stop_loss = -abs(stop_loss)

            draws = bt.load_draws()
            r = bt.run_backtest(draws, ladder=bt.LADDER, stop_profit=stop_profit,
                                stop_loss=stop_loss, detail=False, reverse=rev)

            # 构建前端所需的 days 数组 (已排序, 含累计盈亏 c)
            days = []
            cum = 0
            for d in sorted(r["daily"].keys()):
                info = r["daily"][d]
                cum += info["pnl"]
                days.append({
                    "d": d, "p": info["pnl"], "b": info["bets"],
                    "w": info["win"], "f": info["flat"], "l": info["lose"],
                    "br": info["bursts"], "ml": info.get("max_level", 0), "c": cum,
                })

            result = {
                "meta": {
                    "ladder": bt.LADDER,
                    "stop_profit": stop_profit,
                    "stop_loss": stop_loss,
                    "total_pnl": r["total_pnl"],
                    "max_drawdown": r["max_drawdown"],
                    "ratio": r["ratio"],
                    "bursts": r["bursts"],
                    "bets": r["total_bets"],
                    "win": r["win"], "flat": r["flat"], "lose": r["lose"],
                    "profit_days": r["profit_days"], "loss_days": r["loss_days"],
                },
                "days": days,
                "c": r["c"], "d": r["d"], "l": r["l"], "r": r["r"],
                "times": r["times"],
                "burstTimes": r["burst_times"],
            }
            self._json(200, json.dumps(result, ensure_ascii=False))

        def _handle_status(self):
            n, mx_nbr, mx_date = db.get_db_rows()
            if status_provider:
                s = dict(status_provider())
            else:
                # 仅API模式 (api_server.py): 无采集状态
                s = {
                    "last_update": None,
                    "last_result": None,
                    "last_count": 0,
                    "auto_update": None,
                    "interval_min": None,
                    "total_rows": n,
                }
            s["total_rows"] = n
            s["max_nbr"] = mx_nbr
            s["max_date"] = mx_date
            s["server_time"] = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")
            if not status_provider:
                s["mode"] = "api-only (no collector)"
            self._json(200, json.dumps(s, ensure_ascii=False))

        def _handle_update(self):
            if update_callback:
                result = update_callback()
            else:
                result = "not_supported"
            self._json(200, json.dumps({"result": result}, ensure_ascii=False))

        # ============ SSE 长连接 ============
        def _sse_stream(self):
            """SSE 长连接: 推送 new_draw 事件, 每 15 秒发心跳保活。"""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            q = sse.sse_register()
            try:
                # 连接建立时先发 hello, 顺便同步服务器时间
                now_ts = time_sync.get_synced_ts()
                period, remaining = time_sync.calc_countdown(now_ts)
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
                sse.sse_unregister(q)

        # ============ API 文档渲染 ============
        def _render_markdown(self):
            """读取 API.md 并用 HTML + marked.js 渲染, 浏览器直接查看。"""
            md_path = os.path.join(STATIC_DIR, "API.md")
            try:
                with open(md_path, "r", encoding="utf-8") as f:
                    md_content = f.read()
            except FileNotFoundError:
                self._json(404, json.dumps({"error": "API.md not found"}))
                return
            # 转义反引号等, 安全嵌入到 JS 字符串
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
    --paper:#f6f7fb; --panel:#ffffff; --panel-2:#f0f2f7;
    --ink:#1a1c2e; --ink-2:#545870; --ink-3:#8b8fa3;
    --rule:#e4e7ed; --rule-2:#eef0f5;
    --accent:#4361ee; --accent-2:#5874f5;
    --red:#e5484d; --green:#30a46c; --amber:#e8a317;
    --radius:6px; --radius-lg:12px;
    --shadow-sm:0 1px 3px rgba(20,30,60,0.06),0 1px 2px rgba(20,30,60,0.04);
    --shadow-md:0 4px 12px -2px rgba(20,30,60,0.07),0 2px 6px -2px rgba(20,30,60,0.04);
    --space-1:4px; --space-2:8px; --space-3:12px; --space-4:16px;
    --space-5:20px; --space-6:24px; --space-8:32px;
  }}
  body {{
    font-family:"DM Sans",-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
    background:var(--paper);
    background-image:linear-gradient(180deg,var(--panel-2) 0%,var(--paper) 280px);
    background-attachment:fixed;
    color:var(--ink); line-height:1.7;
    margin:0; padding:var(--space-8) var(--space-5) 60px;
    -webkit-font-smoothing:antialiased;
  }}
  .container {{ max-width:900px; margin:0 auto; }}
  h1, h2, h3 {{ color:var(--ink); letter-spacing:-0.02em; }}
  h1 {{
    font-size:28px; font-weight:700;
    border-bottom:2px solid var(--rule); padding-bottom:var(--space-3);
    margin-top:var(--space-6);
  }}
  h2 {{
    font-size:22px; font-weight:600;
    border-bottom:1px solid var(--rule-2); padding-bottom:var(--space-2);
    margin-top:var(--space-8);
  }}
  h3 {{ font-size:17px; font-weight:600; color:var(--accent); margin-top:var(--space-6); }}
  p, li {{ font-size:15px; color:var(--ink-2); }}
  p {{ margin:var(--space-2) 0; }}
  a {{ color:var(--accent); transition:color 0.15s ease; }}
  code {{
    background:var(--panel-2); color:var(--accent);
    padding:2px var(--space-2); border-radius:var(--radius); border:1px solid var(--rule);
    font-family:"JetBrains Mono",monospace; font-size:13px;
  }}
  pre {{
    background:var(--panel); border:1px solid var(--rule);
    border-radius:var(--radius-lg); padding:var(--space-4) var(--space-5);
    overflow-x:auto; box-shadow:var(--shadow-sm);
    margin:var(--space-4) 0;
  }}
  pre code {{
    background:transparent; border:none; color:var(--ink);
    padding:0; font-size:14px;
  }}
  table {{
    border-collapse:collapse; width:100%; margin:var(--space-4) 0;
    font-size:14px; border-radius:var(--radius-lg); overflow:hidden;
    box-shadow:var(--shadow-sm);
  }}
  th, td {{
    border:1px solid var(--rule); padding:var(--space-2) var(--space-4);
    text-align:left;
  }}
  th {{
    background:var(--panel-2); color:var(--ink); font-weight:600;
    letter-spacing:0.02em;
  }}
  td {{ color:var(--ink-2); }}
  tr:nth-child(even) td {{ background:var(--paper); }}
  blockquote {{
    border-left:4px solid var(--accent);
    margin:var(--space-4) 0; padding:var(--space-4) var(--space-5);
    background:var(--panel); border-radius:0 var(--radius-lg) var(--radius-lg) 0;
    box-shadow:var(--shadow-sm);
  }}
  blockquote p {{
    margin:var(--space-1) 0; color:var(--ink-2); font-size:14px;
    line-height:1.8;
  }}
  blockquote p:first-child {{ margin-top:0; }}
  blockquote p:last-child {{ margin-bottom:0; }}
  hr {{ border:none; border-top:1px solid var(--rule); margin:var(--space-8) 0; }}
  /* 顶部第一个 blockquote 特殊处理: 基础信息卡片 */
  blockquote:first-of-type {{
    background:linear-gradient(135deg,var(--panel) 0%,var(--panel-2) 100%);
    border-left-width:4px;
  }}
</style>
</head>
<body>
<div class="container" id="content"></div>
<script>
  marked.setOptions({{ breaks: true }});
  const md = `{md_escaped}`;
  document.getElementById('content').innerHTML = marked.parse(md);
</script>
</body>
</html>"""
            payload = html_page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(payload)

        # ============ 工具方法 ============
        def _json(self, code, body, raw=False):
            payload = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            # 大响应 (>1KB) 启用 gzip 压缩, /api/draws 1.3MB → ~200KB
            accept_enc = self.headers.get("Accept-Encoding", "")
            if "gzip" in accept_enc and len(payload) > 1024:
                payload = gzip.compress(payload, compresslevel=6)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):
            pass  # 静默日志

    return Handler
