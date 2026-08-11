# 28数据分析 · API 对接文档

> 基础地址：`http://localhost:8080`
> 所有接口均为 `GET` 请求，返回 `application/json; charset=utf-8`
> SSE 端点返回 `text/event-stream; charset=utf-8`
> 所有接口支持跨域（`Access-Control-Allow-Origin: *`）

---

## 1. 服务器状态

`GET /api/status`

返回服务器基本信息，可用于健康检查。

**响应示例：**
```json
{
  "total_rows": 31365,
  "max_nbr": 3468446,
  "max_date": "2026-08-12",
  "server_time": "2026-08-12 06:49:41",
  "mode": "api-only (no collector)"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| total_rows | int | 数据库总期数 |
| max_nbr | int | 最新期号 |
| max_date | string | 最新日期 (YYYY-MM-DD) |
| server_time | string | 服务器时间 (北京时间) |
| mode | string | 服务模式标识 |

---

## 2. 服务器时间与倒计时

`GET /api/time`

返回校正后的服务器时间、当前期号、距下期更新剩余秒数。前端据此同步倒计时。

**响应示例：**
```json
{
  "server_time": 1786488594.71,
  "base_epoch": 1058114851,
  "cycle": 210,
  "current_period": 3468446,
  "countdown": 127,
  "time_offset": 13.675
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| server_time | float | 校正后的 Unix 时间戳 (秒) |
| base_epoch | int | 期号 0 对应的时间戳 |
| cycle | int | 开奖周期 (秒)，固定 210 (3.5 分钟) |
| current_period | int | 当前期号 |
| countdown | int | 距下期更新剩余秒数 (0-210) |
| time_offset | float | 本地时钟与参考站偏移 (秒) |

**倒计时计算公式：**
```
elapsed = int(server_time) - base_epoch
current_period = elapsed // cycle
countdown = cycle - (elapsed % cycle)
```

---

## 3. 最新一期数据

`GET /api/latest`

返回最新一期开奖数据 + 当前期号 + 倒计时。

**响应示例：**
```json
{
  "latest": {
    "draw_nbr": 3468446,
    "draw_date": "2026-08-12",
    "draw_time": "06:48:30",
    "c1": 8,
    "c2": 7,
    "c3": 4,
    "draw_num": 19,
    "size_type": "大",
    "parity_type": "单",
    "combination_type": "大单"
  },
  "current_period": 3468446,
  "countdown": 127,
  "server_time": 1786488594.71
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| latest.draw_nbr | int | 期号 |
| latest.draw_date | string | 开奖日期 |
| latest.draw_time | string | 开奖时间 |
| latest.c1, c2, c3 | int | 三个开奖号码 (0-9) |
| latest.draw_num | int | 和值 (c1+c2+c3, 0-27) |
| latest.size_type | string | 大小：和值≥14为"大"，否则"小" |
| latest.parity_type | string | 单双：和值为偶数"双"，奇数"单" |
| latest.combination_type | string | 组合：大小+单双，如"大单"、"小双" |

> `latest` 可能为 `null`（数据库为空时）

---

## 4. 历史数据（分页）

`GET /api/history?page=1&size=30`

按期号倒序返回历史数据。

**参数：**

| 参数 | 类型 | 默认 | 范围 | 说明 |
|------|------|------|------|------|
| page | int | 1 | ≥1 | 页码 |
| size | int | 30 | 1-100 | 每页条数 |

**响应示例：**
```json
{
  "total": 31365,
  "page": 1,
  "size": 2,
  "pages": 15683,
  "list": [
    {
      "draw_nbr": 3468446,
      "draw_date": "2026-08-12",
      "draw_time": "06:48:30",
      "c1": 8, "c2": 7, "c3": 4,
      "draw_num": 19,
      "size_type": "大",
      "parity_type": "单",
      "combination_type": "大单"
    }
  ]
}
```

---

## 5. 走势数据

`GET /api/trend?limit=100`

返回最近 N 期走势，按期号**升序**排列（适合绘制走势图）。

**参数：**

| 参数 | 类型 | 默认 | 范围 | 说明 |
|------|------|------|------|------|
| limit | int | 100 | 1-500 | 返回条数 |

**响应示例：**
```json
[
  {"draw_nbr": 3468444, "draw_num": 17, "size_type": "大", "parity_type": "单"},
  {"draw_nbr": 3468445, "draw_num": 10, "size_type": "小", "parity_type": "双"},
  {"draw_nbr": 3468446, "draw_num": 19, "size_type": "大", "parity_type": "单"}
]
```

---

## 6. 大小单双未开统计

`GET /api/unopened`

返回各形态距上次开出已间隔多少期。

**响应示例：**
```json
{
  "大": 0,
  "小": 1,
  "单": 0,
  "双": 1,
  "大单": 0,
  "大双": 6,
  "小单": 5,
  "小双": 1
}
```

| 字段 | 说明 |
|------|------|
| 大 / 小 | 和值≥14 / <14 |
| 单 / 双 | 和值奇数 / 偶数 |
| 大单 / 大双 | 大+单 / 大+双 |
| 小单 / 小双 | 小+单 / 小+双 |

---

## 7. 特码（和值）未开统计

`GET /api/sum-unopened`

返回每个和值（0-27）的未开期数，按赔率配对分组（0 与 27 同赔率，1 与 26 同赔率，以此类推）。

**响应示例：**
```json
{
  "latest_nbr": 3468446,
  "groups": [
    {"sums": [0, 27], "odds": 920,   "unopened": [902, 4420]},
    {"sums": [1, 26], "odds": 300,   "unopened": [666, 300]},
    {"sums": [2, 25], "odds": 150,   "unopened": [622, 265]},
    {"sums": [3, 24], "odds": 90,    "unopened": [...]},
    {"sums": [4, 23], "odds": 60,    "unopened": [...]},
    {"sums": [5, 22], "odds": 38,    "unopened": [...]},
    {"sums": [6, 21], "odds": 30,    "unopened": [...]},
    {"sums": [7, 20], "odds": 24,    "unopened": [...]},
    {"sums": [8, 19], "odds": 19,    "unopened": [...]},
    {"sums": [9, 18], "odds": 16,    "unopened": [...]},
    {"sums": [10, 17], "odds": 15,   "unopened": [...]},
    {"sums": [11, 16], "odds": 14,   "unopened": [...]},
    {"sums": [12, 15], "odds": 13.2, "unopened": [...]},
    {"sums": [13, 14], "odds": 13.2, "unopened": [...]}
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| latest_nbr | int | 最新期号 |
| groups[].sums | int[] | 配对的两个和值 |
| groups[].odds | float | 赔率 (倍) |
| groups[].unopened | int[] | 两个和值各自的未出期数；-1 表示从未开出 |

**赔率表：**

| 和值 | 赔率 | 和值 | 赔率 |
|------|------|------|------|
| 0 / 27 | 920倍 | 7 / 20 | 24倍 |
| 1 / 26 | 300倍 | 8 / 19 | 19倍 |
| 2 / 25 | 150倍 | 9 / 18 | 16倍 |
| 3 / 24 | 90倍 | 10 / 17 | 15倍 |
| 4 / 23 | 60倍 | 11 / 16 | 14倍 |
| 5 / 22 | 38倍 | 12 / 15 | 13.2倍 |
| 6 / 21 | 30倍 | 13 / 14 | 13.2倍 |

---

## 8. 全部数据（回测用）

`GET /api/draws`

返回全部历史数据（按期号升序），附带 E9 策略参数。数据量较大（3 万+期），建议仅在回测时调用。

**响应示例（精简）：**
```json
{
  "draws": [
    [1, "2003-07-14", "00:47:31", 9, 9, 1],
    [2, "2003-07-14", "00:51:01", 3, 5, 2]
  ],
  "ladder": [20, 40, 80, 160, 320, 640, 1280],
  "C": {
    "BIG": 14,
    "COMM_SUMS": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27],
    "COMM_RATE": 0.98,
    "HIGH_BET": 100,
    "HIGH_RATE": 0.95
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| draws | array | 每行：[期号, 日期, 时间(8位), c1, c2, c3] |
| ladder | int[] | 7 级阶梯下注金额 |
| C.BIG | int | 大小阈值 (≥14 为大) |
| C.COMM_SUMS | int[] | 退水适用和值列表 |
| C.COMM_RATE | float | 退水率 |
| C.HIGH_BET | int | 高额下注阈值 |
| C.HIGH_RATE | float | 高额退水率 |

---

## 9. 实时推送（SSE）

`GET /api/events`

建立 Server-Sent Events 长连接，服务器在检测到新开奖数据入库后**立即推送**给前端。

**连接方式（JavaScript）：**
```javascript
const evtSource = new EventSource('/api/events');

// 连接成功，同步服务器时间
evtSource.addEventListener('hello', (e) => {
  const data = JSON.parse(e.data);
  console.log('服务器时间:', data.server_time);
  console.log('当前期号:', data.current_period);
  console.log('倒计时:', data.countdown);
});

// 新开奖数据推送
evtSource.addEventListener('new_draw', (e) => {
  const data = JSON.parse(e.data);
  console.log('新数据:', data.latest);
  // data.latest 字段同 /api/latest 的 latest 对象
  // data.added: 新增条数 (通常为 1)
  // data.current_period, data.countdown, data.server_time
});

// 自动重连
evtSource.addEventListener('open', () => console.log('SSE 已连接'));
evtSource.addEventListener('error', () => console.log('SSE 重连中...'));
```

**事件类型：**

| 事件 | 触发时机 | data 字段 |
|------|----------|-----------|
| `hello` | 连接建立时立即发送 | server_time, current_period, countdown |
| `new_draw` | 新开奖数据入库后 | latest, added, current_period, countdown, server_time |

**心跳：** 服务器每 15 秒发送 `: heartbeat\n\n` 注释行保活，前端可忽略。

**推送时机：**
- 服务器后台每 0.5 秒轮询数据库
- 检测到 `draw_nbr` 大于上次推送的期号时立即广播 `new_draw`
- 延迟通常 ≤ 1 秒

---

## 数据字段速查

### 开奖数据对象（latest / history.list 元素）

| 字段 | 类型 | 说明 | 取值范围 |
|------|------|------|----------|
| draw_nbr | int | 期号 | 递增整数 |
| draw_date | string | 日期 | YYYY-MM-DD |
| draw_time | string | 时间 | HH:MM:SS |
| c1 | int | 号码1 | 0-9 |
| c2 | int | 号码2 | 0-9 |
| c3 | int | 号码3 | 0-9 |
| draw_num | int | 和值 | 0-27 |
| size_type | string | 大小 | "大" / "小" |
| parity_type | string | 单双 | "单" / "双" |
| combination_type | string | 组合 | "大单"/"大双"/"小单"/"小双" |

### 计算规则
- 和值 = c1 + c2 + c3
- 大小：和值 ≥ 14 为"大"，否则"小"
- 单双：和值为奇数"单"，偶数"双"
- 组合：大小 + 单双（如和值 19 → 大单）

---

## 开奖周期

- 周期：210 秒（3.5 分钟）一期
- 期号计算：`(server_time - base_epoch) // 210`
- 倒计时：`210 - ((server_time - base_epoch) % 210)`
- base_epoch = 1058114851（北京时间 2003-07-14 00:47:31）

---

## 错误处理

所有接口在出错时返回 HTTP 200 + JSON 错误体（不使用 HTTP 错误码），前端应检查响应内容是否合法。

数据库为空时，`/api/latest` 返回 `latest: null`。

---

## 部署说明

| 服务 | 端口 | 启动命令 | 职责 |
|------|------|----------|------|
| 采集服务器 | 9000 | `python server.py --port 9000` | 后台采集 + 写库 + API |
| 轻量 API | 9001 | `python api_server.py --port 9001` | 仅读库 + API + SSE |

两个服务共享同一数据库（`pc28_history.db`），可独立部署。轻量 API 不启动采集线程，数据由采集服务器写入。
