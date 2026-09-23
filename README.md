# Glassnode Alpha Engine

BTC 链上数据驱动的仓位（alpha）管理系统。抓取 @glassnode 推文 → DeepSeek AI 分析 → 周期判定 → α 平滑 → TradeSync 指令，钉钉推送全程监控。

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
│  Analyzer ──► DeepSeek API                                       │
│     │          ├─ cycle_position (7 阶段)                         │
│     │          ├─ evidence_scores (5 维度评分)                     │
│     │          ├─ meta (漂移检测)                                  │
│     │          └─ signal_board (可追溯信号)                       │
│     ▼                                                            │
│  AlphaEngine ──► Regime 状态机                                    │
│     │          ├─ EvidenceAccumulator (累积/衰减)                 │
│     │          ├─ 3 类别共识、±0.05 步进、10 周期冷却             │
│     │          └─ DEEP_BULL(-1.0) … BEAR_BOTTOM(+1.0)            │
│     ▼                                                            │
│  Knowledge ──► drift_log / prediction_log                        │
│     │          ├─ 风格漂移检测 (新术语、质量骤降)                   │
│     │          ├─ 周度蒸馏 (knowledge_base.md)                    │
│     │          └─ 预测审计                                        │
│     ▼                                                            │
│  StateManager ──► state.json (重启恢复)                           │
│  Memory ────────► memory.md / metrics.json / alpha_history.json   │
│  TradeSync ────► data/orders/ (交易指令, 默认关闭)                 │
│  DataFeed ─────► BTC 价格 (可选, 默认关闭)                         │
│  DingTalk ─────► 钉钉推送 (regime/alpha 变更 + 分析 + 告警)        │
└─────────────────────────────────────────────────────────────────┘
```

## 核心概念

### Alpha 值（-1 ~ 1）

| 值 | 含义 | 对应 Regime |
|---|---|---|
| **+1.00** | 满仓做多 | BULL (牛市多次确认) |
| **+0.70** | 建仓做多 | RECOVERY (牛市恢复确认) |
| **+0.30** | 减仓多单（轻持） | DEEP_BULL (接近牛顶, 减仓) |
| **0.00** | 中性/清仓 | BEAR_BOTTOM(清空做空) / BULL_COOLING(清空做多) |
| **-0.30** | 减仓空单（轻持） | BEAR_DEEP (深熊, 减仓做空, 等底部) |
| **-0.70** | — 不直接设目标 — | (平滑过渡中) |
| **-1.00** | 满仓做空 | BEAR (熊市多次确认) |

### 仓位管理：逐步确认，缓慢累积

仓位不是"确认即满仓"，而是"确认改变方向，多次确认逐步积累"。

```
完整牛熊周期:

熊底 → 牛初 → 牛中 → 牛顶 → 转熊 → 熊中 → 深熊 → 熊底
  │       │      │      │      │      │      │      │
  ▼       ▼      ▼      ▼      ▼      ▼      ▼      ▼
