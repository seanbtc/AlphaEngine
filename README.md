# Glassnode Alpha Engine

BTC 链上数据驱动的**市场估值评估器**。抓取 @glassnode 推文 → AI 分析 → 直接输出 0–1 市场估值（0=极度低估，1=极度高估）→ 推送钉钉/Promo；无仓位、无发单、无状态机。

## 架构

```
┌─────────────────────────────────────────────────────────────────┐
│                      Daily Cycle (每天一次)                        │
│                                                                  │
│  RSS/Nitter                                                      │
│     │                                                            │
│     ▼                                                            │
│  Fetcher ──► tweets.jsonl (去重追加)                              │
│     │                                                            │
│     ▼ (有新推文)                                                  │
│  Analyzer ──► AIService / DeepSeek                               │
│     │          ├─ valuation (0–1) + confidence + evidence         │
│     │          ├─ evidence_scores (5 维度评分, 辅助解释)            │
│     │          ├─ meta (漂移检测)                                  │
│     │          └─ signal_board (可追溯信号)                       │
│     ▼                                                            │
│  StateManager ──► state.json (v2: valuation/runtime/ma/thesis)    │
│     │                                                            │
│     ├─► 估值历史 (valuation_history.json)                         │
│     ├─► 跨档变更通知 (钉钉) / 编号帖子 (Promo)                     │
│     └─► thesis / scorecard / manifest (反馈闭环, 只记录)           │
│     ▼                                                            │
│  Knowledge ──► drift_log / prediction_log / 周度蒸馏               │
│  Memory ─────► memory.md / metrics.json / valuation_history.json  │
│  DataFeed ───► BTC 价格 + 日线均线/周期上下文 (可选)                │
│  DingTalk ───► 钉钉推送 (估值变更 + 分析 + 告警)                    │
└─────────────────────────────────────────────────────────────────┘
```

## 核心概念

### 市场估值（0 ~ 1）

| 区间 | 档位 | 含义 |
|---|---|---|
| **0.00–0.20** | 极度低估 | 多维极值共振向下：投降/亏损兑现/机构撤离到极致，价格远低于全网成本 |
| **0.20–0.50** | 轻度低估 | 多数维度偏便宜：低估证据占优但未到极值，或极值信号刚出现 |
| **0.50–0.80** | 轻度高估 | 多数维度偏昂贵：获利了结/杠杆偏热/资金流入放缓 |
| **0.80–1.00** | 极度高估 | 多维极值共振向上：MVRV/NUPL 极端，LTH 大规模派发，机构流入见顶 |

区间为左闭右开（0.20 → 轻度低估，0.50 → 轻度高估，0.80 → 极度高估）。

### 估值语义（只有评估，没有执行）

- 估值分由 **AI 每轮直接输出**（`valuation` + `valuation_confidence` + `valuation_evidence`）；
- 本地只做范围校验、档位映射、变更留痕；**不做平滑/冷却/连续确认/状态机推进**；
- 无新推文或数据源故障时，估值保持不变（不做时间外推、不按自然日漂移）；
- 移除全部执行链：无仓位建议、无 TradeSync 发单、无节奏门控、无影子账本、无风险预算；
- 持久状态 `state.json` 为 **v2 schema**：`valuation` 块取代旧的 `regime`/`alpha`/`evidence`；
  旧 v1 状态启动时自动迁移（保留 `runtime`/`ma`/`thesis`，估值置空等待下一轮 AI 评估）。

### 交叉验证（valuation.cross_check）

同一 prompt 采样 `samples` 次（默认 2，1=关闭）：

- 样本**跨档**不一致 → 降级 `valuation_confidence=low`；
- 档内数值离散（spread > 0.15）/ 预测方向不一致 → 仅写入 `cross_check` 审计块留痕，不改变 live 置信。

### 证据系统

