# -*- coding: utf-8 -*-
"""时钟同步模块 — 与参考站对齐本地时钟, 计算开奖周期与倒计时。

本模块维护一个全局时钟偏移 _time_offset, 由 sync_time_offset() 周期性更新,
get_synced_ts() 返回校正后的时间戳, calc_countdown() 据此计算期号与剩余秒数。

期号计算采用"参考点 + 维护时段修正"策略:
  - 参考点: 数据库最新一期的 (期号, 时间戳), 由 set_reference() 设置
  - 维护时段: 夏令时 19:00-19:30 / 冬令时 20:00-20:30, 每个消耗 1980 秒
  - calc_countdown: 当前期号 = ref_nbr + (ts - ref_ts - 维护总时长) // CYCLE
"""
import json
import ssl
import threading
import time
import urllib.request
from datetime import datetime, timezone, timedelta

# 开奖周期参数
CYCLE = 210              # 每期 210 秒 (3.5 分钟)
BASE_EPOCH = 1058114851  # 回退用: 无参考点时使用

# 维护时段参数
MAINT_SECS = 1980        # 每个维护时段消耗秒数 (30分钟维护 + 3分钟恢复)
CN_TZ = timezone(timedelta(hours=8))  # 北京时区

# 本地时钟与参考站时钟的偏移 (秒), >0 表示本地慢
_time_offset = 0.0
_offset_lock = threading.Lock()

# 参考点 (期号, 时间戳), 由外部设置
_reference = (None, None)
_ref_lock = threading.Lock()


def get_synced_ts():
    """返回校正后的时间戳 (本地时间 + offset)。"""
    return time.time() + _time_offset


def get_offset():
    """返回当前时钟偏移 (供 API 暴露给前端)。"""
    return _time_offset


def set_reference(nbr, ts):
    """设置参考点 (数据库最新一期的期号和时间戳)。"""
    with _ref_lock:
        global _reference
        _reference = (nbr, ts)
    dt_str = datetime.fromtimestamp(ts, tz=CN_TZ).strftime('%Y-%m-%d %H:%M:%S')
    print(f"[time-sync] 参考点: 期号 {nbr}, 时间 {dt_str}")


def get_reference():
    """返回当前参考点 (nbr, ts)。"""
    with _ref_lock:
        return _reference


# ============ 维护时段 ============

def is_dst(dt):
    """判断是否夏令时 (北美规则: 3月第2个周日 ~ 11月第1个周日)。

    夏令时维护 19:00-19:30, 冬令时维护 20:00-20:30。
    """
    # 3月第2个周日
    mar1 = datetime(dt.year, 3, 1, tzinfo=CN_TZ)
    first_sun_mar = 1 + (6 - mar1.weekday()) % 7
    dst_start = datetime(dt.year, 3, first_sun_mar + 7, tzinfo=CN_TZ)
    # 11月第1个周日
    nov1 = datetime(dt.year, 11, 1, tzinfo=CN_TZ)
    first_sun_nov = 1 + (6 - nov1.weekday()) % 7
    dst_end = datetime(dt.year, 11, first_sun_nov, tzinfo=CN_TZ)
    return dst_start <= dt < dst_end


def get_maintenance_window(dt):
    """返回维护时段 (start_hour, start_min, end_hour, end_min)。"""
    if is_dst(dt):
        return (19, 0, 19, 30)
    else:
        return (20, 0, 20, 30)


def in_maintenance(ts):
    """判断时间戳是否在维护时段内。"""
    dt = datetime.fromtimestamp(ts, tz=CN_TZ)
    sh, sm, eh, em = get_maintenance_window(dt)
    cur_min = dt.hour * 60 + dt.minute
    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    return start_min <= cur_min < end_min


def count_maintenance_between(start_ts, end_ts):
    """计算 start_ts 到 end_ts 之间完整经过的维护时段数量。"""
    if start_ts > end_ts:
        start_ts, end_ts = end_ts, start_ts
    start_dt = datetime.fromtimestamp(start_ts, tz=CN_TZ)
    end_dt = datetime.fromtimestamp(end_ts, tz=CN_TZ)
    count = 0
    cur = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = end_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    while cur <= end_day:
        sh, sm, eh, em = get_maintenance_window(cur)
        m_start = cur.replace(hour=sh, minute=sm, second=0, microsecond=0)
        m_end = cur.replace(hour=eh, minute=em, second=0, microsecond=0)
        if m_end > start_dt and m_start < end_dt:
            count += 1
        cur += timedelta(days=1)
    return count