清空    建仓   满仓   减仓   清空   满仓   减仓   清空
做空    做多   做多   做多   做多   做空   做空   做空
(=0)   (+0.7) (+1.0) (+0.3) (=0)  (-1.0) (-0.3) (=0)
```

| 步骤 | Regime | Alpha 目标 | 操作 |
|---|---|---|---|
| 1. 熊底确认 | BEAR_BOTTOM | 0.00 | 清仓做空，中性等确认 |
| 2. 牛市恢复 | RECOVERY | +0.70 | 多次确认牛市中 → 建仓多单 |
| 3. 牛市确认 | BULL | +1.00 | 连续确认 → 满仓做多 |
| 4. 接近牛顶 | DEEP_BULL | +0.30 | 过热信号 → 减仓多单 |
| 5. 牛顶确认 | BULL_COOLING | 0.00 | 确认转熊 → 清仓多单 |
| 6. 熊市确认 | BEAR | -1.00 | 多次确认熊市中 → 满仓做空 |
| 7. 深熊减仓 | BEAR_DEEP | -0.30 | 接近底部 → 减仓做空，等确认 |

### 稳定机制

| 规则 | 参数 | 效果 |
|---|---|---|
| 空闲时间推进 | 无新推文时 α 按自然日推进 (不响应噪音) | 周期钟与日历一致, 与调度频率解耦 |
| 低置信锁定 | confidence=low 时 α 不动 | 不确定时不动 |
| 每日一步 | 每天只分析一次 (86400s) | 杜绝日内高频跳变 |
| 自然日步长 | `min_daily_step`/`max_change_per_step` (单轮边界 0.015/0.05) | 步长 = 自然日数/regime 预期天数 (2.33 天≈0.023, RECOVERY) |
| 冷却期 | 10 轮 (决策机会数, 非自然日) | regime 变更后禁止再变 |
| 共识门槛 | ≥3 类别、总分 ≥2.0 | 防止单一维度误导 |
| 质量门槛 | `evidence.min_quality_for_regime_change` (默认 5) | 分析质量不足时拒绝 regime 变更 |
| 连续确认 | `stability.required_confirmations` (默认 2) | 同一提议连续 N 轮分析成功才执行变更 |
| 交叉验证 | `cross_check.samples` (默认 2, 1=关闭) | 同 prompt 多采样, `cycle_position` 不一致 → 降为 low |
| 结构一致性 | `cross_check.structure_check.enabled` (默认开) | 提议增加多头暴露且 close<SMA200 → 降为 low (减仓/清仓方向不拦截) |
| 合法转换 | 预定义转换表 | 禁止非法跳变 |

### 时间语义（自然日推进）

- **progress/alpha 按自然日推进**：`state.runtime.last_tick_at` 记录上次推进时刻，每轮
  `days_elapsed = clamp((now - last_tick_at) / 86400, 0, max_catchup_days)`（配置
  `alpha.time_semantics.max_catchup_days`，默认 7 天）。空闲轮 `progress += days_elapsed / 预期天数`；
  分析轮步长 = `days_elapsed / 预期天数`，再 clamp 到 `min_daily_step`/`max_change_per_step`
  （仍为"单轮"边界）。按 `schedule.weekdays` 配置的任意调度（如 3 次/周）周期钟都与日历一致
  ——旧逻辑每轮 `+1/预期天数`，慢 `7/3≈2.33×`；步长旧逻辑因 `1/90 < 下限` 恒为下限，"动态"失效。
- **幂等与容错**：旧 state 无 `last_tick_at` 时首轮仅初始化锚点（不推进）；同秒/同日重复
  运行 `elapsed≈0`，不重复推进；停摆超过 `max_catchup_days` 的部分直接丢弃（不补记）；
  时间倒流不回退锚点。
- **轮次语义保留**：`tick_cooldown`/`tick_stability` 仍按"轮次"（决策机会数）计数，与连续
  确认制/冷却门配套，与自然日推进相互独立（以 3 次/周计，10 轮≈3.3 周而非 10 天；
  具体以 `schedule.weekdays` 配置为准）。
- **故障恢复不补记**：`fetch_error`/`analysis_failed` 故障期间周期钟冻结（WP5），恢复时
  `_clear_outage` 把锚点重置为 now —— 故障时长不计入 progress/步长（恢复轮增量≈0），
  恢复后从下一轮起按自然日推进。
- **锚表对齐 4 年周期**：`REGIME_EXPECTED_DAYS` 各 regime 预期天数按 1461 天对齐（旧表
  1270 天等比放大 ≈×1.15 后取整），合计 = 1461；`config.json.alpha.regime_expected_days`
  可覆盖（值 clamp [1, 500]），需与 `src/alpha_engine.py` 锚表保持同步。
- **校准落盘**：月度复盘 `apply_calibration` 在内存生效的同时写 `data/params.json`
  （键=参数路径，值=新值；tmp+原子替换）并 append `data/calibration_log.jsonl`
  （时间/参数/旧→新/来源）；启动时只读加载 overlay 恢复（非法文件告警并忽略），
  重启不再丢失校准结果；`--test-ai` 等只读入口不写任何文件。

连续确认细节：提议 `cp ≠ 当前 regime` 时写入 `state.regime.pending_proposal`
（同一 cp 连续出现计数 +1，换向重置为 1，提议回到当前 regime 或执行成功后清除）；
仅"有新推文且分析成功"的轮次计数，空闲轮不计数不重置；被冷却/低置信/质量/共识
等门拒绝时计数保留（下次继续累计）；首轮分析与回溯路径豁免确认（一次性引导），
但保留其它门。`required_confirmations=1` 等价于旧行为（可回退）。

交叉验证细节：`cross_check.samples>1` 时同一 prompt 调用 AI 多次（首次必须有效，
否则按原失败流程重试），样本 `cycle_position` 全部一致才按首样本返回；不一致时
仅把 `cycle_confidence` 降为 `low`（不新增/不改动其它输出字段），由既有低置信门
阻断 regime 变更与 alpha 步进。`samples=1` 完全回退旧行为。

结构一致性为**方向感知**：仅当提议 cp 为多头侧正暴露（`REGIME_ALPHA_MAP[cp] > 0`）
且 `> 当前 alpha + 0.005`（加多）且 close < SMA200 时降级。熊侧空头减仓
（`BEAR→BEAR_DEEP`、`BEAR_DEEP→BEAR_BOTTOM`，目标 alpha ≤ 0）与牛侧减仓/清仓
（`BULL→DEEP_BULL`、`DEEP_BULL→BULL_COOLING`，目标不高于当前）均不会被拦截——
前者在弱结构下是正常路径，后者是唯一降风险路径。`market_state` 缺 alpha 时回退为
仅 `cp == "BULL"` 触发；MA 缺失/stale 不干预。

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
均线仅辅助判断周期位置是否与价格结构一致，不构成短期交易信号。

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

每轮正常分析还会把 K 线趋势摘要（`available/as_of/zone/price` + `updated_at`）随
regime/alpha 一起写入 `state.json` 的 `ma` 字段，供 Web 面板只读展示（`--test-ai`/回溯
等只读路径不写）；Web 侧由 `Web/web.py::load_alpha_engine_ma` 读取。

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
  ④熊市（close<SMA200 且 200SMA↓）｜③中后期/顶部（**顶部风险激活** 或 regime=BULL_COOLING；alpha 仅独立展示不参与判定，防时间自推自反馈）｜
  ②结构确认（250SMA↑ 且 regime∈{RECOVERY,BULL}）｜①复苏早期（regime∈{BEAR_BOTTOM,RECOVERY} 且 close>SMA200 且 250SMA↓）；
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
    "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=xxx"
  }
}
```

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
python -m src.alpha --status    # 查看 regime + alpha
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
│   ├── alpha_engine.py            # Regime 状态机 + 证据累积 + α 平滑
│   ├── ma_context.py              # 日线均线结构 (区间/趋势/事件 + 历史快照)
│   ├── cycle_context.py           # 周期定位/阶段判定/四组历史类比 (辅助参考)
│   ├── state_manager.py           # state.json 持久化 + 脏写合批
│   ├── memory.py                  # 双存储 (memory.md + metrics + alpha_history)
│   ├── knowledge.py               # 蒸馏/漂移检测/预测审计
│   ├── tradesync.py               # 交易指令输出 (file/http)
│   ├── datafeed.py                # BTC 价格获取
│   ├── notify.py                  # 钉钉推送
│   └── run.py                     # 主入口 + 回溯 + 循环调度
│
└── data/                          # 运行时自动生成
    ├── state.json                 # 持久状态 (regime, alpha, ma, evidence, runtime)
    ├── tweets.jsonl               # 推文存档 (JSONL)
    ├── memory.md                  # 叙事记忆
    ├── metrics.json               # 结构化指标
    ├── alpha_history.json         # Alpha 时间线 (最近 500 条)
    ├── knowledge_base.md          # 自进化知识库
    ├── drift_log.jsonl            # 漂移追踪
    ├── prediction_log.jsonl       # 预测日志
    ├── ma_history.jsonl           # 日线均线快照历史 (按日去重, 跨轮连续性)
    ├── params.json                # 校准参数覆盖 (启动时加载, 重启不丢失)
    ├── calibration_log.jsonl      # 校准审计 (时间/参数/旧→新/来源)
    └── orders/                    # 交易指令输出 (默认关闭)
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
  ├─ 3. 分批送 DeepSeek (每批 5 条)
  │     收集 cycle_position + evidence_scores
  │
  ├─ 4. 投票决定最终 regime (最近 30% 批次众数)
  │
  └─ 5. 一次性执行 regime + 直接设 alpha
