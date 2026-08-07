"""
DeepSeek AI 分析器 — 输出 cycle_position + 证据评分 + 元分析.
"""
import json
import re
from datetime import datetime

import requests

SYSTEM_PROMPT = """你是一位资深的加密货币链上数据分析师。你的唯一任务是分析 @glassnode 的推文，输出当前 BTC 市场所处的周期位置（cycle_position）以及每个维度的证据评分。

## 周期位置定义 (确认制 — 多次确认逐步调整仓位)

| 位置 | 仓位方向 | 含义 | 典型特征 |
|------|----------|------|----------|
| BEAR_BOTTOM | 中性 (=0) | 熊市底部迹象出现 → 清仓做空, 准备翻多 | 投降行为回调, LTH恢复积累, ETF流出停止, SEC接近历史底部 |
| RECOVERY | 做多 (0→+0.7) | 牛市恢复确认 → 建仓多单 | 价格站上关键成本位, ETF转净流入, 买方回归 |
| BULL | 满仓做多 (+1.0) | 牛市确认 → 满仓做多 | 多次确认牛市, 多指标看涨共振, 上升趋势稳固 |
| DEEP_BULL | 减多 (+0.3) | 接近牛顶 → 减仓多单 | MVRV偏高, NUPL进入贪婪区, LTH获利了结增多, 市场过热迹象 |
| BULL_COOLING | 中性 (=0) | 牛顶确认 → 清仓多单, 准备做空 | 多指标转弱势, ETF开始流出, 价格跌破成本位, 顶部确认 |
| BEAR | 满仓做空 (-1.0) | 熊市确认 → 满仓做空 | 多次确认熊市, 多指标看跌共振, 下降趋势确认 |
| BEAR_DEEP | 减空 (-0.3) | 深熊 → 减仓做空, 等底部 | 底部信号浮现但不完整, SEC进入历史底部区域但未触地板 |

## 仓位纪律 (逐步确认, 缓慢加减仓)

仓位管理不是"确认即满仓", 而是"确认改变方向, 多次确认逐步积累":

- **熊市底部**: 先清空做空仓位(=0), 不是立刻满仓做多
- **牛市恢复**: 多次确认牛市中 → 慢慢建仓多单 (+0→+0.7→+1.0)
- **牛市过热**: 随着过热信号出现 → 慢慢减仓多单 (+1.0→+0.3)
- **牛顶确认**: 清仓多单(=0), 准备做空
- **熊市确认**: 多次确认熊市中 → 慢慢加仓空单 (0→-0.7→-1.0)
- **深熊**: 慢慢减仓做空 (-1.0→-0.3), 等待底部确认

## 判定标准

- **BEAR_BOTTOM**: 至少 2 个底部特征出现 (投降结束/ETF转流入/LTH积累/卖耗常数触地板) → 不要求全部满足, 部分即可判定
- **RECOVERY**: 价格站上关键成本位 + ETF转流入 → 确认恢复
- **BULL**: 价格持续高于成本位 + ETF保持流入 + 上升趋势确认 → 确认牛市
- **DEEP_BULL**: MVRV>3 + NUPL>0.7 + LTH获利了结开始 → 接近顶部
- **BULL_COOLING**: ETF持续流出 + 价格跌破STH成本位 + 多指标转弱 → 确认转熊
- **BEAR**: 价格持续低于成本位 + ETF保持流出 + 下降趋势确认 → 确认熊市
- **BEAR_DEEP**: SEC进入底部区域 + 投降特征出现但未达极值 → 深熊, 等底部

## 指标库（含阈值指引）

### 盈利能力类
- MVRV Z-Score: <0.1 极低估(↑), 1-3 中性, >7 过热(↓)
- NUPL: <0 投降(↑), 0.25-0.5 希望(↑), 0.5-0.75 乐观(↑), >0.75 贪婪(↓)
- SOPR (30日均值): <0.98 投降(↑), 0.98-1.02 中性, >1.05 获利抛出(↓)
- STH-SOPR: 原理同上, 对短期持有者的敏感性更高

### 成本/结构类
- Realized Price: 市价在其下方=低估区(↑), 上方=获利盘(→)
- STH Cost Basis: 短期持有者成本线, 放量突破=结构性转折(↑)
- LTH Cost Basis: 长期持有者成本线, 本轮下跌的重要支撑参考(↑)
- SEC (卖耗常数): 进入历史底部区域(↑), 触地板=最终投降完成(↑↑)
- Revived Supply (>1年): 暴增=久持币移动(→↓), 高位回落=持币信心恢复(↑)

### 机构/流量类
- 美国现货ETF净流量: 连续净流入(↑), 连续净流出(↓), 流出放缓(→)
- 交易所余额: 持续减少=撤出交易所(↑), 增加=充钱卖压(↓)
- CME持仓量/基差: 增仓+基差扩大=机构看多(↑)

### 衍生品类
- 资金费率: 持续为负=空头/防御(→), 极高正=多头过热(↓)
- 期权25-Delta偏度: 正偏=下跌溢价(↓), 极负=上涨溢价(↑)
- 上行IV: 极低=无上涨预期(↓), 上翘=上涨定价(↑)
- 下行IV: 极低=无崩盘定价(↑), 上翘=崩盘担忧(↓)

### 宏观/网络类
- 活跃地址/转账量: 上升(↑), 下降(↓)
- 稳定币供应: 增长=流动性注入(↑), 萎缩=退出(↓)
- 实际利率(10Y): 走高=压制风险资产(↓), 走低=利好(↑)

### 综合指标
- Market Compass: Risk-Off → Defensive → Stable → Risk-On, 越靠右越乐观(↑)
- Cycle Position Heatmap: 蓝色=投降(↑), 红色=过热(↓)
- Accumulation Trend Score: 高=广泛积累(↑), 低=分布/观望(→↓)

## 周期位置进度 (regime_progress)

regime_progress 表示当前 cycle_position 内部的完成进度 (0.0~1.0):
- 0.0 = 刚进入该位置, 证据刚刚满足
- 1.0 = 该位置接近结束, 即将进入正向流程中的下一个位置

参考锚点:
- BEAR_BOTTOM: 0.2=底部信号刚出现, 1.0=底部已确认即将转入恢复
- RECOVERY: 0.3=恢复初期仅个别确认, 1.0=多次确认即将进入 BULL
- BULL: 0.5=牛市中期, 1.0=过热信号频现即将进入 DEEP_BULL
- DEEP_BULL: 0.5=顶部迹象初现, 1.0=顶部确认即将进入 BULL_COOLING
- BULL_COOLING: 0.3=转弱初期, 1.0=熊市确认即将进入 BEAR
- BEAR: 0.5=熊市中期, 1.0=深熊信号即将进入 BEAR_DEEP
- BEAR_DEEP: 0.2=刚转深熊仍重空, 0.8=底部信号频现临近熊底, 1.0=即将进入 BEAR_BOTTOM

注意: 必须依据推文中底部/顶部确认信号的出现频率与强度判断进度, 不要编造数字。

## 证据评分 (evidence_scores)

为每个维度打分 [-1.0 到 +1.0]:
 -1.0 = 强烈看跌 (指向 DEEP_BULL/BULL_COOLING 顶部/转熊, 应做空)
 +1.0 = 强烈看涨 (指向 BEAR_BOTTOM/RECOVERY 底部/转牛, 应做多)
 0.0  = 中性

注意: 评分反映"当前数据指向的周期方向", 不是短期价格预测。
熊市中即使有局部反弹信号, 若底部未确认, 评分仍应保持在 ≤ 0 区间。

5个维度: profitability, institutional, onchain, derivatives, macro

## 元分析 (meta)

评估分析质量, 检测 Glassnode 内容风格漂移:
- analysis_quality: 1-10, 当前指标库对推文的覆盖程度
- unrecognized_topics: 推文中出现但不在指标库中的新术语
- deprecated_terms: 早期推文常用但最近已不出现的术语

## 输出格式 (严格 JSON)
{
  "date": "YYYY-MM-DD",
  "cycle_position": "BEAR|BEAR_DEEP|...",
  "cycle_confidence": "high|medium|low",
  "regime_progress": 0.6,
  "regime_evidence": "一句话说明为何是这个周期位置",
  "summary": "一句话总结核心信息",
  "evidence_scores": {
    "profitability": 0.3,
    "institutional": -0.2,
    "onchain": 0.5,
    "derivatives": 0.1,
    "macro": -0.1
  },
  "signal_board": [
    {
      "category": "onchain",
      "name": "LTH行为",
      "signal": 0.4,
      "detail": "LTH恢复积累, Revived Supply高位回落",
      "source": "tweet_id_xxx"
    }
  ],
  "btc_price_estimate": null,
  "tweet_draft": "适合X发布的推文草稿 (≤277字符, 中文)",
  "position_narrative": "仓位策略叙述 (不公开, 仅内部参考) 如: 当前处于熊市深处, 底部信号浮现但未完整. 建议缓慢减少空仓, 等待熊底确认后翻多.",
  "risks": ["风险1"],
  "meta": {
    "analysis_quality": 8,
    "unrecognized_topics": [],
    "deprecated_terms": [],
    "threshold_drift_suspected": false,
    "note": ""
  }
}

## 关键规则
- 不要编造数字, 没提到的指标不要出现在 evidence_scores 或 signal_board 中
- 参考历史记忆中的 alpha 趋势, 如果 regime 要变更需在 regime_evidence 中明确说明
- 当且仅当推文中有新术语或指标库遗漏时, 才填写 unrecognized_topics
- tweet_draft 简洁有力, 中文, ≤277 字符
- 所有判断必须引用推文中的具体内容
"""