# ============ 期号计算 ============

def calc_countdown(ts):
    """根据时间戳计算当前期号和距下期更新秒数。

    优先使用参考点计算; 无参考点时回退到 BASE_EPOCH 公式。
    维护时段内期号冻结在维护前最后一期, remaining 为维护结束剩余秒数。
    """
    ref_nbr, ref_ts = get_reference()
    if ref_nbr is not None and ref_ts is not None:
        return _calc_with_ref(ts, ref_nbr, ref_ts)
    return _calc_raw(ts)


def _calc_raw(ts):
    """原始公式 (无参考点时回退)。"""
    elapsed = int(ts) - BASE_EPOCH
    if elapsed < 0:
        return 0, 0
    current_period = elapsed // CYCLE
    remaining = CYCLE - (elapsed % CYCLE)
    if remaining == CYCLE:
        remaining = 0
    return current_period, remaining


def _calc_with_ref(ts, ref_nbr, ref_ts):
    """用参考点 + 维护时段修正计算。"""
    # 维护时段内: 期号冻结在维护前最后一期
    if in_maintenance(ts):
        dt = datetime.fromtimestamp(ts, tz=CN_TZ)
        sh, sm, eh, em = get_maintenance_window(dt)
        maint_start_dt = dt.replace(hour=sh, minute=sm, second=0, microsecond=0)
        # 维护开始前1秒的期号 = 维护前最后一期
        frozen_nbr, _ = _calc_with_ref_raw(maint_start_dt.timestamp() - 1, ref_nbr, ref_ts)
        maint_end_dt = dt.replace(hour=eh, minute=em, second=0, microsecond=0)
        remaining = int(maint_end_dt.timestamp() - ts)
        return frozen_nbr, max(0, remaining)
    return _calc_with_ref_raw(ts, ref_nbr, ref_ts)


def _calc_with_ref_raw(ts, ref_nbr, ref_ts):
    """用参考点计算 (不考虑当前是否在维护时段)。

    支持向前 (ts > ref_ts) 和向后 (ts < ref_ts) 计算。
    """
    if ts == ref_ts:
        return ref_nbr, 0
    maint_count = count_maintenance_between(ref_ts, ts)
    maint_total = maint_count * MAINT_SECS
    if ts > ref_ts:
        # 向前计算
        adjusted = ts - ref_ts - maint_total
        if adjusted < 0:
            return ref_nbr, 0
        periods = int(adjusted) // CYCLE
        remaining = CYCLE - (int(adjusted) % CYCLE)
        if remaining == CYCLE:
            remaining = 0
        return ref_nbr + periods, remaining
    else:
        # 向后计算 (历史时间点)
        adjusted = ref_ts - ts - maint_total
        if adjusted < 0:
            return ref_nbr, 0
        periods = int(adjusted) // CYCLE
        return ref_nbr - periods, 0


# ============ 时钟同步 ============

def sync_time_offset():
    """从参考站 api.php 获取服务器时间, 计算本地时钟偏移。

    多次采样取最小 offset, 消除网络延迟带来的正向偏差。
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
                mid = (t1 + t2) / 2
                est_offset = ref_ts - mid
                samples.append(est_offset)
        except Exception as e:
            print(f"[time-sync] 第{i+1}次失败: {e}")
        if i < 4:
            time.sleep(0.5)
    if not samples:
        print("[time-sync] 全部失败, 保持原 offset")
        return
    best = min(samples)
    avg = sum(samples) / len(samples)
    with _offset_lock:
        _time_offset = best
    print(f"[time-sync] offset={best:+.3f}s (min of {len(samples)} samples, avg={avg:+.3f})")


def start_sync_loop(interval_sec=300):
    """启动后台线程, 每 interval_sec 秒同步一次时钟。"""
    def loop():
        while True:
            time.sleep(interval_sec)
            sync_time_offset()
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t