```

重启时**无需清空 data 目录**。若想重新回溯历史推文确认启动时的 alpha：

```bash
python -m src.alpha --backfill   # 强制重跑回溯 (重置证据, 重设 regime/alpha)
```

`tweets.jsonl` 是回溯的数据源，请勿删除；`state.json` 保存运行状态，重启自动恢复。

## 7 个周期位置

```
BULL(+1.0) → DEEP_BULL(+0.3) → BULL_COOLING(0) → BEAR(-1.0) → BEAR_DEEP(-0.3) → BEAR_BOTTOM(0) → RECOVERY(+0.7) → BULL(+1.0)
```

| 位置 | Alpha目标 | 含义 | 仓位操作 |
|---|---|---|---|
| `BULL` | +1.00 | 牛市确认 | 多次确认牛市 → 满仓做多 |
| `DEEP_BULL` | +0.30 | 接近牛顶 | 过热信号 → 减仓多单 |
| `BULL_COOLING` | 0.00 | 牛顶确认 | 确认转熊 → 清仓多单 |
| `BEAR` | -1.00 | 熊市确认 | 多次确认熊市 → 满仓做空 |
| `BEAR_DEEP` | -0.30 | 深熊 | 接近底部 → 减仓做空，等确认 |
| `BEAR_BOTTOM` | 0.00 | 熊底确认 | 底部信号 → 清仓做空，等翻多 |
| `RECOVERY` | +0.70 | 牛市恢复 | 多次确认牛市中 → 建仓做多 |

## 自我进化

### 漂移检测（每轮）

- **新术语**：同一新指标在推文中出现 ≥5 次 → 钉钉告警
- **质量骤降**：连续 3 轮 analysis_quality ≤5 → 钉钉告警
- **术语弃用**：旧指标连续 10 轮不出现 → 标记弃用

### 知识蒸馏（每周日 0 UTC）

AI 自动运行：
1. 压缩 memory.md（超出 10000 字符时）
2. 审计历史预测（30 天窗口）
3. 生成新 knowledge_base.md（最可靠信号、常见误判、周期特殊性）

### 状态恢复

程序 Ctrl+C 正常退出时自动保存 state.json。若文件损坏，从 alpha_history.json 重建。

## 钉钉推送

| 事件 | 推送内容 |
|---|---|
| **Regime 变更** | 🟩 BEAR → BEAR_DEEP, alpha, 原因 |
| **Alpha 变更** | 🟢 +0.30 → +0.35, BTC $64K |
| **分析结果** | 🧠 周期位置 + 信号板 |
| **风格漂移** | ⚠ 新术语/质量骤降 |

## 接入 TradeSync

`config.json` 中启用：

```json
"tradesync": {
    "enabled": true,
    "mode": "file",
    "output_dir": "orders"
}
```

输出格式（`data/orders/YYYYMMDD.jsonl`）：

```json
{"timestamp":"2026-08-06T12:00:00Z","alpha":0.50,"regime":"BEAR_DEEP",
 "direction":"long","size_pct":50.0,"btc_price":64600,"action":"adjust"}
