"""配置加载器。"""
import json
import os


def load_config(config_path: str = None) -> dict:
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), "..", "config.json")
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"config.json not found at {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_data_dir(cfg: dict) -> str:
    data_dir = cfg.get("paths", {}).get("data_dir", "data")
    if not os.path.isabs(data_dir):
        data_dir = os.path.join(os.path.dirname(__file__), "..", data_dir)
    return os.path.normpath(data_dir)


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)
