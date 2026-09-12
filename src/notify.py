"""钉钉通知模块 (发送经 commons.notify 共享实现)."""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from commons.notify import DingTalkNotifier


class DingTalk:
    def __init__(self, cfg: dict):
        self.enabled = cfg.get("enabled", False)
        self.webhook = cfg.get("webhook_url", "").strip()
        self.secret = cfg.get("secret", "").strip()
        self._notifier = DingTalkNotifier(
            self.webhook, self.secret, enabled=self.enabled, timeout=10
        )

    def send(self, content: str) -> bool:
        return self._notifier.send(content)

    # ---- 模板 ----

    def regime_change(self, fr: str, to: str, reason: str, alpha: float, target: float = None,
                      old_alpha: float = None):
        arrow = "🟩" if alpha > 0 else ("🟥" if alpha < 0 else "⬜")
        if old_alpha is not None and old_alpha != alpha:
            alpha_line = f"Alpha: {old_alpha:+.4f} → {alpha:+.4f}"
        else:
            alpha_line = f"Alpha: {alpha:+.4f}"
        if target is not None:
            alpha_line += f" (目标: {target:+.4f})"
        return self.send(
            f"{arrow} **Regime 变更**\n\n"
            f"{fr} → {to}\n"
            f"{alpha_line}\n"
            f"原因: {reason}"
        )

    def alpha_change(self, old: float, new: float, regime: str, btc_price=None, target: float = None):
        arrow = "🟢" if new > old else ("🔴" if new < old else "⚪")
        price_str = f" | BTC ${btc_price:,.0f}" if btc_price else ""
        tgt = new if target is None else target
        direction = "做多" if tgt > 0 else ("做空" if tgt < 0 else "观望")
        return self.send(
            f"{arrow} **Alpha 变更**\n\n"
            f"Regime: {regime}{price_str}\n"
            f"Alpha: {old:+.4f} → {new:+.4f} (target: {tgt:+.2f} {direction})"
        )

    def analysis(self, summary: str, cycle: str, conf: str, alpha: float, signals: list,
                 post_no: int = None, engine_regime: str = None) -> str:
        """发送分析通知。返回构建的帖子文本 (供 Promo 桥接复用)."""
        text = self.analysis_text(summary, cycle, conf, alpha, signals, post_no, engine_regime)
        self.send(text)
        return text

    def analysis_text(self, summary: str, cycle: str, conf: str, alpha: float,
                      signals: list, post_no: int = None, engine_regime: str = None) -> str:
        """构建分析帖子文本 — 无图标/无 markdown 加粗, 适合直接发布.

        cycle 为 AI 判定的周期位置; engine_regime 为引擎当前确认的周期.
        两者不一致时并列显示 (AI判定 vs 引擎状态), 避免误导.
        """
        if engine_regime and engine_regime != cycle:
            header = (f"周期判定: {cycle} (置信: {conf}) | 引擎: {engine_regime} | "
                      f"Alpha: {alpha:+.4f}")
        else:
            header = f"周期: {cycle} (置信: {conf}) | Alpha: {alpha:+.4f}"
        if post_no is not None:
            header = f"No.{post_no} | {header}"
        lines = [header + "\n"]
        if summary:
            lines.append(summary)
        if signals:
            lines.append("\n关键信号:")
            for i, s in enumerate(signals[:5], 1):
                name = s.get('name', '?')
                cat = s.get('category', '')
                detail = s.get('detail', '')
                prefix = f"{i}. [{cat}] {name}" if cat else f"{i}. {name}"
                lines.append(f"{prefix}: {detail}")
        return "\n".join(lines)

    def alert(self, title: str, body: str = ""):
        return self.send(f"⚠️ **{title}**\n\n{body}" if body else f"⚠️ **{title}**")
