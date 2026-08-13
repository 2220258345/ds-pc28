# PC28 E9 策略文档

## 一、游戏规则

- 每 210 秒一期，每期开奖三个数字 c1/c2/c3（各 0-9），和值 = c1+c2+c3（0-27）
- 投注两腿（同金额）：**大小注** + **单双注**（如 `大20 双20`）
- **大小**：和值 ≥ 14 为大，< 14 为小
- **单双**：和值偶数为双，奇数为单
- **抽水**：和值 13/14 时赢方只得 98%（rate=0.98）；其余和值 100%
- **高注抽水**：单腿金额 > 5000 时，所有和值赢方只得 90%（rate=0.9）
- **结算**：双中（两腿都对）赢 2×金额×rate；双错（两腿都错）亏 2×金额；平局（一对一错）约 0

## 二、E9 预测规则

用**当期开奖数据**预测**下期**：

| 维度 | 规则 |
|------|------|
| **大小** | 和值 ≤ 9 -> 预测大；和值 ≥ 18 -> 预测小；和值 10-17 -> 看首位 c1（0-4 预测大，5-9 预测小）|
| **单双** | 看第二位 c2 奇偶（偶数预测双，奇数预测单）|

E9 的统计优势：双中率 25.3% > 双错率 24.7%（每注期望 +0.89，约 0.44% 边际优势）。

## 三、梯度倍投系统

### 3.1 原版 6 级梯度

```
39 -> 78 -> 156 -> 312 -> 624 -> 1248
```

- 2 倍递增，**全额回收**：任意级双中 = 回收前面所有亏损 + 净赚 2×39=78
- 数学性质：`LADDER[k] = sum(LADDER[0..k-1]) + 起始注`
- 双中 -> 回底 Level 0；双错 -> 升级 Level+1；平局 -> 保持
- 最高级双错 -> **炸**（回底，亏损 -2496）
- 炸需连续 5 次双错（概率 0.247⁵ ≈ 0.09%）

### 3.2 回测最优 7 级梯度（推荐）

```
20 -> 40 -> 80 -> 160 -> 320 -> 640 -> 1280
```

- 同样 2x 递增 + 全额回收，起始注降为 20
- 最大注 1280（资金需求 ~2560，与 6 级的 2496 相当）
- 炸需连续 6 次双错（概率 0.247⁶ ≈ 0.02%），炸次从 93 降至 38
- 配合止盈 +2500，炸次进一步降至 20

### 3.3 风控参数

| 参数 | 原版（6级） | 回测最优（7级） | 说明 |
|------|------------|---------------|------|
| 止盈 | +4000 | **+2500** | 当天累计达此值停投至次日 |
| 止损 | -6000 | **不设** | 7级下止损有害（截断恢复机会）|
| 维护时段 | 19:00-19:33(夏)/20:00-20:33(冬) | 同 | 不开奖，跳过 |
| 双中暂停 | 18:00-18:50 双中 -> 暂停至 19:40 | 同 | 防止连续双中后立即亏回 |
| 每日重置 | 每天 Level 回 0，日盈亏归零 | 同 | |

## 四、回测结论（31244期 / 79天）

| 配置 | 总盈亏 | 最大回撤 | 收益/回撤 | 炸次 | 盈天:亏天 |
|------|--------|----------|-----------|------|-----------|
| 6级起39 无风控 | +93,466 | 23,781 | 3.93 | 93 | 47:29 |
| 6级起39 止盈+5000 | +109,870 | 26,098 | 4.21 | 71 | 55:21 |
| 6级起39 止损-8K/止盈+5K | +112,379 | 20,509 | 5.48 | - | 55:21 |
| **7级起20 止盈+2500（推荐）** | **+108,035** | **11,144** | **9.69** | **21** | **62:17** |
| 7级起20 无风控 | +88,837 | 14,918 | 5.96 | 38 | 45:31 |

**推荐配置**：7级起20（20/40/80/160/320/640/1280）+ 止盈+2500，不设止损。

### 关键发现

1. **2x 倍率是数学必然**：只有 2x 递增才能保证全额回收，其他倍率打破回收性质
2. **7 级优于 6 级**：多一级缓冲使炸次暴降（93->38），同资金下收益/回撤比翻倍
3. **止盈有效，止损有害**：止盈锁定日内利润；止损截断恢复机会
4. **止盈需按比例缩放**：最优止盈 ≈ 起始注 × 128（起20->止2500，起39->止5000）
5. **单腿 > 5000 抽水 10%**：高注抽水使 3x+ 倍率和 8 级+ 梯度失效

