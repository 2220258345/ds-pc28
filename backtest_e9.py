# -*- coding: utf-8 -*-
"""
PC28 E9 策略回测引擎 (完整版)
============================================================

一、游戏规则
  - 每 210 秒一期, 每期开奖 c1+c2+c3 (各 0-9), 和值 0-27
  - 投注两腿: 大小注 + 单双注 (同金额)
  - 大小: 和值 >= 14 为大, < 14 为小
  - 单双: 和值偶数为双, 奇数为单
  - 抽水: 和值 13/14 时, 赢方只得 98% (rate=0.98); 其余和值 100%
  - 单腿 > 5000 时, 所有和值赢方只得 90% (rate=0.9)

二、E9 预测规则
  - 大小: 用当期和值预测下期
    和值 <= 9  -> 预测下期"大"
    和值 >= 18 -> 预测下期"小"
    和值 10-17 -> 看首位 c1 (0-4 预测大, 5-9 预测小)
  - 单双: 看第二位 c2 奇偶 (偶数预测双, 奇数预测单)

三、梯度倍投 (2x 马丁格尔, 全额回收)
  - 梯度: [20, 40, 80, 160, 320, 640, 1280] (7级, 2倍递增)
  - 数学性质: 任意级双中 = 回收前面所有亏损 + 净赚 2×起始注(40)
  - 双中(两腿都对): 回底 Level 0
  - 双错(两腿都错): 升级 Level+1; 最高级双错 = 炸(回底)
  - 平局(一对一错): 保持当前级
  - 炸 = 连续 6 次双错 (概率 0.247^6 ≈ 0.02%)

四、风控
  - 止盈: 当天累计盈亏 >= 止盈阈值时停投至次日 (最优值 2500)
  - 止损: 经测试, 7级梯度下止损有害, 不设止损
  - 每日重置: 新的一天 Level 回 0, 日盈亏归零
  - 维护时段: 夏令时 19:00-19:33 / 冬令时 20:00-20:33 不开奖
  - 18:00-18:50 双中 -> 暂停至 19:40

五、关键回测结论 (31244期, 79天)
  - 7级起20 + 止盈2500: 盈亏 +108,035, 回撤 11,144, 收益/回撤 9.69
  - 双中率 25.4%, 平局率 49.7%, 双错率 24.9%
  - 炸次 21 (6级原版 93次), 盈利天62 / 亏损天17
  - 最大单注 1,280, 资金需求约 2,560

用法:
  python backtest_e9.py              # 全量回测 + 每日统计HTML
  python backtest_e9.py --today      # 今日逐期明细HTML
  python backtest_e9.py --stops      # 止盈止损网格扫描
"""
import sys, os, sqlite3, argparse
from datetime import datetime

# ============================================================
# 配置区
# ============================================================
DB_PATH = os.path.join(os.environ.get("DB_DIR", os.path.dirname(os.path.abspath(__file__))), "pc28_history.db")

# 梯度 (7级, 2x全额回收, 最大注1280, 资金需求~2560)
LADDER = [20, 40, 80, 160, 320, 640, 1280]

# 风控
STOP_PROFIT = 2500     # 止盈 (None=不设)
STOP_LOSS = None       # 止损 (None=不设, 经测试7级下止损有害)

# 游戏规则常量
BIG_THRESHOLD = 14          # 和值 >= 14 为大
COMMISSION_SUMS = (13, 14)  # 这些和值赢方抽水
COMMISSION_RATE = 0.98      # 抽水比例 (2%)
HIGH_BET_THRESHOLD = 5000   # 单腿超过此值, 所有和值抽水
HIGH_BET_RATE = 0.90        # 高注抽水比例 (10%)

TODAY = "2026-08-11"  # --today 模式的日期


# ============================================================
# 核心逻辑
# ============================================================

def predict_e9(c1, c2, c3, amount):
    """E9预测: 大小看和值三段, 单双看c2奇偶。"""
    s = c1 + c2 + c3
    pdx = "大" if s <= 9 else ("小" if s >= 18 else ("大" if c1 <= 4 else "小"))
    pds = "双" if c2 % 2 == 0 else "单"
    return f"{pdx}{amount}", f"{pds}{amount}", pdx, pds