5 个评分维度，每条 -1.0 到 +1.0：
- **profitability** — 盈利性指标 (MVRV, NUPL, SOPR)
- **institutional** — 机构行为 (ETF 流量, 交易所余额)
- **onchain** — 链上结构 (LTH 行为, SEC, Revived Supply)
- **derivatives** — 衍生品 (资金费率, 期权偏度, IV)
- **macro** — 宏观 (活跃地址, 稳定币, 实际利率)

证据累积为正则支持 bullish→bear 的过渡，为负则支持 bearish→bull。空闲周期衰减（每日 -0.01）。

### 移动均线结构（每轮评审锚点）

每轮 AI 评审前，引擎经 DataFeed 服务取日线 K 线（`GET /klines?symbol=BTCUSDT&interval=1d`，
Binance U 本位口径），计算 5/10/20 EMA + 50/100/200/250 SMA，向 AI 提供价格所在区间、
趋势斜率与近期均线事件（穿越/金叉死叉/斜率翻转），并写入 `data/ma_history.jsonl`
按日去重的历史快照——AI 因此能看到"上次评审区间 → 本次变化"的演化，而非孤立快照。
均线仅辅助判断价格相对成本结构的位置（贵/便宜），不构成短期交易信号。

数据源**不回退本地归档**：DataFeed 不可用（`ok=false`/异常）或 `stale=true` 时，
均线段整体缺省并打印 `[MA]` 错误日志，主流程继续。

| 均线 | 语义 |
|---|---|
| 5 EMA | ⚡动能 |
| 10 EMA | 🔍短期趋势 |
| 20 EMA | 🎯均值回归 |
| 50 SMA | 🛡️强劲上升趋势支撑 |
| 100 SMA | 📉回调买入警报 |
| 200 SMA | 🔄趋势转变 |
| 250 SMA | 💰公允价值 |

区间档位：强势多头区 / 上升趋势回调区 / 趋势转变观察区 / 转弱/反抽区 / 空头区。
配置见 `config.json` → `ma_context`（归档路径、均线周期、斜率阈值、事件窗口、历史保留条数）。

每轮正常分析还会把 K 线趋势摘要（`available/as_of/zone/price` + `updated_at`）与估值一起
写入 `state.json` 的 `ma` 字段，供 Web 面板只读展示（`--test-ai`/回溯等只读路径不写）；
Web 侧由 `Web/web.py::load_alpha_engine_ma` 读取。

### 周期上下文（cycle_context）

在均线结构之外，每轮 AI 评审还会收到"周期定位与历史类比"小节（`src/cycle_context.py`，
纯计算、无新依赖），固定包含：

- **周期位置**：ATH（收盘/最高价口径 + 日期）、现价与距 ATH 回撤、近 365 日周期低点与低点恢复%、
  距低点/距 ATH/距上次减半（2024-04-20）天数；
- **顶部风险**（`cycle_context.top_risk`，价格结构警示，带滞回 + since 锁存）：
  进入 = 连续 2 日满足"距滚动 90 日最高收盘回撤 ≥ 10% 且 收盘跌破 SMA50 且 收盘仍高于 200SMA"，
  或当日回撤 ≥ 11% 立即进入；退出 = 收盘 > SMA50（结构修复）或回撤收窄至 ≥ 9%
  （可选 `exit_below_200sma=true` 时收盘跌破 200SMA 也退出）。`since` 为当前风险簇最初激活日
  （新激活距簇首 ≤ `since_latch_days=30` 交易日则沿用，否则重置），`active` 随
  `since`/`reasons`/回撤/距 200SMA 一起进入 AI 上下文。该口径替代原"距 200SMA>30%"独立触发
  ——旧口径在牛市频繁误报（两轮牛市激活占比 52.5%/25.5%）且漏掉 2025-10 浅顶（仅 +17.8%）；
  滞回口径 W1 段数 3（目标 ≤3）、占比 27.1%，W2 14 段/22.8%，2021-11-08/2025-10-06
  两顶命中 +10d/+5d，2025 顶后 3 段且 `since` 恒为 2025-10-11（修复 ③↔② 闪烁与 since 失真）。
  顶部风险须与链上/宏观/新闻证据（Glassnode 内容）结合判断，不单独构成结论；
