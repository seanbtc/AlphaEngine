# AlphaEngine/tools — 独立工具

本目录工具与主分析管线隔离（不参与抓取/分析/发单）。当前工具：

## xwatch（独立 X 观察通道）

- 采集关注账号时间线（fxtwitter；按 id 去重只追加），数据只写 `data/xwatch/`（已 gitignore）。
- 命令（从 `AlphaEngine` 目录运行）：
  - 正式采集：`python3 tools/xwatch.py`（`--dry-run` 预览不写盘）
  - 账号验证卡：`python3 tools/xwatch.py --check "Dune,SlowMist_Team" [--json]`
    —— 只读体检：拉取数 / 转推数 / **过滤转推后的留存条数与样本** / 发帖频率；用于账号池新增准入。
- 退出码：0 全部/部分成功；1 全部失败或状态写失败；2 配置缺失/非法。

## 账号池治理（`xwatch_config.json`）

单一事实源；`accounts` = 实际采集名单；`pool` = 治理元数据（xwatch 运行时忽略该键，供治理工具消费）。

- tier：`active`（正式）/ `trial`（试用或复评中，带 `review_after`）/ `removed`（移除留痕，不在 `accounts`）。
- 一致性约定：`pool` 中非 removed 键集合应等于 `accounts`（`ops/xdigest_stats.py` 会做差集提示）。

流程：

1. **新增**：候选先 `--check` 体检（活跃 + 过滤转推后留存充足 + 内容契合人工确认）
   → 加入 `accounts` 并记 `pool{tier: trial, added: 当天, review_after: +14d, note}`
   → 先 `--dry-run` 抽查留存内容 → 正式并入后预览首期（必要时 `xdigest --mark-seen` 跳过回填洪峰）。
2. **评审**：跑贡献报表 `python3 ops/xdigest_stats.py --days 30`（工作区/Mac）
   → 按「试用到期 / 零入选 / 停更 ≥30 天 / 采集错误」提示复评 → 转正 / 延长 / 移除。
3. **移除**：从 `accounts` 删除 → `pool` 标 `removed{removed_at, reason}` → 工作区 project-memory 记一行。
   旧条目保留在 `tweets.jsonl` 不清洗；增量 offset 机制保证不再进报。
4. **复活**：轻量重走新增流程（加回 `accounts` + `tier: trial` + note 注明）。

> 观察备注：xwatch 过滤转推（retweet whitelist 为空），部分账号页面观感与"留存密度"差异大，
> 一律以 `--check` / `--dry-run` 实测为准。