def actual_result(c1, c2, c3):
    """结算: 返回 (实际大小, 实际单双, 是否抽水)。"""
    s = c1 + c2 + c3
    adx = "大" if s >= BIG_THRESHOLD else "小"
    ads = "双" if s % 2 == 0 else "单"
    return adx, ads, s in COMMISSION_SUMS


def get_rate(amount, comm):
    """获取抽水比例: 单腿>5000所有和值抽10%, 否则仅13/14抽2%。"""
    if amount > HIGH_BET_THRESHOLD:
        return HIGH_BET_RATE
    return COMMISSION_RATE if comm else 1.0


def in_maintenance(dt):
    """维护时段: 夏令时19:00-19:33, 冬令时20:00-20:33。"""
    year = dt.year
    mar1 = datetime(year, 3, 1)
    dst_start_day = 1 + (6 - mar1.weekday()) % 7 + 7
    dst_start = datetime(year, 3, dst_start_day)
    nov1 = datetime(year, 11, 1)
    dst_end_day = 1 + (6 - nov1.weekday()) % 7
    dst_end = datetime(year, 11, dst_end_day)
    is_summer = dst_start <= dt < dst_end
    sh, sm, eh, em = (19, 0, 19, 33) if is_summer else (20, 0, 20, 33)
    cur = dt.hour * 60 + dt.minute
    return sh * 60 + sm <= cur < eh * 60 + em