- **阶段判定**：固定规则按 ④→③→②→① 顺序匹配（阈值可配），未命中为"过渡期"：
  ④熊市（close<SMA200 且 200SMA↓）｜③中后期/顶部（**顶部风险激活** 或 **估值 ≥ 0.80**）｜
  ②结构确认（250SMA↑）｜①复苏早期（close>SMA200 且 250SMA↓）；
- **趋势关键值**：200/250 SMA 值与斜率、距 200SMA%、近 30/90 日站上 200SMA 天数、区间档位
  （传入 `ma_context` 时复用其 zone/days_above/zone_changes）；
- **四组历史类比**（收盘口径，连续日去重 + 相邻≤5 交易日去抖，前瞻 30/90/180/365 交易日中位/正收益率/极值）：
  A 首次上穿 200SMA｜B 当前状态（>200SMA、200↑、250↓、距 200SMA 10-30%、距 ATH>30%）｜
  C 250SMA 斜率转正｜D 距 200SMA>20% 首入；B/D 附未来 180 日最大回撤中位/最差；
- **风险提示**：样本量、预热（200SMA 自 2020-07、250SMA 自 2020-09）、截断剔除与阈值去抖说明。

数据来自 DataFeed `/klines`（`kline_limit=3000` 覆盖全历史，`min_bars=300` 兜底），失败/stale/
数据不足时该段整体缺省并打印 `[Cycle]` 日志，主流程继续；`--test-ai`/回溯等只读路径同样只读不落盘。
周期定位与历史类比仅辅助判断 cycle_position/confidence，样本量小（尤其 B），不构成短期交易信号。
配置见 `config.json` → `cycle_context`（统计窗口、去抖间隔、阶段/类比阈值、减半日期）。

## 快速开始

### 安装

```bash
cd glassnode-engine
pip install -r requirements.txt
pip install snscrape   # 可选：批量历史抓取
```

### 配置

编辑 `config.json`：

```json
{
  "ai_service": {
    "enabled": true,
    "endpoint": "http://127.0.0.1:5010"   // 统一 AI 服务地址 (API Key/模型见 AIService/config.json)
  },
  "dingtalk": {
    "enabled": true,
    "webhook_url": ""                     // 留空或填 ${DINGTALK_WEBHOOK} 占位; 凭证走环境变量
  }
}
```

钉钉凭证优先读环境变量（部署侧由工作区根 `.env` 经 `start_all.py` 加载，本地可自行 export）：

```bash
ALPHAENGINE_DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx  # 服务专属 (优先, 与 Sentinel 等分离)
ALPHAENGINE_DINGTALK_SECRET=SEC...    # 可选, 专属加签机器人
DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=xxx  # 共享回退
DINGTALK_SECRET=SEC...    # 可选, 共享加签机器人
```

- 读取优先级：`ALPHAENGINE_DINGTALK_WEBHOOK`（服务专属）> `DINGTALK_WEBHOOK`（兼容 `DINGTALK_WEBHOOK_URL`）> `config.json`；secret 同理（`ALPHAENGINE_DINGTALK_SECRET` > `DINGTALK_SECRET` > config）；
- 专属变量缺失时自动回退共享变量（兼容现状）；可用专属变量做服务级覆盖，与其它服务（如 Sentinel）分离；
- `config.json` 的 `webhook_url`/`secret` 留空或填 `${DINGTALK_WEBHOOK}` 占位即可（占位视为未配置），**明文 token 不得入库**；
- 启动日志只打印凭证来源名与 webhook 掩码（access_token 末 6 位），不打印完整 URL/token；
- 环境变量与 config 均为空且 `enabled=true` → 启动打印告警，通知自动禁用（不崩、不影响主流程）；
- `.env` 已被 `.gitignore` 忽略，不要提交。