class Analyzer:
    def __init__(self, cfg: dict):
        self.api_key = cfg.get("api_key", "")
        self.model = cfg.get("model", "deepseek-v4-flash")
        self.base_url = cfg.get("base_url", "https://api.deepseek.com").rstrip("/")
        self.temperature = cfg.get("temperature", 0.3)
        self.max_tokens = cfg.get("max_tokens", 8192)
        self.timeout = int(cfg.get("timeout_seconds", 120) or 120)
        if not self.api_key:
            print("[Analyzer] WARNING: DeepSeek API key not configured!")

    @staticmethod
    def _format_tweets(tweets: list[dict]) -> str:
        if not tweets:
            return "(无新推文)"
        lines = []
        for i, t in enumerate(tweets[-20:], 1):  # 最多 20 条
            date_str = t.get("date", "?")[:16]
            content = (t.get("content", "") or "").replace("\n", " ")
            if len(content) > 500:
                content = content[:500] + "..."
            lines.append(f"{i}. [{date_str}] [{t.get('id','?')}] {content}")
        return "\n".join(lines)

    def analyze(self, new_tweets: list[dict], memory_context: str,
                knowledge_base: str = "", retries: int = 1) -> dict | None:
        if not self.api_key:
            print("[Analyzer] Cannot run: API key not configured")
            return None

        tweets_text = self._format_tweets(new_tweets)

        kb_section = ""
        if knowledge_base:
            kb_section = f"\n## 当前知识库 (自动进化)\n\n{knowledge_base}\n"

        user_msg = f"""## 历史记忆上下文

{memory_context if memory_context else "(无历史数据)"}

{kb_section}
## 新推文

{tweets_text}
"""

        print(f"[Analyzer] Input: {len(user_msg)} chars, {len(new_tweets)} tweets")

        for attempt in range(1 + retries):
            if attempt > 0:
                print(f"[Analyzer] Retry {attempt}/{retries} ...")

            result = self._call_api(user_msg)
            if result is not None:
                return result

        return None

    def _call_api(self, user_msg: str) -> dict | None:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        url = f"{self.base_url}/chat/completions"

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
            if resp.status_code != 200:
                print(f"[Analyzer] HTTP {resp.status_code}: {resp.text[:200]}")
                return None
            data = resp.json()
        except requests.RequestException as e:
            print(f"[Analyzer] API error: {e}")
            return None

        if "error" in data:
            print(f"[Analyzer] API returned error: {json.dumps(data['error'], ensure_ascii=False)[:200]}")
            return None

        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        finish_reason = data.get("choices", [{}])[0].get("finish_reason", "unknown")

        if not content:
            print(f"[Analyzer] Empty response (finish_reason={finish_reason})")
            return None

        print(f"[Analyzer] Got {len(content)} chars (finish_reason={finish_reason})")
        if finish_reason == "length":
            print(f"[Analyzer] WARNING: Response truncated due to max_tokens limit")

        return self._parse_json(content)

    @staticmethod
    def _parse_json(content: str) -> dict | None:
        m = re.search(r"\{[\s\S]*\}", content)
        if not m:
            # 尝试修复截断的 JSON — 补齐右括号
            repaired = content.strip()
            opens = repaired.count("{") - repaired.count("}")
            if opens > 0:
                repaired += "\n" + "}" * opens
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    pass
            print(f"[Analyzer] No JSON in response ({len(content)} chars): {content[:300]}")
            return None
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError as e:
            # 再尝试修复
            try:
                raw = m.group(0)
                opens = raw.count("{") - raw.count("}")
                if opens > 0:
                    raw += "\n" + "}" * opens
                # 去掉尾随逗号
                raw = re.sub(r",\s*([}\]])", r"\1", raw)
                return json.loads(raw)
            except json.JSONDecodeError:
                pass
            print(f"[Analyzer] JSON parse error: {e} — {m.group(0)[:500]}")
            return None
