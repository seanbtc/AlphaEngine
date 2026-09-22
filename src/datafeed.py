"""DataFeed 服务客户端 — 实时价格 + K 线 (Binance U 本位合约口径).

endpoint 为 DataFeed 服务基址 (如 http://127.0.0.1:9550):
    GET {base}/price?symbol=BTCUSDT                  → {ok, price, ts, stale, source, error}
    GET {base}/klines?symbol=BTCUSDT&interval=1d&limit=N
        → {ok, stale, error, source, bars:[{open_time, open, high, low, close, volume, close_time}]}

失败只返回 None 并打印日志, 不抛异常 (调用方跳过对应数据即可)。
"""
import requests


class DataFeed:
    def __init__(self, cfg: dict):
        self.enabled = cfg.get("enabled", False)
        self.mode = cfg.get("mode", "http")
        self.endpoint = str(cfg.get("endpoint") or "").rstrip("/")
        self.symbol = cfg.get("symbol", "BTC/USDT")
        self.timeout = int(cfg.get("timeout_seconds", 10) or 10)
        # stale 价格默认仍返回 (DataFeed 明确标注的缓存价, 仅告警); 从严可置 true
        self.strict_stale_price = bool(cfg.get("strict_stale_price", False))

    def _symbol_param(self) -> str:
        return str(self.symbol or "").replace("/", "").upper()

    def get_price(self) -> float | None:
        if not self.enabled or not self.endpoint:
            return None
        url = f"{self.endpoint}/price"
        try:
            resp = requests.get(url, params={"symbol": self._symbol_param()},
                                timeout=self.timeout)
            if resp.status_code != 200:
                print(f"[DataFeed] /price HTTP {resp.status_code}")
                return None
            data = resp.json()
            if not data.get("ok"):
                print(f"[DataFeed] /price ok=false: {data.get('error') or data}")
                return None
            price = data.get("price")
            if price in (None, ""):
                print("[DataFeed] /price 响应缺少 price")
                return None
            if data.get("stale"):
                print(f"[DataFeed] /price 价格 stale "
                      f"(source={data.get('source')}, ts={data.get('ts')})")
                if self.strict_stale_price:
                    return None
            return float(price)
        except Exception as exc:
            print(f"[DataFeed] /price 请求失败: {exc}")
            return None

    def get_klines(self, interval: str = "1d", limit: int = None) -> dict | None:
        """取 K 线; 返回 {ok, stale, error, source, bars} 或 None (非 200/异常)."""
        if not self.enabled or not self.endpoint:
            return None
        url = f"{self.endpoint}/klines"
        params = {"symbol": self._symbol_param(), "interval": interval}
        if limit is not None:
            params["limit"] = int(limit)
        try:
            resp = requests.get(url, params=params, timeout=self.timeout)
            if resp.status_code != 200:
                print(f"[DataFeed] /klines HTTP {resp.status_code} "
                      f"(interval={interval}, limit={limit})")
                return None
            data = resp.json()
            if not isinstance(data, dict):
                print("[DataFeed] /klines 响应不是对象")
                return None
            return {
                "ok": bool(data.get("ok")),
                "stale": bool(data.get("stale")),
                "error": data.get("error"),
                "source": data.get("source") or "",
                "bars": data.get("bars") or [],
            }
        except Exception as exc:
            print(f"[DataFeed] /klines 请求失败: {exc}")
            return None