### 首次运行（需海外网络）

```bash
# 1. 批量抓取历史推文
python -m src.alpha --bulk 3000

# 2. 启动引擎（自动回溯 + 监控）
python -m src.alpha

# 或常驻
nohup python3 -u -m src.alpha > engine.log 2>&1 &
```

### 日常使用

```bash
python -m src.alpha --status    # 查看当前估值 + 分析计数
python -m src.alpha --once      # 手动跑一次
python -m src.alpha --backfill  # 强制重新回溯历史推文, 重设启动时的 alpha
python -m src.alpha             # 常驻监控 (每天检测一次)
```

### 导入外部推文数据

```bash
python -m src.alpha --import tweets_backup.jsonl
# 支持 JSONL: 每行一个 {"id":"...","date":"...","content":"..."}
# 支持 JSON 数组: [{"id":"...","date":"...","content":"..."}, ...]
```

## 项目结构

```
glassnode-engine/
├── config.json                    # 全部配置
├── requirements.txt               # Python 依赖
├── README.md
├── run.py                         # 根启动器
│
├── src/
│   ├── __init__.py
│   ├── config_loader.py           # 配置加载 + 路径解析
│   ├── fetcher.py                 # RSS 多源 + snscrape 批量 + 文件导入
│   ├── analyzer.py                # DeepSeek API (system prompt + JSON 修复)
│   ├── valuation.py               # 0–1 估值: 档位/解析/变更文案 (唯一口径)
│   ├── ma_context.py              # 日线均线结构 (区间/趋势/事件 + 历史快照)
│   ├── cycle_context.py           # 周期定位/阶段判定/四组历史类比 (辅助参考)
│   ├── state_manager.py           # state.json v2 持久化 + v1 迁移 + 损坏恢复
│   ├── memory.py                  # 双存储 (memory.md + metrics + valuation_history)
│   ├── knowledge.py               # 蒸馏/漂移检测/预测审计 + 价格快照
│   ├── thesis.py                  # 可证伪命题 (label=估值档位, 仅追溯)
│   ├── scorecard.py               # 成绩单 (近期审计 + 估值轨迹)
│   ├── datafeed.py                # BTC 价格获取
│   ├── notify.py                  # 钉钉推送
│   └── run.py                     # 主入口 + 回溯 + 循环调度
│
├── tools/                         # 只读报告工具
│   └── xwatch.py                  # X 抓取诊断
│
└── data/                          # 运行时自动生成
    ├── state.json                 # 持久状态 v2 (valuation, runtime, ma, thesis)
    ├── tweets.jsonl               # 推文存档 (JSONL)
    ├── memory.md                  # 叙事记忆
    ├── metrics.json               # 结构化指标
    ├── valuation_history.json     # 估值历史 (最近 500 条, 损坏恢复依据)
    ├── knowledge_base.md          # 自进化知识库
    ├── drift_log.jsonl            # 漂移追踪
    ├── prediction_log.jsonl       # 预测日志 (valuation / forecast / cross_check)
    ├── price_history.jsonl        # 价格快照 (趋势 + 预测审计)
    ├── theses.jsonl               # 命题事件 (created/updated/invalidated/expired)
    ├── run_manifest.jsonl         # 每轮估值前后/输入摘要 (审计)
    └── ma_history.jsonl           # 日线均线快照历史 (按日去重, 跨轮连续性)
```

## 首次回溯流程

```
引擎检测 analysis_count == 0 (或 --backfill 强制)
  │
  ├─ 1. snscrape 批量抓取 (limit=2000)
  │     失败 → RSS 补充 → 文件导入
  │
  ├─ 2. 智能采样 (2000→200 条, 关键推文全保留)
  │
  ├─ 3. 分批送 AI (每批 5 条)
  │     收集 valuation + evidence_scores
  │
  └─ 4. 以最后一个有效批次的估值作为初始估值 (最新信息, 不做投票)
```