def load_draws(filter_date=None):
    """从 SQLite 读取开奖数据, 返回 [(期号, 日期, 时间, c1, c2, c3, 和值), ...] 旧->新。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        if filter_date:
            cur = conn.execute(
                "SELECT draw_nbr, draw_date, draw_time, c1, c2, c3, draw_num "
                "FROM draws WHERE draw_date = ? ORDER BY draw_nbr ASC", (filter_date,))
        else:
            cur = conn.execute(
                "SELECT draw_nbr, draw_date, draw_time, c1, c2, c3, draw_num "
                "FROM draws ORDER BY draw_nbr ASC")
        rows = [tuple(r) for r in cur.fetchall()]
    finally:
        conn.close()
    return rows


# ============================================================
# 回测引擎
# ============================================================

def run_backtest(draws, ladder=LADDER, stop_profit=STOP_PROFIT, stop_loss=STOP_LOSS,
                 detail=False, reverse=False):
    """
    回测引擎。
    每期流程: 结算上期待结算注单 -> 检查风控 -> 预测下期并下注。
    reverse=True 时反向投注 (押 E9 预测的反面)。
    返回: dict(总盈亏, 回撤, 每日统计, [逐期明细])
    """
    level = 0
    daily_pnl = 0
    cur_date = None
    pause_until_dt = None
    pending = None  # (pdx, pds, amount, level)
    total_pnl = 0
    total_bets = 0
    max_pnl = 0
    max_drawdown = 0
    profit_days = 0
    loss_days = 0
    bursts = 0
    t_win = t_flat = t_lose = 0
    daily = {}
    events = [] if detail else None
    burst_chains = []   # [(start_event_idx, burst_event_idx), ...]
    chain_start_idx = None
    event_count = 0

    for i in range(len(draws)):
        period, date, time_str, c1, c2, c3, total = draws[i]
        adx, ads, comm = actual_result(c1, c2, c3)
        combo = f"{adx}{ads}"

        if date != cur_date:
            cur_date = date
            if daily_pnl > 0: profit_days += 1
            elif daily_pnl < 0: loss_days += 1
            daily_pnl = 0
            level = 0
            pause_until_dt = None
            daily.setdefault(date, {"pnl": 0, "bets": 0, "win": 0, "flat": 0, "lose": 0,
                                    "max_level": 0, "max_pnl": 0, "min_pnl": 0, "bursts": 0})
        day = daily[date]

        # --- 1. 结算上期 ---
        s_pred = s_amt = s_pnl = s_result = s_note = ""
        if pending is not None:
            p_dx, p_ds, amount, p_level = pending
            rate = get_rate(amount, comm)
            dx_ok = p_dx == adx
            ds_ok = p_ds == ads
            win = amount * rate

            if dx_ok and ds_ok:
                pnl = round(win * 2)
                level = 0
                s_result = "双中"
                t_win += 1
                day["win"] += 1
                s_note = f"双中 -> 回{ladder[0]}"
                chain_start_idx = None  # 双中重置链
            elif not dx_ok and not ds_ok:
                pnl = -amount * 2
                t_lose += 1
                day["lose"] += 1
                if p_level == 0:
                    chain_start_idx = event_count  # L0双错, 链起点
                if p_level >= len(ladder) - 1:
                    level = 0
                    bursts += 1
                    day["bursts"] += 1
                    s_result = "双错💥炸"
                    s_note = f"{amount}双错爆 -> 回{ladder[0]}"
                    if chain_start_idx is not None:
                        burst_chains.append((chain_start_idx, event_count))
                    chain_start_idx = None
                else:
                    level = p_level + 1
                    s_result = "双错"
                    s_note = f"双错 -> {ladder[level]}"
            else:
                pnl = round((win if dx_ok else -amount) + (win if ds_ok else -amount))
                s_result = "平局"
                t_flat += 1
                day["flat"] += 1
                s_note = f"平局 -> 保持{amount}"

            daily_pnl += pnl
            total_pnl += pnl
            day["pnl"] += pnl
            max_pnl = max(max_pnl, total_pnl)
            max_drawdown = max(max_drawdown, max_pnl - total_pnl)

            s_pred = f"{p_dx}{p_ds}"
            s_amt = str(amount)
            s_pnl = f"{pnl:+d}"

            # 18:00-18:50 双中暂停至19:40
            if dx_ok and ds_ok:
                t = datetime.strptime(time_str, "%H:%M:%S")
                cm = t.hour * 60 + t.minute
                if 18 * 60 <= cm <= 18 * 60 + 50:
                    pause_until_dt = datetime.strptime(date + " 19:40:00", "%Y-%m-%d %H:%M:%S")
                    s_note += " ->暂停至19:40"
            pending = None

        # --- 2. 检查是否下注 ---
        b_pred = b_amt = b_level = b_note = ""
        row_cls = ""
        dt = datetime.strptime(date + " " + time_str, "%Y-%m-%d %H:%M:%S")

        if s_result:  # 有结算结果, 按结果着色
            if "炸" in s_result:
                row_cls = "burst"
            elif s_result == "双中":
                row_cls = "win"
            elif "双错" in s_result:
                row_cls = "lose"
            else:
                row_cls = "flat"

        if in_maintenance(dt):
            b_note = "维护时段"
            if not row_cls: row_cls = "skip"
        elif pause_until_dt and dt < pause_until_dt:
            b_note = "双中暂停中"
            if not row_cls: row_cls = "skip"
        elif stop_loss is not None and daily_pnl <= stop_loss:
            b_note = f"止损{stop_loss}停投"
            if not row_cls: row_cls = "skip"
        elif stop_profit is not None and daily_pnl >= stop_profit:
            b_note = f"止盈+{stop_profit}停投"
            if not row_cls: row_cls = "skip"
        else:
            amount = ladder[level]
            dx_str, ds_str, pdx, pds = predict_e9(c1, c2, c3, amount)
            if reverse:
                pdx = "小" if pdx == "大" else "大"
                pds = "单" if pds == "双" else "双"
                dx_str = f"{pdx}{amount}"
                ds_str = f"{pds}{amount}"
            b_pred = f"{pdx}{pds}"
            b_amt = str(amount)
            b_level = f"L{level}"
            b_note = "预测下期"
            total_bets += 1
            day["bets"] += 1
            day["max_level"] = max(day["max_level"], level)
            day["max_pnl"] = max(day["max_pnl"], daily_pnl)
            day["min_pnl"] = min(day["min_pnl"], daily_pnl)
            pending = (pdx, pds, amount, level)
            if not row_cls: row_cls = "bet"

        if events is not None:
            events.append({
                "idx": i + 1, "period": period, "date": date, "time": time_str,
                "draw": f"{c1}+{c2}+{c3}={total}", "actual": f"{adx}{ads}", "comm": comm,
                "s_pred": s_pred, "s_amt": s_amt, "s_pnl": s_pnl, "s_result": s_result, "s_note": s_note,
                "b_pred": b_pred, "b_amt": b_amt, "b_level": b_level, "b_note": b_note,
                "row_cls": row_cls, "daily_pnl": daily_pnl,
            })
            event_count += 1

    if daily_pnl > 0: profit_days += 1
    elif daily_pnl < 0: loss_days += 1

    # 标记炸链: 起点 -> 全程 -> 终点, 统一红色
    if events is not None:
        for start_idx, burst_idx in burst_chains:
            for j in range(start_idx, burst_idx + 1):
                if j < len(events):
                    events[j]["row_cls"] = "burst"

    return {
        "total_pnl": total_pnl, "max_drawdown": max_drawdown,
        "ratio": total_pnl / max_drawdown if max_drawdown > 0 else 0,
        "total_bets": total_bets, "profit_days": profit_days, "loss_days": loss_days,
        "bursts": bursts, "win": t_win, "flat": t_flat, "lose": t_lose,
        "daily": daily, "events": events, "final_level": level,
    }


# ============================================================
# 输出: 每日统计 HTML
# ============================================================

def generate_daily_html(r, draws, filename, title_suffix=""):
    """生成每日统计 HTML。"""
    daily = r["daily"]
    all_dates = sorted(daily)
    rows_html = []
    cum = 0
    for d in all_dates:
        info = daily[d]
        cum += info["pnl"]
        cls = "pos" if info["pnl"] > 0 else "neg" if info["pnl"] < 0 else ""
        cum_cls = "pos" if cum >= 0 else "neg"
        burst_cls = "neg" if info["bursts"] >= 2 else "warn" if info["bursts"] == 1 else ""
        row_cls = "burst2" if info["bursts"] >= 2 else "burst1" if info["bursts"] == 1 else \
                  ("posrow" if info["pnl"] > 0 else "negrow" if info["pnl"] < 0 else "")
        rows_html.append(
            f"<tr class='{row_cls}'>"
            f"<td class='date'>{d}</td>"
            f"<td class='{cls}'>{info['pnl']:+,}</td>"
            f"<td>{info['bets']}</td>"
            f"<td class='pos'>{info['win']}</td>"
            f"<td class='flat'>{info['flat']}</td>"
            f"<td class='neg'>{info['lose']}</td>"
            f"<td>L{info['max_level']}</td>"
            f"<td class='{burst_cls}'>{info['bursts']}</td>"
            f"<td class='pos'>{info['max_pnl']:+,}</td>"
            f"<td class='neg'>{info['min_pnl']:+,}</td>"
            f"<td class='{cum_cls}'>{cum:+,}</td>"
            f"</tr>"
        )

    t_pnl = r["total_pnl"]
    t_bets = r["total_bets"]
    t_win = r["win"]
    t_flat = r["flat"]
    t_lose = r["lose"]
    t_bursts = r["bursts"]
    sp_str = f"+{STOP_PROFIT}" if STOP_PROFIT else "无"
    sl_str = f"{STOP_LOSS}" if STOP_LOSS else "无"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>E9 回测 {title_suffix}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,"Microsoft YaHei",monospace; background:#0f1117; color:#e0e0e0; padding:20px; }}
  h1 {{ text-align:center; font-size:22px; margin-bottom:4px; color:#fff; }}
  .sub {{ text-align:center; font-size:13px; color:#888; margin-bottom:16px; }}
  .info {{ background:#1a1d29; border-radius:10px; padding:12px 16px; margin-bottom:16px; font-size:13px; line-height:1.8; color:#aaa; }}
  .summary {{ display:flex; gap:10px; margin-bottom:16px; flex-wrap:wrap; justify-content:center; }}
  .s-card {{ background:#1a1d29; border-radius:8px; padding:10px 16px; text-align:center; min-width:100px; }}
  .s-card .label {{ font-size:11px; color:#888; margin-bottom:3px; }}
  .s-card .val {{ font-size:18px; font-weight:bold; }}
  .pos {{ color:#91cc75; }} .neg {{ color:#ee6666; }} .flat {{ color:#fac858; }} .warn {{ color:#fac858; }}
  .table-wrap {{ background:#1a1d29; border-radius:10px; overflow:hidden; }}
  table {{ width:100%; border-collapse:collapse; font-size:12px; }}
  thead {{ position:sticky; top:0; z-index:10; }}
  th {{ background:#252836; padding:8px 5px; color:#aaa; font-weight:600; white-space:nowrap; border-bottom:2px solid #333; }}
  td {{ padding:5px 5px; border-bottom:1px solid #1e2130; white-space:nowrap; text-align:center; }}
  tr:hover {{ background:#1e2130; }}
  tr.burst2 {{ background:rgba(238,102,102,0.12); }}
  tr.burst1 {{ background:rgba(250,200,88,0.05); }}
  tr.posrow {{ background:rgba(145,204,117,0.03); }}
  tr.negrow {{ background:rgba(238,102,102,0.03); }}
  .date {{ font-family:Consolas,monospace; text-align:left; }}
  .footer {{ background:#252836; font-weight:bold; }}
  .footer td {{ padding:8px 5px; border-top:2px solid #333; border-bottom:none; }}
</style>
</head>
<body>
<h1>E9 7级起20 每日统计{title_suffix}</h1>
<p class="sub">{all_dates[0]} ~ {all_dates[-1]} | {len(all_dates)}天 | {len(draws)}期</p>
<div class="info">
  <b style="color:#ccc;">配置:</b> 梯度 {LADDER} (2x全额回收, 最大注{LADDER[-1]}) | 止盈{sp_str} 止损{sl_str} | E9预测<br>
  <b style="color:#ccc;">回收:</b> 任意级双中=回收前亏+净赚{LADDER[0]*2} | 炸需6连错 | 资金需求~{LADDER[-1]*2}
</div>
<div class="summary">
  <div class="s-card"><div class="label">总盈亏</div><div class="val {'pos' if t_pnl>=0 else 'neg'}">{t_pnl:+,}</div></div>
  <div class="s-card"><div class="label">最大回撤</div><div class="val neg">{r['max_drawdown']:,}</div></div>
  <div class="s-card"><div class="label">收益/回撤</div><div class="val pos">{r['ratio']:.2f}</div></div>
  <div class="s-card"><div class="label">总下注</div><div class="val">{t_bets:,}</div></div>
  <div class="s-card"><div class="label">双中</div><div class="val pos">{t_win}</div></div>
  <div class="s-card"><div class="label">平局</div><div class="val flat">{t_flat}</div></div>
  <div class="s-card"><div class="label">双错</div><div class="val neg">{t_lose}</div></div>
  <div class="s-card"><div class="label">炸次</div><div class="val neg">{t_bursts}</div></div>
  <div class="s-card"><div class="label">盈利天</div><div class="val pos">{r['profit_days']}</div></div>
  <div class="s-card"><div class="label">亏损天</div><div class="val neg">{r['loss_days']}</div></div>
</div>
<div class="table-wrap">
<table>
<thead><tr>
  <th class="date">日期</th><th>日盈亏</th><th>下注</th><th>双中</th><th>平局</th><th>双错</th>
  <th>最高档</th><th>炸次</th><th>日最高</th><th>日最低</th><th>累计盈亏</th>
</tr></thead>
<tbody>
{chr(10).join(rows_html)}
<tr class="footer">
  <td class="date">总计</td>
  <td class="{'pos' if t_pnl>=0 else 'neg'}">{t_pnl:+,}</td>
  <td>{t_bets:,}</td><td class="pos">{t_win}</td><td class="flat">{t_flat}</td><td class="neg">{t_lose}</td>
  <td>-</td><td class="neg">{t_bursts}</td><td>-</td><td>-</td>
  <td class="{'pos' if t_pnl>=0 else 'neg'}">{t_pnl:+,}</td>
</tr>
</tbody>
</table>
</div>
</body>
</html>"""

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML 已生成: {out}")