## 五、文件说明

| 路径 | 说明 |
|------|------|
| `app/config.py` | 统一配置：数据库后端与连接参数 |
| `app/storage/` | 存储抽象层（SQLite / PostgreSQL / MySQL） |
| `app/migrate_db.py` | SQLite -> 目标后端迁移脚本 |
| `app/core/db.py` | 数据库门面（委托 storage） |
| `app/server.py` | 采集服务器 + HTTP/SSE（容器内默认 8000） |
| `app/api_server.py` | 轻量只读 API 服务器 |
| `app/collector.py` | 多源采集器（pc89/jndpc/wh28/pc28.help 等） |
| `app/backtest_e9.py` | E9 回测引擎 |
| `scripts/db_setup.py` | 数据维护：CSV 导入/校验/导出 |
| `scripts/fetch_update.py` | 数据更新脚本（复用 collector） |
| `scripts/strategy_search.py` | 统计搜索：公平性/方法/EV/洗牌检验 |
| `scripts/generate_chart.py` | 旧版独立图表模板生成器 |
| `static/index.html` | 前端看板（数据/走势/未开/特码/回测） |
| `pc28_history.db` | SQLite 源库（当前 32189 期，期号 3436946~3469134） |
| `requirements.txt` | Python 依赖 |
| `tests/test_storage.py` | 存储层回归测试 |
| `Dockerfile` / `docker-compose.yml` / `.dockerignore` | Docker 部署配置 |

## 六、回测脚本使用

```bash
# 更新开奖数据 (从 pc28.help 拉取最近2000期, 自动入库并校验)
python scripts/fetch_update.py

# 其他更新选项
python scripts/fetch_update.py --nbr 5000          # 指定拉取期数 (最多30000)
python scripts/fetch_update.py --source wh28 --days 1  # 备用源 wh28.com (每天仅最新100期)
python scripts/fetch_update.py --verify            # 仅校验数据库完整性

# 首次使用: 初始化数据库 (从 CSV 导入, 现已迁移完成)
python scripts/db_setup.py --verify

# 全量回测 + 每日统计 HTML
python app/backtest_e9.py

# 今日逐期明细 HTML
python app/backtest_e9.py --today
python app/backtest_e9.py --today --date 2026-08-07

# 数据库导出为 CSV (备份/交换用)
python scripts/db_setup.py --export

# 止盈止损网格扫描（8×8=64种组合）
python app/backtest_e9.py --stops

# 交互式图表仪表盘 (曲线 + 时间范围选择 + 按日下钻)
python scripts/generate_chart.py
```

### 修改配置

脚本顶部配置区：
```python
LADDER = [20, 40, 80, 160, 320, 640, 1280]  # 梯度
STOP_PROFIT = 2500   # 止盈（None=不设）
STOP_LOSS = None     # 止损（None=不设）
```

### 数据存储

数据统一存放在 `draws` 表（SQLite / PostgreSQL / MySQL 同构）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `draw_nbr` | INTEGER 主键 | 期号 |
| `draw_date` | TEXT | 日期 YYYY-MM-DD |
| `draw_time` | TEXT | 时间 HH:MM:SS |
| `c1/c2/c3` | INTEGER | 三个开奖数字 |
| `draw_num` | INTEGER | 和值 |
| `size_type` | TEXT | 大小（大/小） |
| `parity_type` | TEXT | 单双（单/双） |
| `combination_type` | TEXT | 组合（大双/小单/...） |

更新数据后执行 `python scripts/db_setup.py --verify` 可校验完整性（数量、主键唯一、和值、期号连续）。

## 七、Docker 部署（PostgreSQL，推荐）

当前部署采用 **PostgreSQL 16 + 应用容器**，数据持久化在 `pg_data` 卷；应用容器内监听 8000，映射到宿主机 8001（因 8000 常被占用）。

### 7.1 构建并启动

```bash
docker compose up -d --build
```

启动后访问 http://localhost:8001/ 。

### 7.2 服务与容器

| 容器 | 镜像 | 说明 |
|------|------|------|
| `pc28-postgres` | postgres:16-alpine | PostgreSQL，内部 5432 |
| `pc28-app` | pc28-app:local | server.py + 后台采集，8001->8000 |

数据库连接由 `docker-compose.yml` 注入：`DB_BACKEND=postgres`、`DB_HOST=postgres`、`DB_PORT=5432`、`DB_NAME=pc28`、`DB_USER=pc28`、`DB_PASSWORD=pc28pass`。

### 7.3 常用命令

