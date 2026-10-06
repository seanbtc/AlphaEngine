"""钉钉通知模块 (发送经 commons.notify 共享实现).

凭证读取优先级: 服务专属环境变量 > 共享环境变量 > config.json。
- webhook: ALPHAENGINE_DINGTALK_WEBHOOK (专属) > DINGTALK_WEBHOOK
  (兼容 DINGTALK_WEBHOOK_URL) > config dingtalk.webhook_url
- secret:  ALPHAENGINE_DINGTALK_SECRET (专属) > DINGTALK_SECRET (加签, 可选) > config
config 中留空或填 ${DINGTALK_WEBHOOK} 占位即可; 均无 → 通知禁用并告警。
日志只打来源名与 webhook 掩码 (access_token 末 6 位), 不打印完整 URL。
"""
import os
import re
import sys
import urllib.parse
from datetime import datetime

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from commons.notify import DingTalkNotifier

_ENV_WEBHOOK_NAMES = ("ALPHAENGINE_DINGTALK_WEBHOOK", "DINGTALK_WEBHOOK",
                      "DINGTALK_WEBHOOK_URL")
_ENV_SECRET_NAMES = ("ALPHAENGINE_DINGTALK_SECRET", "DINGTALK_SECRET")
_PLACEHOLDER_RE = re.compile(r"^\$\{[A-Za-z0-9_]+\}$")


def mask_webhook(webhook) -> str:
    """返回 webhook 中 access_token 的掩码串 (仅保留末 6 位; 规则同 Sentinel).

    只输出掩码本身, 不含 URL/查询键名, 供日志安全展示。
    """
    raw = str(webhook or "").strip()
    if not raw:
        return "<未配置>"
    parsed = urllib.parse.urlsplit(raw)
    token = (urllib.parse.parse_qs(parsed.query).get("access_token") or [""])[0]
    if not token:
        return "<已配置>"
    return ("*" * max(len(token) - 6, 0)) + token[-6:]


def _resolve_credential(env_names, config_value):
    """按 环境变量 > config 解析凭证; 返回 (值, 来源). 占位值视为未配置。"""
    for name in env_names:
        value = str(os.getenv(name, "") or "").strip()
        if value and not _PLACEHOLDER_RE.match(value):
            return value, f"env:{name}"
    value = str(config_value or "").strip()
    if not value or _PLACEHOLDER_RE.match(value):
        return "", ""
    return value, "config"


class DingTalk:
    def __init__(self, cfg: dict):
        self.enabled = cfg.get("enabled", False)
        self.webhook, self.webhook_source = _resolve_credential(
            _ENV_WEBHOOK_NAMES, cfg.get("webhook_url", ""))
        self.secret, self.secret_source = _resolve_credential(
            _ENV_SECRET_NAMES, cfg.get("secret", ""))
        self._notifier = DingTalkNotifier(
            self.webhook, self.secret, enabled=self.enabled, timeout=10,
            quiet_errors=True
        )
        # 失败可观测 (内存计数, 不改变发送语义): print_status 展示
        self.failure_count = 0
        self.last_error = ""
        self.last_failure_at = ""
        if self.enabled and not self.webhook:
            print("[DingTalk] WARNING: 未配置 webhook "
                  "(环境变量 ALPHAENGINE_DINGTALK_WEBHOOK/DINGTALK_WEBHOOK/"
                  "DINGTALK_WEBHOOK_URL 与 config dingtalk.webhook_url 均为空), "
                  "通知已禁用")
        elif self.enabled:
            if self.webhook_source:
                print(f"[DingTalk] webhook 来源: {self.webhook_source} "
                      f"(掩码: {mask_webhook(self.webhook)})")
            if self.secret_source:
                print(f"[DingTalk] secret 来源: {self.secret_source}")

    def send(self, content: str) -> bool:
        try:
            ok = bool(self._notifier.send(content))
        except Exception as exc:
            self._record_failure(exc)
            return False
        if not ok and self._configured():
            # 未配置/禁用 (enabled=false 或 webhook 为空) 的 False 不算失败, 避免状态行误报
            self._record_failure("send 返回 False")
        return ok

    def _configured(self) -> bool:
        return bool(self.enabled) and bool(self.webhook)

    def _record_failure(self, error) -> None:
        self.failure_count += 1
        self.last_error = str(error)
        self.last_failure_at = datetime.utcnow().isoformat() + "Z"

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
