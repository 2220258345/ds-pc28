# -*- coding: utf-8 -*-
"""
生成 E9 回测交互式图表仪表盘 backtest_chart.html
============================================================
功能:
  - 累计盈亏曲线 / 每日盈亏 / 回撤 / 每日双中平局双错 / 炸次标记
  - 时间范围选择: 预设 (全部/近3月/近30天/近7天) + 自定义起止日期
  - 区间汇总统计 (盈亏/盈利天/炸次/最大回撤/收益回撤比)
  - 选择日期下钻: 当日逐期累计曲线 + 档位变化

用法:
  python generate_chart.py
"""
import json
import os

from backtest_e9 import LADDER, STOP_PROFIT, load_draws, run_backtest

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "backtest_chart.html")

RESULT_NAMES = {0: "双中", 1: "平局", 2: "双错", 3: "炸", 4: "未下注"}


def build():
    draws = load_draws()
    print(f"已加载 {len(draws)} 期数据")
    r = run_backtest(draws, ladder=LADDER, stop_profit=STOP_PROFIT, detail=True)
    events = r["events"]
    daily = r["daily"]

    # 每日聚合 (按日期排序)
    days = []
    cum = 0
    peak = 0
    for d in sorted(daily):
        info = daily[d]
        cum += info["pnl"]
        peak = max(peak, cum)
        days.append({
            "d": d,
            "p": info["pnl"],
            "b": info["bets"],
            "w": info["win"],
            "f": info["flat"],
            "l": info["lose"],
            "br": info["bursts"],
            "ml": info["max_level"],
            "c": cum,
        })

    # 逐期明细 (所有事件): 日期时间 / 总累计 / 当日累计 / 档位 / 结果
    periods = []
    total_cum = 0
    for ev in events:
        pnl = 0
        if ev["s_pnl"]:
            try:
                pnl = int(ev["s_pnl"])
            except ValueError:
                pnl = 0
        total_cum += pnl
        lvl = ev["b_level"].lstrip("L") if ev["b_level"] else ""
        lvl = int(lvl) if lvl.isdigit() else -1
        res = 4
        if ev["s_result"]:
            if "炸" in ev["s_result"]:
                res = 3
            elif ev["s_result"] == "双中":
                res = 0
            elif ev["s_result"] == "平局":
                res = 1
            elif "双错" in ev["s_result"]:
                res = 2
        periods.append([ev["date"] + " " + ev["time"][:5], total_cum, ev["daily_pnl"], lvl, res])

    payload = {
        "days": days,
        "periods": periods,
        "config": {
            "ladder": LADDER,
            "stop_profit": STOP_PROFIT,
            "total_pnl": r["total_pnl"],
            "max_drawdown": r["max_drawdown"],
            "ratio": r["ratio"],
            "bursts": r["bursts"],
            "bets": r["total_bets"],
            "win": r["win"],
            "flat": r["flat"],
            "lose": r["lose"],
            "profit_days": r["profit_days"],
            "loss_days": r["loss_days"],
            "first_date": days[0]["d"],
            "last_date": days[-1]["d"],
        },
    }
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    html = TEMPLATE.replace("/*__DATA__*/", data_json)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"图表已生成: {OUT} ({len(html)/1024:.0f} KB)")


TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>E9 回测图表仪表盘</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:-apple-system,"Microsoft YaHei",sans-serif; background:#0f1117; color:#e0e0e0; padding:16px; }
  h1 { font-size:20px; color:#fff; margin-bottom:4px; }
  .sub { font-size:12px; color:#888; margin-bottom:14px; }
  .controls { display:flex; flex-wrap:wrap; gap:8px; align-items:flex-end; margin-bottom:12px; }
  .controls label { font-size:12px; color:#aaa; margin-right:4px; }
  .controls input[type=date] { background:#1a1d29; color:#e0e0e0; border:1px solid #333; border-radius:6px; padding:5px 8px; font-size:12px; }
  .preset { background:#1a1d29; color:#aaa; border:1px solid #333; border-radius:6px; padding:6px 12px; font-size:12px; cursor:pointer; }
  .preset:hover { border-color:#5470c6; color:#fff; }
  .preset.active { background:#5470c6; color:#fff; border-color:#5470c6; }
  .summary { display:flex; flex-wrap:wrap; gap:10px; margin-bottom:12px; }
  .s-card { background:#1a1d29; border-radius:8px; padding:8px 14px; min-width:88px; }
  .s-card .label { font-size:11px; color:#888; margin-bottom:2px; }
  .s-card .val { font-size:17px; font-weight:600; }
  .pos { color:#91cc75; } .neg { color:#ee6666; } .flat { color:#fac858; }
  .chart { background:#1a1d29; border-radius:10px; padding:10px; margin-bottom:12px; }
  .chart h3 { font-size:13px; color:#ccc; font-weight:500; margin:2px 0 6px 4px; }
  .chart div { width:100%; height:280px; }
  .chart.tall div { height:320px; }
  @media (max-width:640px) { .chart div { height:220px; } .chart.tall div { height:260px; } }
</style>
</head>
<body>
<h1>E9 回测图表仪表盘</h1>
<div class="sub" id="subtitle"></div>

<div class="controls">
  <button class="preset" data-days="all">全部</button>
  <button class="preset" data-days="90">近3月</button>
  <button class="preset" data-days="30">近30天</button>
  <button class="preset" data-days="7">近7天</button>
  <label>从</label><input type="date" id="from">
  <label>至</label><input type="date" id="to">
  <button class="preset" id="apply">应用区间</button>
</div>

<div class="summary" id="summary"></div>

<div class="chart"><h3>累计盈亏曲线（炸次红色标记）</h3><div id="equity"></div></div>
<div class="chart"><h3>每日盈亏</h3><div id="daily"></div></div>
<div class="chart"><h3>回撤曲线（相对区间峰值）</h3><div id="drawdown"></div></div>
<div class="chart"><h3>每日 双中 / 平局 / 双错</h3><div id="outcomes"></div></div>
<div class="chart tall">
  <h3>当日逐期明细（选择日期下钻：累计盈亏 + 档位）</h3>
  <div style="margin:0 0 6px 4px;"><label>选择日期</label>
  <select id="daySel" style="background:#1a1d29;color:#e0e0e0;border:1px solid #333;border-radius:6px;padding:4px 8px;font-size:12px;"></select></div>
  <div id="drilldown"></div>
</div>

<script>
const DATA = /*__DATA__*/;
const days = DATA.days;
const periods = DATA.periods;
const cfg = DATA.config;

const $ = id => document.getElementById(id);
const FMT = new Intl.NumberFormat('zh-CN');
const DAYS = days.map(d => d.d);
$('from').value = DAYS[0];
$('to').value = DAYS[DAYS.length-1];
$('subtitle').textContent = `数据 ${cfg.first_date} ~ ${cfg.last_date} | ${days.length}天 | ${cfg.bets.toLocaleString()}注 | 梯度[${cfg.ladder.join(',')}] 止盈+${cfg.stop_profit}`;

const charts = {};
['equity','daily','drawdown','outcomes','drilldown'].forEach(id => {
  charts[id] = echarts.init($(id), null, {renderer:'canvas'});
});
window.addEventListener('resize', () => Object.values(charts).forEach(c => c.resize()));

// 从 tooltip 参数里取安全的数据索引 (markPoint 参数没有 dataIndex)
function safeIdx(p) {
  for (const q of p) {
    if (Number.isInteger(q.dataIndex) && q.dataIndex >= 0) return q.dataIndex;
  }
  return -1;
}
const TT = { trigger:'axis', confine:true, backgroundColor:'#252836', borderColor:'#333',
  textStyle:{color:'#e0e0e0', fontSize:12}, padding:[4,8],
  extraCssText:'max-width:420px;white-space:nowrap;' };
const sgn = v => v>=0 ? '+' : '';

function dateRange(daysArr) {
  const from = $('from').value, to = $('to').value;
  return daysArr.filter(d => d >= from && d <= to);
}

function summarize(sel) {
  const pnl = sel.reduce((a,d)=>a+d.p,0);
  const winD = sel.filter(d=>d.p>0).length;
  const lossD = sel.filter(d=>d.p<0).length;
  const bursts = sel.reduce((a,d)=>a+d.br,0);
  let cum = 0, peak = -Infinity, maxdd = 0;
  sel.forEach(d => { cum += d.p; peak = Math.max(peak, cum); maxdd = Math.max(maxdd, peak-cum); });
  const ratio = maxdd > 0 ? pnl/maxdd : 0;
  $('summary').innerHTML = `
    <div class="s-card"><div class="label">区间盈亏</div><div class="val ${pnl>=0?'pos':'neg'}">${pnl>=0?'+':''}${FMT.format(pnl)}</div></div>
    <div class="s-card"><div class="label">天数</div><div class="val">${sel.length}</div></div>
    <div class="s-card"><div class="label">盈/亏天</div><div class="val"><span class="pos">${winD}</span>/${lossD}</div></div>
    <div class="s-card"><div class="label">炸次</div><div class="val neg">${bursts}</div></div>
    <div class="s-card"><div class="label">最大回撤</div><div class="val neg">${FMT.format(maxdd)}</div></div>
    <div class="s-card"><div class="label">收益/回撤</div><div class="val pos">${ratio.toFixed(2)}</div></div>`;
}

function drawAll(sel) {
  const dates = sel.map(d=>d.d);
  const cum = []; let c = 0;
  sel.forEach(d => { c += d.p; cum.push(c); });
  const burstIdx = [];
  sel.forEach((d,i) => { if (d.br>0) burstIdx.push({value:[dates[i], cum[i]], times:d.br}); });

  charts.equity.setOption({
    tooltip: Object.assign({}, TT, { formatter: p => {
      const i = safeIdx(p);
      if (i < 0 || i >= sel.length) return '';
      const d = sel[i];
      let s = `${dates[i]} · 累计 ${sgn(cum[i])}${FMT.format(cum[i])} · 当日 ${sgn(d.p)}${FMT.format(d.p)}`;
      if (d.br > 0) s += ` · <b style="color:#ee6666">炸${d.br}次</b>`;
      return s;
    } }),
    grid:{left:70,right:20,top:25,bottom:55},
    xAxis:{type:'category',data:dates,axisLabel:{color:'#888',fontSize:11},axisLine:{lineStyle:{color:'#333'}}},
    yAxis:{type:'value',axisLabel:{color:'#888',fontSize:11,formatter:v=>FMT.format(v)},splitLine:{lineStyle:{color:'#222'}}},
    dataZoom:[{type:'inside'},{type:'slider',height:18,bottom:8,borderColor:'#333',backgroundColor:'#1a1d29',dataBackground:{lineStyle:{color:'#444'},areaStyle:{color:'#2a2e3d'}},selectedDataBackground:{lineStyle:{color:'#5470c6'},areaStyle:{color:'#5470c6'}}}],
    series:[{type:'line',data:cum,smooth:true,symbol:'none',lineStyle:{color:'#91cc75',width:2},areaStyle:{color:'rgba(145,204,117,0.12)'},
      markPoint:{symbol:'pin',symbolSize:22,data:burstIdx.map(b=>({coord:b.value,value:b.times,itemStyle:{color:'#ee6666'},
        label:{show:true,formatter:'{c}',color:'#fff',fontSize:9,position:'inside'}}))}}]
  }, {notMerge:true});

  charts.daily.setOption({
    tooltip:Object.assign({}, TT, { formatter:p=>{
      const i=safeIdx(p); if (i<0 || i>=sel.length) return '';
      const d=sel[i];
      return `${dates[i]} · ${sgn(d.p)}${FMT.format(d.p)} · 注${d.b} 中${d.w}/平${d.f}/错${d.l} 炸${d.br}`; }}),
    grid:{left:70,right:20,top:25,bottom:50},
    xAxis:{type:'category',data:dates,axisLabel:{color:'#888',fontSize:11},axisLine:{lineStyle:{color:'#333'}}},
    yAxis:{type:'value',axisLabel:{color:'#888',fontSize:11,formatter:v=>FMT.format(v)},splitLine:{lineStyle:{color:'#222'}}},
    series:[{type:'bar',data:sel.map(d=>({value:d.p,itemStyle:{color:d.p>=0?'rgba(145,204,117,0.75)':'rgba(238,102,102,0.75)'}})),barMaxWidth:18}]
  }, {notMerge:true});

  let peak=-Infinity, c2=0;
  const dd = sel.map(d => { c2+=d.p; peak=Math.max(peak,c2); return Math.max(0, peak-c2); });
  charts.drawdown.setOption({
    tooltip:Object.assign({}, TT, { formatter:p=>{
      const i=safeIdx(p); if (i<0 || i>=sel.length) return '';
      return `${dates[i]} · 回撤 ${FMT.format(dd[i])}`; }}),
    grid:{left:70,right:20,top:25,bottom:50},
    xAxis:{type:'category',data:dates,axisLabel:{color:'#888',fontSize:11},axisLine:{lineStyle:{color:'#333'}}},
    yAxis:{type:'value',axisLabel:{color:'#888',fontSize:11,formatter:v=>FMT.format(v)},splitLine:{lineStyle:{color:'#222'}}},
    series:[{type:'line',data:dd,smooth:true,symbol:'none',lineStyle:{color:'#ee6666',width:1.5},areaStyle:{color:'rgba(238,102,102,0.18)'}}]
  }, {notMerge:true});

  charts.outcomes.setOption({
    tooltip:Object.assign({}, TT, { formatter:p=>{
      const i=safeIdx(p); if (i<0 || i>=sel.length) return '';
      const d=sel[i];
      return `${dates[i]} · 双中${d.w} 平局${d.f} 双错${d.l}`; }}),
    legend:{textStyle:{color:'#aaa',fontSize:11},top:0},
    grid:{left:70,right:20,top:30,bottom:50},
    xAxis:{type:'category',data:dates,axisLabel:{color:'#888',fontSize:11},axisLine:{lineStyle:{color:'#333'}}},
    yAxis:{type:'value',axisLabel:{color:'#888',fontSize:11},splitLine:{lineStyle:{color:'#222'}}},
    series:[
      {name:'双中',type:'bar',stack:'o',data:sel.map(d=>d.w),itemStyle:{color:'rgba(145,204,117,0.7)'}},
      {name:'平局',type:'bar',stack:'o',data:sel.map(d=>d.f),itemStyle:{color:'rgba(250,200,88,0.6)'}},
      {name:'双错',type:'bar',stack:'o',data:sel.map(d=>d.l),itemStyle:{color:'rgba(238,102,102,0.7)'}}
    ]
  }, {notMerge:true});
  summarize(sel);
}

function fillDaySel() {
  const sel = $('daySel');
  sel.innerHTML = DAYS.map(d=>`<option>${d}</option>`).join('');
  sel.value = DAYS[DAYS.length-1];
  sel.onchange = () => drawDrill(sel.value);
  drawDrill(sel.value);
}

function drawDrill(date) {
  const pts = periods.filter(p => p[0].startsWith(date));
  if (!pts.length) return;
  charts.drilldown.setOption({
    tooltip:Object.assign({}, TT, { formatter:p=>{
      const i=safeIdx(p); if (i<0 || i>=pts.length) return '';
      const pt=pts[i];
      return `${pt[0]} · 累计 ${sgn(pt[1])}${FMT.format(pt[1])} · ${RES[pt[4]]}${pt[3]>=0?' · L'+pt[3]:''}`; }}),
    legend:{textStyle:{color:'#aaa',fontSize:11},top:0},
    grid:{left:70,right:20,top:30,bottom:40},
    xAxis:{type:'category',data:pts.map(p=>p[0].slice(6)),axisLabel:{color:'#888',fontSize:10,interval:Math.max(0,Math.floor(pts.length/8)-1)},axisLine:{lineStyle:{color:'#333'}}},
    yAxis:[{type:'value',axisLabel:{color:'#888',fontSize:11,formatter:v=>FMT.format(v)},splitLine:{lineStyle:{color:'#222'}}},
           {type:'value',min:0,max:6,interval:1,axisLabel:{color:'#888',fontSize:11},splitLine:{show:false}}],
    series:[
      {name:'累计盈亏',type:'line',data:pts.map(p=>p[1]),smooth:false,symbol:'none',lineStyle:{color:'#91cc75',width:1.8}},
      {name:'档位',type:'line',yAxisIndex:1,data:pts.map(p=>p[3]>=0?p[3]:null),step:'end',smooth:false,symbol:'none',lineStyle:{color:'#5470c6',width:1.5}}
    ]
  }, {notMerge:true});
}
const RES = ['双中','平局','双错','炸','未下注'];

function applyRange() {
  const from = $('from').value, to = $('to').value;
  const sel = days.filter(d => d.d >= from && d.d <= to);
  document.querySelectorAll('.preset').forEach(b=>b.classList.remove('active'));
  drawAll(sel);
}

document.querySelectorAll('.preset[data-days]').forEach(btn => {
  btn.onclick = () => {
    const n = btn.dataset.days;
    const end = DAYS[DAYS.length-1];
    $('to').value = end;
    if (n === 'all') { $('from').value = DAYS[0]; }
    else { const start = new Date(end); start.setDate(start.getDate()-(parseInt(n)-1));
      $('from').value = start.toISOString().slice(0,10);
      const d = days.find(x=>x.d>=$('from').value);
      if (d) $('from').value = d.d;
    }
    document.querySelectorAll('.preset').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active');
    applyRange();
  };
});
$('apply').onclick = applyRange;

applyRange();
fillDaySel();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    build()
