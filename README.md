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
| 空闲锁定 | 无新推文时 α 不动 | 不被噪音驱动 |
| 低置信锁定 | confidence=low 时 α 不动 | 不确定时不动 |
| 每日一步 | 每天只分析一次 (86400s) | 杜绝日内高频跳变 |
| 步进上限 | ±0.02/次 (2%) | 从 0 到 ±1 需 50 天 (每周约 7 步) |
| 冷却期 | 10 步 (10天) | regime 变更后禁止再变 |
| 共识门槛 | ≥3 类别、总分 ≥2.0 | 防止单一维度误导 |
| 合法转换 | 预定义转换表 | 禁止非法跳变 |

### 证据系统

5 个评分维度，每条 -1.0 到 +1.0：
- **profitability** — 盈利性指标 (MVRV, NUPL, SOPR)
- **institutional** — 机构行为 (ETF 流量, 交易所余额)
- **onchain** — 链上结构 (LTH 行为, SEC, Revived Supply)
- **derivatives** — 衍生品 (资金费率, 期权偏度, IV)
- **macro** — 宏观 (活跃地址, 稳定币, 实际利率)

证据累积为正则支持 bullish→bear 的过渡，为负则支持 bearish→bull。空闲周期衰减（每日 -0.01）。

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
  "deepseek": {
    "api_key": "sk-xxxxxxxx"   // DeepSeek API key (platform.deepseek.com)
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
python -m src.run --bulk 3000

# 2. 启动引擎（自动回溯 + 监控）
python -m src.run

# 或常驻
nohup python3 -u -m src.run > engine.log 2>&1 &
```

### 日常使用

```bash
python -m src.run --status    # 查看 regime + alpha
python -m src.run --once      # 手动跑一次
python -m src.run --backfill  # 强制重新回溯历史推文, 重设启动时的 alpha
python -m src.run             # 常驻监控 (每天检测一次)
```

### 导入外部推文数据

```bash
python -m src.run --import tweets_backup.jsonl
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
│   ├── state_manager.py           # state.json 持久化 + 脏写合批
│   ├── memory.py                  # 双存储 (memory.md + metrics + alpha_history)
│   ├── knowledge.py               # 蒸馏/漂移检测/预测审计
│   ├── tradesync.py               # 交易指令输出 (file/http)
│   ├── datafeed.py                # BTC 价格获取
│   ├── notify.py                  # 钉钉推送
│   └── run.py                     # 主入口 + 回溯 + 循环调度
│
└── data/                          # 运行时自动生成
    ├── state.json                 # 持久状态 (regime, alpha, evidence, runtime)
    ├── tweets.jsonl               # 推文存档 (JSONL)
    ├── memory.md                  # 叙事记忆
    ├── metrics.json               # 结构化指标
    ├── alpha_history.json         # Alpha 时间线 (最近 500 条)
    ├── knowledge_base.md          # 自进化知识库
    ├── drift_log.jsonl            # 漂移追踪
    ├── prediction_log.jsonl       # 预测日志
    ├── calibration_log.jsonl      # 校准审计
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
python -m src.run --backfill   # 强制重跑回溯 (重置证据, 重设 regime/alpha)
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

## 接入 DataFeed (BTC 价格)

```json
"datafeed": {
    "enabled": true,
    "endpoint": "http://localhost:8080/api/btc/price"
}
```

端点应返回 `{"price": 64600.0}` 或 `{"last": 64600}` 或 `{"close": 64600}`。

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
