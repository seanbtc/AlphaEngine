"""钉钉通知模块."""
import hashlib
import hmac
import base64
import time
import urllib.parse
import requests


class DingTalk:
    def __init__(self, cfg: dict):
        self.enabled = cfg.get("enabled", False)
        self.webhook = cfg.get("webhook_url", "").strip()
        self.secret = cfg.get("secret", "").strip()
        self.session = requests.Session()

    def _sign_url(self) -> str:
        if not self.secret:
            return self.webhook
        ts = str(round(time.time() * 1000))
        string_to_sign = f"{ts}\n{self.secret}"
        hmac_code = hmac.new(
            self.secret.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256
        ).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        sep = "&" if "?" in self.webhook else "?"
        return f"{self.webhook}{sep}timestamp={ts}&sign={sign}"

    def send(self, content: str) -> bool:
        if not self.enabled or not self.webhook:
            return False
        url = self._sign_url()
        payload = {"msgtype": "text", "text": {"content": content}}
        try:
            resp = self.session.post(url, json=payload, timeout=10)
            data = resp.json()
            if data.get("errcode") == 0:
                return True
            print(f"[DingTalk] Send failed: {data.get('errmsg', resp.text[:100])}")
        except Exception as e:
            print(f"[DingTalk] Error: {e}")
        return False

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
                 post_no: int = None) -> str:
        """发送分析通知。返回构建的帖子文本 (供 Promo 桥接复用)."""
        text = self.analysis_text(summary, cycle, conf, alpha, signals, post_no)
        self.send(text)
        return text

    def analysis_text(self, summary: str, cycle: str, conf: str, alpha: float,
                      signals: list, post_no: int = None) -> str:
        """构建分析帖子文本 — 无图标/无 markdown 加粗, 适合直接发布."""
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