# ============================================================
# 输出: 逐期明细 HTML
# ============================================================

def generate_detail_html(r, draws, filename, title_suffix=""):
    """生成逐期明细 HTML。"""
    events = r["events"]
    rows_html = []
    for ev in events:
        cls = ev["row_cls"]
        pnl_cls = "pos" if ev["s_pnl"] and ev["s_pnl"][0] == "+" else "neg" if ev["s_pnl"] else ""
        cum_cls = "pos" if ev["daily_pnl"] >= 0 else "neg"
        comm_tag = " <span class='comm'>(抽水)</span>" if ev["comm"] else ""
        b_pred = ev["b_pred"] if ev["b_pred"] else "-"
        b_note = ev["b_note"] if ev["b_note"] else ""
        s_note = ev["s_note"] if ev["s_note"] else ""
        note = f"{s_note}{' | ' if s_note and b_note else ''}{b_note}"

        rows_html.append(
            f"<tr class='{cls}'>"
            f"<td>{ev['idx']}</td><td>{ev['period']}</td><td>{ev['time']}</td>"
            f"<td class='num'>{ev['draw']}{comm_tag}</td>"
            f"<td>{ev['actual']}</td>"
            f"<td class='pred'>{ev['s_pred']}</td><td>{ev['s_amt']}</td>"
            f"<td class='pnl {pnl_cls}'>{ev['s_pnl']}</td>"
            f"<td>{ev['s_result']}</td>"
            f"<td class='pred'>{b_pred}</td><td>{ev['b_amt']}</td><td>{ev['b_level']}</td>"
            f"<td class='cum {cum_cls}'>{ev['daily_pnl']:+d}</td>"
            f"<td class='note'>{note}</td>"
            f"</tr>"
        )

    sp_str = f"+{STOP_PROFIT}" if STOP_PROFIT else "无"
    t_pnl = r["total_pnl"]

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>E9 逐期明细 {title_suffix}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family:-apple-system,"Microsoft YaHei",monospace; background:#0f1117; color:#e0e0e0; padding:20px; }}
  h1 {{ text-align:center; font-size:22px; margin-bottom:4px; color:#fff; }}
  .sub {{ text-align:center; font-size:13px; color:#888; margin-bottom:16px; }}
  .summary {{ display:flex; gap:10px; margin-bottom:16px; flex-wrap:wrap; justify-content:center; }}
  .s-card {{ background:#1a1d29; border-radius:8px; padding:10px 16px; text-align:center; min-width:90px; }}
  .s-card .label {{ font-size:11px; color:#888; margin-bottom:3px; }}
  .s-card .val {{ font-size:18px; font-weight:bold; }}
  .pos {{ color:#91cc75; }} .neg {{ color:#ee6666; }}
  .table-wrap {{ background:#1a1d29; border-radius:10px; overflow:hidden; }}
  table {{ width:100%; border-collapse:collapse; font-size:11px; }}
  thead {{ position:sticky; top:0; z-index:10; }}
  th {{ background:#252836; padding:7px 4px; color:#aaa; font-weight:600; white-space:nowrap; border-bottom:2px solid #333; }}
  th.grp {{ border-left:1px solid #333; }}
  td {{ padding:3px 4px; border-bottom:1px solid #1e2130; white-space:nowrap; text-align:center; }}
  tr:hover {{ background:#1e2130; }}
  tr.win {{ background:rgba(145,204,117,0.06); }}
  tr.lose {{ background:rgba(238,102,102,0.06); }}
  tr.burst {{ background:rgba(238,102,102,0.18); border-left:3px solid #ff4444; }}
  tr.burst td {{ font-weight:bold; }}
  tr.bet td {{ color:#aaa; }}
  tr.skip td {{ color:#555; }}
  .num {{ font-family:Consolas,monospace; }}
  .pred {{ color:#5470c6; font-weight:bold; }}
  .pnl {{ font-weight:bold; }}
  .cum {{ font-family:Consolas,monospace; font-weight:bold; }}
  .note {{ color:#888; font-size:10px; max-width:160px; }}
  .comm {{ color:#fac858; font-size:9px; }}
</style>
</head>
<body>
<h1>E9 7级起20 逐期明细{title_suffix}</h1>
<p class="sub">{events[0]['date'] if events else ''} | {len(events)}期 | 梯度{LADDER} | 止盈{sp_str}</p>
<div class="summary">
  <div class="s-card"><div class="label">总盈亏</div><div class="val {'pos' if t_pnl>=0 else 'neg'}">{t_pnl:+,}</div></div>
  <div class="s-card"><div class="label">下注</div><div class="val">{r['total_bets']}</div></div>
  <div class="s-card"><div class="label">双中</div><div class="val pos">{r['win']}</div></div>
  <div class="s-card"><div class="label">平局</div><div class="val" style="color:#fac858">{r['flat']}</div></div>
  <div class="s-card"><div class="label">双错</div><div class="val neg">{r['lose']}</div></div>
  <div class="s-card"><div class="label">炸次</div><div class="val neg">{r['bursts']}</div></div>
  <div class="s-card"><div class="label">档位</div><div class="val">L{r['final_level']}</div></div>
</div>
<div class="table-wrap">
<table>
<thead>
<tr>
  <th rowspan="2">#</th><th rowspan="2">期号</th><th rowspan="2">时间</th>
  <th rowspan="2">开奖</th><th rowspan="2">实大单</th>
  <th colspan="4" class="grp">结算上期</th>
  <th colspan="3" class="grp">下注下期</th>
  <th rowspan="2">当天累计</th><th rowspan="2">备注</th>
</tr>
<tr>
  <th class="grp">预测</th><th>金额</th><th>盈亏</th><th>结果</th>
  <th class="grp">预测</th><th>金额</th><th>档位</th>
</tr>
</thead>
<tbody>
{chr(10).join(rows_html)}
</tbody>
</table>
</div>
</body>
</html>"""

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML 已生成: {out}")


# ============================================================
# 止盈止损网格扫描
# ============================================================

def run_stops_grid(draws):
    """止盈止损网格扫描, 输出排行榜HTML。"""
    stop_losses = [None, -1000, -1500, -2000, -2500, -3000, -4000, -5000]
    stop_profits = [None, 1000, 1500, 2000, 2500, 3000, 4000, 5000]

    grid = {}
    all_r = []
    for sl in stop_losses:
        for sp in stop_profits:
            r = run_backtest(draws, stop_profit=sp, stop_loss=sl)
            sl_name = str(sl) if sl else "无"
            sp_name = f"+{sp}" if sp else "无"
            r["sl"] = sl_name
            r["sp"] = sp_name
            grid[(sl, sp)] = r
            all_r.append(r)

    # 打印网格
    print("\n总盈亏网格 (行=止损, 列=止盈):")
    label = "止损\\止盈"
    header = f"{label:>10}" + "".join(f"{sp or '无':>8}" for sp in stop_profits)
    print(header)
    print("-" * len(header))
    for sl in stop_losses:
        row = f"{str(sl) if sl else '无':>10}"
        for sp in stop_profits:
            row += f"{grid[(sl, sp)]['total_pnl']:>+8,}"
        print(row)

    print("\n收益/回撤比网格:")
    print(header)
    print("-" * len(header))
    for sl in stop_losses:
        row = f"{str(sl) if sl else '无':>10}"
        for sp in stop_profits:
            row += f"{grid[(sl, sp)]['ratio']:>8.2f}"
        print(row)

    # 排行榜
    by_pnl = sorted(all_r, key=lambda x: x["total_pnl"], reverse=True)
    by_ratio = sorted(all_r, key=lambda x: x["ratio"], reverse=True)

    print(f"\n排行榜 TOP 10 (按收益/回撤比):")
    for i, r in enumerate(by_ratio[:10]):
        print(f"  {i+1}. 止损{r['sl']} 止盈{r['sp']}  盈亏{r['total_pnl']:+,} 回撤{r['max_drawdown']:,} 比值{r['ratio']:.2f} 炸{r['bursts']}")

    # 生成HTML
    def make_rows(lst, n=20):
        rows = []
        for i, r in enumerate(lst[:n]):
            cls = "pos" if r["total_pnl"] >= 0 else "neg"
            rows.append(
                f"<tr class='{cls}'><td>{i+1}</td><td>{r['sl']}</td><td>{r['sp']}</td>"
                f"<td class='{cls}'>{r['total_pnl']:+,}</td><td class='neg'>{r['max_drawdown']:,}</td>"
                f"<td>{r['ratio']:.2f}</td><td>{r['bursts']}</td>"
                f"<td class='pos'>{r['profit_days']}</td><td class='neg'>{r['loss_days']}</td></tr>"
            )
        return "".join(rows)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>E9 止盈止损网格</title>
<style>
  *{{margin:0;padding:0;box-sizing:border-box;}}
  body{{font-family:-apple-system,"Microsoft YaHei",monospace;background:#0f1117;color:#e0e0e0;padding:20px;}}
  h1{{text-align:center;font-size:22px;margin-bottom:4px;color:#fff;}}
  h2{{font-size:16px;margin:16px 0 8px;color:#ccc;}}
  .sub{{text-align:center;font-size:13px;color:#888;margin-bottom:16px;}}
  .pos{{color:#91cc75;}}.neg{{color:#ee6666;}}
  .table-wrap{{background:#1a1d29;border-radius:10px;overflow:hidden;margin-bottom:20px;}}
  table{{width:100%;border-collapse:collapse;font-size:12px;}}
  thead{{position:sticky;top:0;z-index:10;}}
  th{{background:#252836;padding:8px 5px;color:#aaa;white-space:nowrap;}}
  td{{padding:5px;border-bottom:1px solid #1e2130;text-align:center;white-space:nowrap;}}
  tr:hover{{background:#1e2130;}}
  tr.pos{{background:rgba(145,204,117,0.04);}}
  tr.neg{{background:rgba(238,102,102,0.04);}}
</style>
</head>
<body>
<h1>E9 7级起20 止盈止损网格</h1>
<p class="sub">{len(all_r)}种组合 | 梯度{LADDER} | {len(draws)}期</p>
<h2>按总盈亏 TOP 20</h2>
<div class="table-wrap"><table>
<thead><tr><th>#</th><th>止损</th><th>止盈</th><th>总盈亏</th><th>回撤</th><th>比值</th><th>炸</th><th>盈天</th><th>亏天</th></tr></thead>
<tbody>{make_rows(by_pnl, 20)}</tbody>
</table></div>
<h2>按收益/回撤比 TOP 20</h2>
<div class="table-wrap"><table>
<thead><tr><th>#</th><th>止损</th><th>止盈</th><th>总盈亏</th><th>回撤</th><th>比值</th><th>炸</th><th>盈天</th><th>亏天</th></tr></thead>
<tbody>{make_rows(by_ratio, 20)}</tbody>
</table></div>
</body>
</html>"""

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_stops_grid.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nHTML 已生成: {out}")


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="PC28 E9 策略回测")
    parser.add_argument("--today", action="store_true", help="生成今日逐期明细")
    parser.add_argument("--stops", action="store_true", help="止盈止损网格扫描")
    parser.add_argument("--date", default=TODAY, help="--today模式的日期 (默认2026-08-08)")
    args = parser.parse_args()

    if args.stops:
        draws = load_draws()
        print(f"已加载 {len(draws)} 期数据")
        run_stops_grid(draws)
        return

    if args.today:
        draws = load_draws(args.date)
        print(f"已加载 {args.date} 数据: {len(draws)} 期")
        r = run_backtest(draws, detail=True)
        print(f"总盈亏: {r['total_pnl']:+,}  双中/平局/双错: {r['win']}/{r['flat']}/{r['lose']}  "
              f"炸: {r['bursts']}  下注: {r['total_bets']}  档位: L{r['final_level']}")
        generate_detail_html(r, draws, "backtest_today.html", f" {args.date}")
        return

    # 默认: 全量回测 + 每日统计
    draws = load_draws()
    print(f"已加载 {len(draws)} 期数据")
    r = run_backtest(draws)
    settled = r["win"] + r["flat"] + r["lose"]
    print(f"\n{'=' * 60}")
    print(f"  E9 7级起20 回测结果")
    print(f"{'=' * 60}")
    print(f"  梯度:       {LADDER}")
    print(f"  止盈:       {'+' + str(STOP_PROFIT) if STOP_PROFIT else '无'}")
    print(f"  止损:       {STOP_LOSS or '无'}")
    print(f"  总盈亏:     {r['total_pnl']:+,}")
    print(f"  最大回撤:   {r['max_drawdown']:,}")
    print(f"  收益/回撤:  {r['ratio']:.2f}")
    print(f"  下注期数:   {r['total_bets']:,}")
    print(f"  双中率:     {r['win']/settled*100:.1f}% ({r['win']})")
    print(f"  平局率:     {r['flat']/settled*100:.1f}% ({r['flat']})")
    print(f"  双错率:     {r['lose']/settled*100:.1f}% ({r['lose']})")
    print(f"  炸次:       {r['bursts']}")
    print(f"  盈天/亏天:  {r['profit_days']}/{r['loss_days']}")
    print(f"  最终档位:   L{r['final_level']} ({LADDER[r['final_level']]})")
    print(f"{'=' * 60}")
    generate_daily_html(r, draws, "backtest_daily.html")


if __name__ == "__main__":
    main()