重启时**无需清空 data 目录**。若想重新回溯历史推文重建估值：

```bash
python -m src.alpha --backfill   # 强制重跑回溯 (以最新批次估值重建)
```

`tweets.jsonl` 是回溯的数据源，请勿删除；`state.json` 保存运行状态，重启自动恢复。

## 4 个估值档位

| 档位 | 区间 | AI 输出倾向 |
|---|---|---|
| `极度低估` | 0.00 ≤ v < 0.20 | 多维极值共振向下（投降/机构撤离/深度亏损） |
| `轻度低估` | 0.20 ≤ v < 0.50 | 多数维度偏便宜，但未到极值 |
| `轻度高估` | 0.50 ≤ v < 0.80 | 多数维度偏昂贵：获利了结/杠杆偏热 |
| `极度高估` | 0.80 ≤ v ≤ 1.00 | 多维极值共振向上（MVRV/NUPL 极端/LTH 派发） |

档位由本地 `src/valuation.py` 按区间映射（AI 只需输出 0–1 数值与证据）。

## 自我进化

### 漂移检测（每轮）

- **新术语**：同一新指标在推文中出现 ≥5 次 → 钉钉告警
- **质量骤降**：连续 3 轮 analysis_quality ≤5 → 钉钉告警

### 预测日志（prompt/模型可追溯）

每条预测（`data/prediction_log.jsonl`）记录本轮判断的版本信息：

- `prompt_hash`：`SYSTEM_PROMPT` 的 sha256 前 12 位（模块级常量 `PROMPT_HASH`，prompt 改动即变、同 prompt 稳定）；旧记录无该字段，审计读取兼容（不强制存在）；
- `model`：取自 AIService 响应（服务端实际映射的模型）；拿不到时省略字段；
- `temperature`：请求温度（客户端配置值）；拿不到时省略字段。

`state.runtime.last_prompt_hash` 随每轮分析成功同步，便于对账"某轮判断由哪版 prompt 产生"。

### 预测校准（K-03）

- **来源标注**：`signal_board[].source_type` = `retrieved` / `remembered` / `inferred`；`retrieved` 必须带 `source`；`source_type_counts` 随预测落盘。
- **区间预测**：`forecast` = `horizon_days(30/90)` + `direction` + `price_low/high` + `confidence` + `hit_probability` + `basis`；非法/缺失置 `null`，不阻断分析。
- **重跑离散**：`_cross_check` 采样 2 次（可配 `valuation.cross_check.samples`），落盘 `cross_check` 块（valuations / valuation_spread / bands / band_agreement / forecast_direction_agreement / dispersed）。只有**跨档**不一致才降 `valuation_confidence=low`；档内离散/方向离散仅留痕。
- **审计**：`audit_predictions` 对 forecast 取 target ±7 天最近价（平局偏好 after；`before` 表示 horizon 被缩短），落盘 `interval_hit` / `direction_hit` / `brier` / `width_pct` / `horizon_price_offset_days` / `horizon_price_source`；旧预测仍按方向审计。
- **口径注意**：direction 命中阈值与 point 一致（up/down ±2%、flat ±3%）；估值→预期方向按 `<0.45 up / >0.55 down / 其余 flat` 推导（旧记录按原 cycle_position 兜底）；90 天 flat 的命中率需谨慎解读。
- **周度蒸馏**：知识蒸馏会把预测校准摘要写入 prompt，用于归纳模型过/欠置信区间；月度复盘与参数自动校准已随 regime 状态机移除。

### 知识蒸馏（每周日 0 UTC）

AI 自动运行：
1. 压缩 memory.md（超出 10000 字符时）
2. 审计历史预测（30 天窗口）
3. 生成新 knowledge_base.md（最可靠信号、常见误判、周期特殊性）

### 状态恢复

