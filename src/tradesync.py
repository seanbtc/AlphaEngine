"""TradeSync 交易指令接口 — 输出仓位指令."""
import json
import os
from datetime import datetime


class TradeSync:
    def __init__(self, cfg: dict, data_dir: str):
        self.cfg = cfg
        self.enabled = cfg.get("enabled", False)
        self.mode = cfg.get("mode", "file")
        self.output_dir = os.path.join(data_dir, cfg.get("output_dir", "orders").lstrip("/\\"))
        self.http_endpoint = cfg.get("http_endpoint", "")
        self.api_key = cfg.get("api_key", "")
        self._last_order = None

    def _ensure_dir(self):
        os.makedirs(self.output_dir, exist_ok=True)

    def _direction(self, alpha: float) -> str:
        if alpha > 0.05:
            return "long"
        elif alpha < -0.05:
            return "short"
        return "cash"

    def _size_pct(self, alpha: float) -> float:
        return round(abs(alpha) * 100, 1)

    def send_order(self, alpha: float, regime: str, btc_price: float = None) -> dict | None:
        if not self.enabled:
            return None

        direction = self._direction(alpha)
        size = self._size_pct(alpha)
        order = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "alpha": alpha,
            "regime": regime,
            "direction": direction,
            "size_pct": size,
            "btc_price": btc_price,
            "action": "close_all" if direction == "cash" else "adjust",
        }

        if self._last_order:
            if (self._last_order.get("direction") == direction and
                abs(self._last_order.get("size_pct", 0) - size) < 1.0):
                return None

        if self.mode == "file":
            self._ensure_dir()
            filename = f"{datetime.utcnow().strftime('%Y%m%d')}.jsonl"
            filepath = os.path.join(self.output_dir, filename)
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(json.dumps(order, ensure_ascii=False) + "\n")
            print(f"[TradeSync] Order written to {filepath}: {direction} {size}%")

        elif self.mode == "http" and self.http_endpoint:
            try:
                import requests
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                }
                resp = requests.post(self.http_endpoint, json=order, headers=headers, timeout=10)
                print(f"[TradeSync] HTTP {resp.status_code}: {direction} {size}%")
            except Exception as e:
                print(f"[TradeSync] HTTP error: {e}")
                return None

        self._last_order = order
        return order