```bash
docker compose up -d --build   # 构建并后台启动
docker compose ps              # 查看运行状态
docker compose logs -f app     # 查看应用日志
docker compose restart app     # 重启应用
docker compose down            # 停止（保留 pg_data 卷）
docker compose down -v         # 停止并清空 PostgreSQL 数据
```

### 7.4 首次初始化 / 迁移数据

从本地 SQLite 源库迁移到 PostgreSQL：

```bash
docker compose run --rm \
  -v "C:/path/to/pc28_history.db:/migration/pc28_history.db" \
  app python app/migrate_db.py --source-path /migration/pc28_history.db
```

迁移脚本以只读方式读取源 SQLite，不会改动源库；目标后端由 `DB_BACKEND` 决定。

### 7.5 补采（回填缺口）

由于容器内 `pc28.help` 域名会被解析成 `0.0.0.0` 而无法直连，补采在宿主机执行（宿主机需有代理 `127.0.0.1:10808`），再同步回 PostgreSQL：

```bash
# 1. 宿主机补采到本地 SQLite
python scripts/fetch_update.py --nbr 2000

# 2. 同步回 PostgreSQL
docker compose run --rm \
  -v "C:/path/to/pc28_history.db:/migration/pc28_history.db" \
  app python app/migrate_db.py --source-path /migration/pc28_history.db
```

`--nbr` 建议 2000（约最近 4.9 天，可回填近期缺口）；如需更长历史可调大，最大 30000。

### 7.6 调整端口

修改 `docker-compose.yml` 的 `ports`：

```yaml
ports:
  - "9000:8000"   # 宿主机 9000 -> 容器 8000
```

### 7.7 本地直接运行（无 Docker）

```bash
python app/server.py          # SQLite，默认 8000
python app/server.py --port 9000
DB_BACKEND=postgres DB_HOST=127.0.0.1 DB_PORT=5432 DB_NAME=pc28 DB_USER=pc28 DB_PASSWORD=pc28pass python app/server.py
```

## 数据库后端切换（SQLite / PostgreSQL / MySQL）

存储层已统一到 `app/storage`，通过环境变量选择后端，上层代码与脚本无需改动。
当前部署使用 PostgreSQL（见第七节），本地无 Docker 时默认 SQLite。

| 变量 | 说明 | 默认 |
|------|------|------|
| `DB_BACKEND` | `sqlite` / `postgres` / `mysql` | `sqlite` |
| `DB_URI` | 完整连接串（优先级高于 `DB_BACKEND`） | - |
| `DB_DIR` | SQLite 数据库目录 | 项目根目录 |
| `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` | PostgreSQL/MySQL 连接参数 | - |
| `DB_DRIVER` | PostgreSQL: `psycopg2`；MySQL: `pymysql` | `psycopg2` / `pymysql` |

示例：

```bash
# SQLite（默认）
python app/server.py

# PostgreSQL
DB_BACKEND=postgres DB_HOST=127.0.0.1 DB_PORT=5432 DB_NAME=pc28 \
  DB_USER=postgres DB_PASSWORD=secret python app/server.py

# 或使用完整 URI
DB_URI="postgresql+psycopg2://postgres:secret@127.0.0.1:5432/pc28" python app/server.py

# MySQL
DB_URI="mysql+pymysql://root:secret@127.0.0.1:3306/pc28?charset=utf8mb4" python app/server.py
```

`scripts/db_setup.py`、`scripts/fetch_update.py`、`scripts/strategy_search.py` 已统一走同一存储层，
切换后端后这些脚本同样生效。

## 八、采集器说明

`collector.py` 支持 4 个 API 数据源，自动故障切换：

| 数据源 | 地址 | 期数 | 限流 | 用途 |
|--------|------|------|------|------|
| `pc28.help` CSV | `pc28.help` | 最多 30000 | 有 | 全量回补 |
| `www.pc28.help` | pc28.help 镜像 | 最多 30000 | 限流策略不同 | 备用全量 |
| `wh28.com` history | `wh28.com` | 最新 100 | 无 | 增量更新（优先） |
| `wh28.com` trend | `wh28.com` | 最新 30 | 无 | 备用增量 |

```bash
python collector.py                 # 增量更新 (优先 wh28, 失败切 pc28)
python collector.py --full 5000     # 全量拉取 5000 期
python collector.py --full 30000    # 全量拉取最大 30000 期
python collector.py --source wh28   # 指定数据源
python collector.py --test          # 测试所有数据源连通性
python collector.py --verify        # 校验数据库
```
