# AlphaEngine 预测校准观察计划（K-03）

- 建立：2026-10-21
- 范围：AlphaEngine 预测审计校准（source_type / forecast / cross_check / calibration）
- 分析节奏：**周二 / 周四 12:25 北京时间**（04:25 UTC）；首次部署后下一轮为 **2026-10-22 12:25 北京**
- 数据：服务器 `/home/bot/AlphaEngine/data/`（prediction_log / prediction_outcomes / params / calibration_log）
- 红线：只观察，不自动改参数；不因校准数据产生 `adjustments`（B1 已修复并有回归测试）

## 1. 首次检查（2026-10-22 12:30 北京）

```bash
ssh ubuntu@43.164.132.13 "pgrep -af 'src.alpha' | head -3; tail -n 80 /home/bot/logs/alphaengine.log"
ssh ubuntu@43.164.132.13 "cd /home/bot/AlphaEngine && sudo python3 tools/calibration_report.py"
```

看：

- `[Analyzer] Input / Got chars`、`VALIDATION failed`、retry 次数是否异常
- `交叉验证` 日志：position 不一致（降 low）vs progress/方向“仅留痕”
- `state.runtime.last_prompt_hash` 是否变为新 hash
- `prediction_log.jsonl` 新行是否含 `forecast` / `source_type_counts` / `cross_check`
- `forecast` 非空率、`source_type_counts.retrieved_no_source` 比例
- `cycle_confidence=low` 占比是否异常上升；`[REGIME]` 是否有非预期变更
- `[Audit]` 的 `new_audited / skipped`

## 2. 定期节奏

| 时间 | 动作 | 重点 |
|---|---|---|
| **2026-10-22 12:30** | 首轮检查 | JSON 合规、字段落盘、live 行为 |
| **每周日 21:00** | 周检查 | validation/retry、forecast 非空率、source_type、low 占比、audit |
| **2026-11-20**（30 天） | 第一次校准检查 | pending/awaiting/skipped、coverage/Brier/ECE（样本很少，只看方向） |
| **2026-12-20**（60 天） | 第二次 | 按 confidence / horizon / price_source 分桶 |
| **2027-01-19**（90 天） | 决策检查 | 是否调整 prompt/区间口径；不自动改参数 |

## 3. 周检查命令

```bash
# 进程/日志
ssh ubuntu@43.164.132.13 "pgrep -af 'src.alpha'; tail -n 120 /home/bot/logs/alphaengine.log"

# 校准报告（服务器本地读取，只读）
ssh ubuntu@43.164.132.13 "cd /home/bot/AlphaEngine && sudo python3 tools/calibration_report.py"

# 月度复盘/蒸馏后检查自动校准是否有 prediction_calibration 驱动
ssh ubuntu@43.164.132.13 "cd /home/bot/AlphaEngine && sudo cat data/calibration_log.jsonl | tail -5"
```

## 4. 观察指标与阈值

| 指标 | 正常 | 警戒/动作 |
|---|---|---|
| `VALIDATION failed` | 0–偶发 | 连续 2 轮失败 → 查 prompt/JSON，必要时回滚 |
| `forecast` 非空率 | ≥70% | 连续 3 轮 <50% → 调整 prompt 长度/字段说明 |
| `retrieved_no_source` | 低 | 明显偏高 → 提示模型必须带 source，或加强校验 |
| `cycle_confidence=low` 占比 | 与部署前同量级 | 明显上升 → 查交叉验证/结构检查是否误伤 |
| `[REGIME]` 变更 | 与既有节奏一致 | 非预期变更 → 立即人工复核 |
| `pending/awaiting_horizon` | 正常积累 | 长期不降 → 查审计调度/horizon 价 |
| `by_price_source.before` | 低 | 偏高 → 重新评估 ±7 天窗口/数据缺口 |
| `coverage/Brier/ECE` | 30 天后才有意义 | 样本 <10 不下结论 |

## 5. 回滚/停手

- **回滚锚点**：部署前 AlphaEngine HEAD（提交后记录具体 hash）；只重启 `alphaengine`
- 触发回滚：连续 validation 失败、live 置信/regime 异常、审计异常影响主流程、任何非预期下单（AlphaEngine 不下单，但需确认链路）
- 数据兼容：prediction_log / outcomes 为 append-only；旧代码忽略新字段，回滚不丢历史
- 不自动改参数：`apply_calibration` 只读 `review_result["calibration"]`；prediction_calibration 只留档/蒸馏

## 6. 90 天决策

只有同时满足才考虑调整（仍不自动改）：

1. 无 validation/审计异常；
2. forecast 非空率稳定 ≥70%；
3. coverage 与 `hit_probability` 的偏差可解释（ECE/Brier 有样本）；
4. `by_price_source.before` 占比可接受；
5. 交叉验证 position 不一致率未异常上升；
6. 月度复盘未出现由 prediction_calibration 驱动的 `adjustments`。

结论只用于 prompt/区间口径与人工评审，不直接改 live 参数。
