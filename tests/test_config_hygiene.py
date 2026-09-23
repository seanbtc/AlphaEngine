"""WP7 ④: 配置卫生 — 死参数已移除 + 配置键均有引用 + 新键在位."""
import json
import sys
from pathlib import Path

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

_CONFIG_FILE = _ALPHA_ROOT / "config.json"

_REMOVED_KEYS = [
    "deprecated_term_trigger_count",
    "auto_calibrate_new_terms",
    "auto_calibrate_thresholds",
    "require_confirm_regime_change",
    "require_confirm_threshold_change",
    "max_total_tweets",
]

_ACTIVE_KEYS = [
    "retry_on_fetch_failure",
    "retry_delay_seconds",
    "image_cache_max_entries",
    "image_cache_max_bytes",
]


def _config() -> dict:
    return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))


def _leaf_items(obj, prefix=""):
    for key, value in obj.items():
        if key.startswith("_"):
            continue
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            yield from _leaf_items(value, path)
        else:
            yield path, key, value


def _source_text() -> str:
    parts = []
    for path in sorted((_ALPHA_ROOT / "src").glob("*.py")):
        parts.append(path.read_text(encoding="utf-8", errors="replace"))
    parts.append((_ALPHA_ROOT / "README.md").read_text(encoding="utf-8"))
    return "\n".join(parts)


def test_removed_dead_keys_absent():
    text = json.dumps(_config(), ensure_ascii=False)
    for key in _REMOVED_KEYS:
        assert f'"{key}"' not in text, key


def test_active_new_keys_present():
    keys = {key for _, key, _ in _leaf_items(_config())}
    for key in _ACTIVE_KEYS:
        assert key in keys, key


def test_every_config_key_is_referenced():
    """每个叶子配置键都能在 src/ 或 README 中找到引用 (无"看似生效实则无效"的键)."""
    text = _source_text()
    missing = [path for path, key, _ in _leaf_items(_config()) if key not in text]
    assert missing == []


def test_review_section_has_no_dead_schedule_day():
    """review.schedule_day 从未生效 (复盘固定月末 20:00 UTC), 不得再出现."""
    assert "schedule_day" not in _config().get("review", {})


def test_retry_delay_within_documented_clamp():
    schedule = _config()["schedule"]
    assert schedule["retry_on_fetch_failure"] is True
    assert 0 <= schedule["retry_delay_seconds"] <= 30


def test_dingtalk_credentials_not_plaintext():
    dingtalk = _config()["dingtalk"]
    webhook = str(dingtalk.get("webhook_url", "") or "")
    secret = str(dingtalk.get("secret", "") or "")
    assert "access_token=" not in webhook
    assert secret == ""


def test_image_cache_defaults_documented():
    ai = _config()["ai_service"]
    assert ai["image_cache_max_entries"] == 32
    assert ai["image_cache_max_bytes"] == 64 * 1024 * 1024
