# -*- coding: utf-8 -*-
"""
PC28 方法搜索: 探索比 E9 更好的预测/投注方法
============================================================
核心问题: 3万期数据里是否存在可被利用的结构?

流程:
  1. 基准: E9 及静态组合策略的胜率/EV/显著性
  2. 上下文搜索: 历史特征 -> 下期大小/单双的条件概率, 多重比较校正
  3. 训练/测试切分: 样本内选规则, 样本外验证 (防过拟合)
  4. 洗牌对照: 打乱数据重跑 E9 倍投, 展示收益的方差本质

用法:
  python strategy_search.py
"""
import os
import sqlite3
import numpy as np
import pandas as pd
from collections import Counter

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pc28_history.db")
COMMISSION_SUMS = (13, 14)


# ============================================================
# 数据加载
# ============================================================

def load():
    """从 SQLite 读取开奖数据 (旧 -> 新)。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(
            "SELECT draw_nbr, draw_date, draw_time, c1, c2, c3, draw_num "
            "FROM draws ORDER BY draw_nbr ASC", conn)
    finally:
        conn.close()
    df["s"] = df["c1"] + df["c2"] + df["c3"]
    df["big"] = (df["s"] >= 14).astype(int)
    df["even"] = (df["s"] % 2 == 0).astype(int)
    df["combo"] = df["big"] * 2 + df["even"]  # 0小单 1小双 2大单 3大双
    for col, name in [("big", "streak_b"), ("even", "streak_e")]:
        v = df[col].values
        streak = np.zeros(len(df), int)
        cnt = 0
        for i in range(len(df)):
            cnt = cnt + 1 if i > 0 and v[i] == v[i - 1] else 1
            streak[i] = cnt
        df[name] = streak
    return df


# ============================================================
# 评估工具
# ============================================================

def evaluate(pred_big, pred_even, act_big, act_even, act_sum):
    """返回 (双中, 平局, 双错, 双中-双错, EV/腿金额, z值)。"""
    n = len(pred_big)
    w = (pred_big == act_big) & (pred_even == act_even)
    l = (pred_big != act_big) & (pred_even != act_even)
    f = ~w & ~l
    # 抽水: 双中且和值13/14 时赢方仅98%
    comm = w & np.isin(act_sum, COMMISSION_SUMS)
    pw, pl = w.mean(), l.mean()
    ev = 2 * (pw - pl) - 0.04 * comm.mean()
    z = (pw - pl) / np.sqrt(0.375 / n) if n > 1 else 0.0
    return pw, f.mean(), pl, pw - pl, ev, z, w.sum(), l.sum()


def fmt_res(name, r):
    pw, pf, pl, gap, ev, z, nw, nl = r
    return (f"{name:<26s} 双中{pw:6.3f} 平局{pf:6.3f} 双错{pl:6.3f} "
            f"差{gap:+6.3f} (z={z:+5.2f}) EV/腿{ev:+.4f}  ({nw}/{nl})")


def context_stats(keys, df, label):
    """keys: 长度 n-1 的上下文标签数组。返回行列表 (ctx, n, p_big, p_even, z_big, z_even)。"""
    rows = []
    nxt_b = df["big"].iloc[1:].values
    nxt_e = df["even"].iloc[1:].values
    for key in sorted(set(keys.tolist())):
        m = keys == key
        nb = m.sum()
        if nb < 30:
            continue
        pb = nxt_b[m].mean()
        pe = nxt_e[m].mean()
        zb = (pb - 0.5) / np.sqrt(0.25 / nb)
        ze = (pe - 0.5) / np.sqrt(0.25 / nb)
        rows.append((key, nb, pb, pe, zb, ze))
    return rows


def print_ctx_table(rows, title, n_contexts, max_rows=12):
    print(f"\n== {title} (共{len(rows)}组, Bonferroni 阈值 z>{3.3:.2f}) ==")
    rows_sorted = sorted(rows, key=lambda r: max(abs(r[4]), abs(r[5])), reverse=True)
    print(f"{'上下文':<24s} {'n':>6s} {'P(大)':>7s} {'P(双)':>7s} {'z大':>6s} {'z双':>6s}")
    for key, nb, pb, pe, zb, ze in rows_sorted[:max_rows]:
        print(f"{str(key):<24s} {nb:6d} {pb:7.3f} {pe:7.3f} {zb:+6.2f} {ze:+6.2f}")
    n_sig = sum(1 for r in rows if max(abs(r[4]), abs(r[5])) > 3.3)
    print(f"  -> 超过 Bonferroni 阈值的组数: {n_sig} / {n_contexts}")


# ============================================================
# 策略定义 (输入 df -> 预测下一期大小/单双的数组)
# ============================================================

def rule_e9(df):
    cur = df.iloc[:-1]
    pb = np.where(cur["s"] <= 9, 1, np.where(cur["s"] >= 18, 0, np.where(cur["c1"] <= 4, 1, 0)))
    pe = (cur["c2"] % 2 == 0).astype(int)
    return pb, pe


def rule_follow(df):
    return df["big"].iloc[:-1].values.copy(), df["even"].iloc[:-1].values.copy()


def rule_reverse(df):
    return 1 - df["big"].iloc[:-1].values, 1 - df["even"].iloc[:-1].values


def rule_fixed(b, e):
    def f(df):
        n = len(df) - 1
        return np.full(n, b), np.full(n, e)
    return f


def rule_lookup(df, train_mask, ctx_key, nbins, smooth=5):
    """训练集上学条件概率, 测试集上预测。ctx_key: 's_bucket' | 'c1' | 's_c2' 等。"""
    n = len(df)
    cur = df.iloc[:-1].reset_index(drop=True)
    nxt = df.iloc[1:].reset_index(drop=True)
    tr = train_mask[:-1]
    te = ~tr

    if ctx_key == "s_bucket":
        k_tr = np.minimum(cur["s"].values // nbins, 28 // nbins)
    elif ctx_key == "s_exact":
        k_tr = cur["s"].values
    elif ctx_key == "c1_c2":
        k_tr = cur["c1"].values * 10 + cur["c2"].values
    else:
        raise ValueError(ctx_key)

    stat_b = {}
    stat_e = {}
    for k in np.unique(k_tr[tr]):
        m = tr & (k_tr == k)
        stat_b[k] = (nxt["big"].values[m].sum() + smooth) / (m.sum() + 2 * smooth)
        stat_e[k] = (nxt["even"].values[m].sum() + smooth) / (m.sum() + 2 * smooth)
    k_te = k_tr[te]
    pb = np.array([1 if stat_b.get(k, 0.5) > 0.5 else 0 for k in k_te])
    pe = np.array([1 if stat_e.get(k, 0.5) > 0.5 else 0 for k in k_te])
    return pb, pe


def rule_best_ctx_train_only(df, train_mask, ctx_key):
    """样本内找最强上下文并用于测试集 (演示过拟合)。非匹配上下文回退 E9。"""
    n = len(df)
    cur = df.iloc[:-1].reset_index(drop=True)
    nxt = df.iloc[1:].reset_index(drop=True)
    tr = train_mask[:-1]
    te = ~tr
    if ctx_key == "s_exact":
        k_tr = cur["s"].values
    elif ctx_key == "s_c2":
        k_tr = cur["s"].values * 2 + (cur["c2"].values % 2)
    else:
        raise ValueError(ctx_key)
    best = None
    for k in np.unique(k_tr[tr]):
        m = tr & (k_tr == k)
        nb = m.sum()
        if nb < 100:
            continue
        pb = nxt["big"].values[m].mean()
        pe = nxt["even"].values[m].mean()
        z = max(abs(pb - 0.5), abs(pe - 0.5)) / np.sqrt(0.25 / nb)
        if best is None or z > best[0]:
            best = (z, k, pb, pe)
    _, k, pb, pe = best
    k_te = k_tr[te]
    cur_te = cur.iloc[te]
    pred_b = np.where(cur_te["s"].values <= 9, 1,
                      np.where(cur_te["s"].values >= 18, 0,
                               np.where(cur_te["c1"].values <= 4, 1, 0)))
    pred_e = (cur_te["c2"].values % 2 == 0).astype(int)
    pred_b = np.where(k_te == k, 1 if pb > 0.5 else 0, pred_b)
    pred_e = np.where(k_te == k, 1 if pe > 0.5 else 0, pred_e)
    return pred_b, pred_e


# ============================================================
# 逻辑回归 (纯 numpy, 有交互特征的公平基线)
# ============================================================

def logistic_eval(df, train_mask, feature_fn, label, iterations=200, lr=0.05):
    cur = df.iloc[:-1].reset_index(drop=True)
    nxt = df.iloc[1:].reset_index(drop=True)
    tr = train_mask[:-1]
    te = ~tr
    X = feature_fn(cur)
    valid = np.isfinite(X).all(1)
    X = X[valid]
    tr, te = tr[valid], te[valid]
    X = (X - X.mean(0)) / (X.std(0) + 1e-9)
    X = np.hstack([np.ones((len(X), 1)), X])
    y_b = nxt["big"].values[valid]
    y_e = nxt["even"].values[valid]
    out = {}
    for tgt, name in [(y_b, "大"), (y_e, "双")]:
        y = tgt.astype(float)
        w = np.zeros(X.shape[1])
        for _ in range(iterations):
            p = 1 / (1 + np.exp(-(X @ w)))
            g = X[tr].T @ (p[tr] - y[tr])
            w -= lr * g / tr.sum()
        prob_te = 1 / (1 + np.exp(-(X[te] @ w)))
        auc = roc_auc(prob_te, y[te])
        acc = ((prob_te > 0.5).astype(int) == y[te]).mean()
        out[name] = (auc, acc)
    print(f"  逻辑回归 [{label}] (n_te={int(te.sum())}): " + "  ".join(
        f"{k}: AUC={v[0]:.4f} 准确率={v[1]:.4f}" for k, v in out.items()))
    return out


def roc_auc(prob, y):
    order = np.argsort(prob)
    y = y[order]
    ranks = np.arange(1, len(y) + 1)
    n1 = y.sum()
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return 0.5
    return (ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def feat_basic(cur):
    cols = ["s", "c1", "c2", "c3"]
    X = cur[cols].values.astype(float)
    return np.hstack([X, (X % 2), (X >= 5).astype(float)])


def feat_recent(df):
    out = []
    for k in [1, 2, 3, 5, 10, 20]:
        roll_b = df["big"].values
        out.append(pd.Series(roll_b).rolling(k).mean().shift(1).values)
        roll_e = df["even"].values
        out.append(pd.Series(roll_e).rolling(k).mean().shift(1).values)
    return np.column_stack(out)


# ============================================================
# 固定预测数组的倍投回测 (复刻 backtest_e9.run_backtest, 仅替换预测来源)
# ============================================================

def run_backtest_preds(draws, pred_b, pred_e, ladder, stop_profit, stop_loss=None):
    """pred_b/pred_e[i] 为第 i 期(对第 i+1 期)的预测, 长度 len(draws)-1。"""
    from datetime import datetime
    from backtest_e9 import actual_result, get_rate, in_maintenance
    level = 0
    daily_pnl = 0
    cur_date = None
    pause_until_dt = None
    pending = None
    total_pnl = 0
    total_bets = 0
    max_pnl = 0
    max_drawdown = 0
    profit_days = 0
    loss_days = 0
    bursts = 0
    t_win = t_flat = t_lose = 0
    daily = {}
    for i in range(len(draws)):
        period, date, time_str, c1, c2, c3, total = draws[i]
        adx, ads, comm = actual_result(c1, c2, c3)
        if date != cur_date:
            cur_date = date
            if daily_pnl > 0:
                profit_days += 1
            elif daily_pnl < 0:
                loss_days += 1
            daily_pnl = 0
            level = 0
            pause_until_dt = None
            daily.setdefault(date, {"pnl": 0, "bets": 0, "win": 0, "flat": 0, "lose": 0,
                                    "max_level": 0, "bursts": 0})
        day = daily[date]
        if pending is not None:
            p_dx, p_ds, amount, p_level = pending
            rate = get_rate(amount, comm)
            dx_ok = p_dx == adx
            ds_ok = p_ds == ads
            win = amount * rate
            if dx_ok and ds_ok:
                pnl = round(win * 2)
                level = 0
                t_win += 1
                day["win"] += 1
            elif not dx_ok and not ds_ok:
                pnl = -amount * 2
                t_lose += 1
                day["lose"] += 1
                if p_level >= len(ladder) - 1:
                    level = 0
                    bursts += 1
                    day["bursts"] += 1
                else:
                    level = p_level + 1
            else:
                pnl = round((win if dx_ok else -amount) + (win if ds_ok else -amount))
                t_flat += 1
                day["flat"] += 1
            daily_pnl += pnl
            total_pnl += pnl
            day["pnl"] += pnl
            max_pnl = max(max_pnl, total_pnl)
            max_drawdown = max(max_drawdown, max_pnl - total_pnl)
            # 18:00-18:50 双中暂停
            if dx_ok and ds_ok:
                t = datetime.strptime(time_str, "%H:%M:%S")
                cm = t.hour * 60 + t.minute
                if 18 * 60 <= cm <= 18 * 60 + 50:
                    pause_until_dt = datetime.strptime(date + " 19:40:00", "%Y-%m-%d %H:%M:%S")
            pending = None
        dt = datetime.strptime(date + " " + time_str, "%Y-%m-%d %H:%M:%S")
        if in_maintenance(dt):
            pass
        elif pause_until_dt and dt < pause_until_dt:
            pass
        elif stop_loss is not None and daily_pnl <= stop_loss:
            pass
        elif stop_profit is not None and daily_pnl >= stop_profit:
            pass
        else:
            if i >= len(pred_b):
                continue
            amount = ladder[level]
            pdx = "大" if pred_b[i] == 1 else "小"
            pds = "双" if pred_e[i] == 1 else "单"
            total_bets += 1
            day["bets"] += 1
            day["max_level"] = max(day["max_level"], level)
            pending = (pdx, pds, amount, level)
    if daily_pnl > 0:
        profit_days += 1
    elif daily_pnl < 0:
        loss_days += 1
    return {
        "total_pnl": total_pnl, "max_drawdown": max_drawdown,
        "ratio": total_pnl / max_drawdown if max_drawdown > 0 else 0,
        "total_bets": total_bets, "profit_days": profit_days, "loss_days": loss_days,
        "bursts": bursts, "win": t_win, "flat": t_flat, "lose": t_lose,
    }


# ============================================================
# 主流程
# ============================================================

def main():
    df = load()
    n = len(df)
    print(f"已加载 {n} 期数据")

    cur = df.iloc[:-1].reset_index(drop=True)
    nxt = df.iloc[1:].reset_index(drop=True)
    act_b = nxt["big"].values
    act_e = nxt["even"].values
    act_s = nxt["s"].values

    # ---------- 1. 基准 ----------
    print("\n" + "=" * 100)
    print("一、基准对比 (全样本, 每期都下注)")
    print("=" * 100)
    strategies = {
        "E9": rule_e9(df),
        "跟组合(上期)": rule_follow(df),
        "反组合(上期)": rule_reverse(df),
        "固定大双": rule_fixed(1, 1)(df),
        "固定小单": rule_fixed(0, 0)(df),
        "固定大单": rule_fixed(1, 0)(df),
        "固定小双": rule_fixed(0, 1)(df),
    }
    for name, (pb, pe) in strategies.items():
        r = evaluate(pb, pe, act_b, act_e, act_s)
        print(fmt_res(name, r))

    # ---------- 2. 上下文搜索 ----------
    print("\n" + "=" * 100)
    print("二、上下文搜索: 历史状态 -> 下期概率 (Bonferroni 校正)")
    print("=" * 100)
    cur = df.iloc[:-1].reset_index(drop=True)
    ctx_s = context_stats(cur["s"].values, df, "s")
    print_ctx_table(ctx_s, "上下文=上期和值 (28组)", 28)
    ctx_sc2 = context_stats(cur["s"].values * 2 + (cur["c2"].values % 2), df, "s_c2")
    print_ctx_table(ctx_sc2, "上下文=上期和值×c2奇偶 (56组)", 56)
    ctx_strb = context_stats(cur["big"].values * 10 + np.minimum(cur["streak_b"].values, 5), df, "streak_b")
    print_ctx_table(ctx_strb, "上下文=大小连号长度 (12组)", 12)
    ctx_stre = context_stats(cur["even"].values * 10 + np.minimum(cur["streak_e"].values, 5), df, "streak_e")
    print_ctx_table(ctx_stre, "上下文=单双连号长度 (12组)", 12)

    # ---------- 3. 训练/测试切分 ----------
    split = int(n * 0.7)
    tr_mask = np.zeros(n, bool)
    tr_mask[:split] = True
    te_mask = ~tr_mask
    te1 = te_mask[:-1]  # 预测期对应 mask (长度 n-1)
    n_tr, n_te = split, n - split
    print("\n" + "=" * 100)
    print(f"三、训练/测试切分验证 (训练前{n_tr}期, 测试后{n_te}期)")
    print("=" * 100)

    # 样本内选规则的过拟合演示
    pb_bt, pe_bt = rule_best_ctx_train_only(df, tr_mask, "s_exact")
    r = evaluate(pb_bt, pe_bt, act_b[te1], act_e[te1], act_s[te1])
    print("  样本内选最优和值上下文 -> 测试集: " + fmt_res("最强上下文(过拟合演示)", r))

    # 查表法 (平滑)
    for key, nb in [("s_bucket", 7), ("s_exact", 28), ("c1_c2", 10)]:
        pb, pe = rule_lookup(df, tr_mask, key, nb)
        r = evaluate(pb, pe, act_b[te1], act_e[te1], act_s[te1])
        print("  " + fmt_res(f"查表[{key}]", r))

    # 逻辑回归
    logistic_eval(df, tr_mask, feat_basic, "基础特征(s,c1,c2,c3+奇偶+上下半区)")
    logistic_eval(df, tr_mask, feat_recent, "滚动窗口特征(1~20期大/双占比)")

    # ---------- 4. 洗牌对照 ----------
    print("\n" + "=" * 100)
    print("四、洗牌对照: 每天内部打乱顺序后 E9 倍投 (正确零假设)")
    print("=" * 100)
    from backtest_e9 import load_draws, run_backtest, LADDER, STOP_PROFIT
    draws = load_draws()
    r0 = run_backtest(draws, ladder=LADDER, stop_profit=STOP_PROFIT, stop_loss=None)
    print(f"  原始顺序: 盈亏 {r0['total_pnl']:+,}  回撤 {r0['max_drawdown']:,}  炸 {r0['bursts']}")
    rng = np.random.default_rng(42)
    pnls = []
    by_date = {}
    for d in draws:
        by_date.setdefault(d[1], []).append(d)
    for i in range(80):
        shuffled = []
        for date, rows in by_date.items():
            perm = rng.permutation(len(rows))
            shuffled.extend(rows[j] for j in perm)
        rs = run_backtest(shuffled, ladder=LADDER, stop_profit=STOP_PROFIT, stop_loss=None)
        pnls.append(rs["total_pnl"])
    pnls = np.array(pnls)
    print(f"  80次天内洗牌: 盈亏 mean={pnls.mean():+,.0f}  std={pnls.std():+,.0f}  "
          f"min={pnls.min():+,}  max={pnls.max():+,}")
    print(f"  原始{'+' if r0['total_pnl']>0 else ''}{r0['total_pnl']:,} 位于洗牌分布的 "
          f"{(pnls < r0['total_pnl']).mean()*100:.1f}% 分位")

    # ---------- 5. 候选策略汇总 ----------
    print("\n" + "=" * 100)
    print("五、候选策略总结")
    print("=" * 100)
    summary = []
    for name, (pb, pe) in {
        "E9": rule_e9(df),
        "固定大双": rule_fixed(1, 1)(df),
        "固定小单": rule_fixed(0, 0)(df),
        "固定大单": rule_fixed(1, 0)(df),
        "固定小双": rule_fixed(0, 1)(df),
        "跟组合": rule_follow(df),
    }.items():
        r = evaluate(pb, pe, act_b, act_e, act_s)
        summary.append((name, r[4], r[3], r[5]))
    summary.sort(key=lambda x: -x[1])
    print(f"{'策略':<14s} {'EV/腿':>8s} {'双中-双错':>10s} {'z':>6s}")
    for name, ev, gap, z in summary:
        print(f"{name:<14s} {ev:+8.4f} {gap:+10.4f} {z:+6.2f}")

    # ---------- 6. 倍投回测对比 + 炸率洗牌检验 ----------
    print("\n" + "=" * 100)
    print("六、7级倍投回测对比 + 炸率洗牌检验 (验证 E9 的优势是否规则特有)")
    print("=" * 100)
    from backtest_e9 import load_draws, LADDER, STOP_PROFIT
    draws = load_draws()
    rules = {
        "E9(原版)": rule_e9(df),
        "跟组合": rule_follow(df),
        "固定大单(错位)": rule_fixed(1, 0)(df),
        "固定小双(错位)": rule_fixed(0, 1)(df),
        "固定大双(对齐)": rule_fixed(1, 1)(df),
        "固定小单(对齐)": rule_fixed(0, 0)(df),
    }
    print(f"{'规则':<18s} {'盈亏':>9s} {'回撤':>8s} {'比值':>6s} {'炸':>4s} {'下注':>7s} {'炸率/千注':>9s}")
    for name, (pb, pe) in rules.items():
        r = run_backtest_preds(draws, np.asarray(pb), np.asarray(pe),
                               ladder=LADDER, stop_profit=STOP_PROFIT)
        print(f"{name:<18s} {r['total_pnl']:+9,d} {r['max_drawdown']:8,d} "
              f"{r['ratio']:6.2f} {r['bursts']:4d} {r['total_bets']:7,d} "
              f"{r['bursts']/r['total_bets']*1000:9.3f}")

    by_date = {}
    for d in draws:
        by_date.setdefault(d[1], []).append(d)
    rng = np.random.default_rng(31)
    print("\n  天内洗牌100次炸率检验 (每千注炸率):")
    for name, (pb, pe) in [("E9", rule_e9(df)), ("跟组合", rule_follow(df)),
                           ("固定大双", rule_fixed(1, 1)(df))]:
        pb = np.asarray(pb)
        pe = np.asarray(pe)
        r0 = run_backtest_preds(draws, pb, pe, ladder=LADDER, stop_profit=STOP_PROFIT)
        orig_rate = r0["bursts"] / r0["total_bets"] * 1000
        rates = []
        for _ in range(100):
            sh = []
            for date, rows in by_date.items():
                perm = rng.permutation(len(rows))
                sh.extend(rows[j] for j in perm)
            rs = run_backtest_preds(sh, pb, pe, ladder=LADDER, stop_profit=STOP_PROFIT)
            rates.append(rs["bursts"] / rs["total_bets"] * 1000)
        rates = np.array(rates)
        z = (orig_rate - rates.mean()) / rates.std()
        print(f"    {name:<8s} 原始={orig_rate:.3f}  洗牌mean={rates.mean():.3f} "
              f"std={rates.std():.3f}  分位={(rates < orig_rate).mean()*100:.1f}%  z={z:+.2f}")


if __name__ == "__main__":
    main()