程序 Ctrl+C 正常退出时自动保存 state.json。若文件损坏/版本未知，备份损坏文件并从 `valuation_history.json` 重建（估值 + 分析计数）；v1 状态自动迁移为 v2。

## 钉钉推送

凭证配置见「配置」节：优先服务专属环境变量 `ALPHAENGINE_DINGTALK_WEBHOOK` / `ALPHAENGINE_DINGTALK_SECRET`，缺失时回退共享 `DINGTALK_WEBHOOK`（兼容 `DINGTALK_WEBHOOK_URL`）/ `DINGTALK_SECRET`（部署侧由工作区根 `.env` 加载），`config.json` 仅保留空值或 `${DINGTALK_WEBHOOK}` 占位；均未配置时通知禁用并告警。日志只打印来源名与 webhook 掩码（access_token 末 6 位）。

| 事件 | 推送内容 |
|---|---|
| **估值变更（跨档）** | 🟢 0.42 轻度低估 → 0.62 轻度高估，置信 + 依据 |
| **分析结果** | 估值 + 档位 + 关键信号 + 评估总结 + 预测 |
| **风格漂移** | ⚠ 新术语/质量骤降 |

## Promo 帖子推送

编号帖子经 HTTP POST 推送至 Promo 事件服务（`PROMO_EVENTS_URL`），失败即丢弃、不写文件桥。仅**发往 Promo 的载荷文本**按 `promo.term_replacements` 做品牌词避让（大小写不敏感、按序替换，如 `Coinbase Premium` → `美资现货溢价`）；钉钉消息、记忆/知识库与 AI 原始输出保持原文。配置缺失/非法、替换后为空 → 原文透传并打日志。

## 接入 DataFeed (BTC 价格 / K 线)

```json
"datafeed": {
    "enabled": true,
    "endpoint": "http://127.0.0.1:9550",
    "symbol": "BTC/USDT",
    "timeout_seconds": 10,
    "strict_stale_price": false
}
```

`endpoint` 为 DataFeed 服务基址：

- `GET /price?symbol=BTCUSDT` → `{ok, price, ts, stale, source, error}`（`stale=true` 默认仍采用并告警，`strict_stale_price=true` 则视为不可用）
- `GET /klines?symbol=BTCUSDT&interval=1d&limit=N` → `{ok, stale, error, source, bars:[...]}`（均线结构用）

## 配置调优

| 场景 | 参数调整 |
|---|---|
| 关闭交叉验证（降成本） | `valuation.cross_check.samples`: 2→1 |
| 减少 AI 成本 | 降 `backfill.max_analysis_samples`: 200→100 |
| 加速回溯 | 增 `backfill.batch_size`: 5→10 (可能截断), 增 `max_tokens`: 4096→8192 |
| 调整分析频率 | `schedule.daily_time` / `schedule.weekdays` |

## 故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| `No JSON in response` | API 返回不完整 | 增 `max_tokens` 或减 `batch_size` |
| `Empty response` | API 错误 | 查看 `finish_reason`；等 5 分钟重试 |
| `Snscrape not installed` | 未安装 | `pip install snscrape` |
| `IP 不在白名单中` | 钉钉安全设置 | 把服务器 IP 加入钉钉机器人白名单 |
| `All RSS sources failed` | 网络问题 | 等服务恢复；或用 `--import` 手动导入 |
| 估值长期不动 | 无新推文 / 数据源故障 | 正常（估值不随时间漂移），等新数据 |
| 钉钉重复推送 | 回溯阶段 | 正常，仅首次回溯时有 |

## 安全注意事项

- `config.json` 不应包含 API key / webhook token 等敏感信息；钉钉凭证走环境变量或 `.env`（已 gitignore），**明文 token 不得入库**
- 若凭证曾以明文提交过，应到钉钉后台轮换 token（历史提交无法撤回），并只更新服务器 `.env`
- `data/` 目录下的文件均为运行时数据，无需版本控制
