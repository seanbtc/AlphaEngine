"""DataFeed 价格数据接口 — 获取 BTC 实时价格."""
import requests


class DataFeed:
    def __init__(self, cfg: dict):
        self.enabled = cfg.get("enabled", False)
        self.mode = cfg.get("mode", "http")
        self.endpoint = cfg.get("endpoint", "")
        self.symbol = cfg.get("symbol", "BTC/USDT")
        self.timeout = int(cfg.get("timeout_seconds", 10) or 10)

    def get_price(self) -> float | None:
        if not self.enabled or not self.endpoint:
            return None
        try:
            resp = requests.get(self.endpoint, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                price = data.get("price") or data.get("last") or data.get("close")
                if price:
                    return float(price)
        except Exception as e:
            print(f"[DataFeed] Error: {e}")
        return None
