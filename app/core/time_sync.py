# -*- coding: utf-8 -*-
"""时钟同步模块 — 与参考站对齐本地时钟, 计算开奖周期与倒计时。

本模块维护一个全局时钟偏移 _time_offset, 由 sync_time_offset() 周期性更新,
get_synced_ts() 返回校正后的时间戳, calc_countdown() 据此计算期号与剩余秒数。
"""
import json
import ssl
import threading
import time
import urllib.request

# 开奖周期参数
CYCLE = 210              # 每期 210 秒 (3.5 分钟)
BASE_EPOCH = 1058114851  # 期号 0 对应的 Unix 时间戳 (北京时间 2003-07-14 00:47:31)

# 本地时钟与参考站时钟的偏移 (秒), >0 表示本地慢
_time_offset = 0.0
_offset_lock = threading.Lock()


def get_synced_ts():
    """返回校正后的时间戳 (本地时间 + offset)。"""
    return time.time() + _time_offset


def get_offset():
    """返回当前时钟偏移 (供 API 暴露给前端)。"""
    return _time_offset


def sync_time_offset():
    """从参考站 api.php 获取服务器时间, 计算本地时钟偏移。

    多次采样取最小 offset, 消除网络延迟带来的正向偏差
    (本地被算得偏慢 -> offset 偏大)。
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


def start_sync_loop(interval_sec=300):
    """启动后台线程, 每 interval_sec 秒同步一次时钟。"""
    def loop():
        while True:
            time.sleep(interval_sec)
            sync_time_offset()
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t