```

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
| 更敏感的 regime 切换 | 降 `min_total_score`: 2.0→1.5, 降 `min_categories`: 3→2 |
| 更稳定的 alpha | 降 `max_change_per_step`: 0.05→0.02, 升 `cooldown_cycles`: 10→20 |
| 减少 DeepSeek 成本 | 降 `max_analysis_samples`: 200→100 |
| 加速回溯 | 增 `batch_size`: 5→10 (可能截断), 增 `max_tokens`: 4096→8192 |

## 故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| `No JSON in response` | API 返回不完整 | 增 `max_tokens` 或减 `batch_size` |
| `Empty response` | API 错误 | 查看 `finish_reason`；等 5 分钟重试 |
| `Snscrape not installed` | 未安装 | `pip install snscrape` |
| `IP 不在白名单中` | 钉钉安全设置 | 把服务器 IP 加入钉钉机器人白名单 |
| `All RSS sources failed` | 网络问题 | 等服务恢复；或用 `--import` 手动导入 |
| Alpha 长期不动 | 无新推文 + 已到 target | 正常，等新数据 |
| 钉钉重复推送 | 回溯阶段 | 正常，仅首次回溯时有 |

## 安全注意事项

- `config.json` 内含 API key 和 webhook token，**不应提交到公开仓库**
- 建议生产环境改用环境变量读取敏感配置
- `data/` 目录下的文件均为运行时数据，无需版本控制
