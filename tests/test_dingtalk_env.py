"""WP7 ②: 钉钉凭证 env 化 — 优先级/占位/缺失禁用/发送路径不变."""
import sys
from pathlib import Path

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.notify import DingTalk  # noqa: E402

_ENV_NAMES = ("DINGTALK_WEBHOOK", "DINGTALK_WEBHOOK_URL", "DINGTALK_SECRET")


def _clear_env(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


# ---- 优先级: 环境变量 > config ----

def test_env_overrides_config(monkeypatch):
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://env.example/hook")
    monkeypatch.setenv("DINGTALK_SECRET", "env-secret")

    dt = DingTalk({"enabled": True, "webhook_url": "https://config.example/hook",
                   "secret": "config-secret"})

    assert dt.webhook == "https://env.example/hook"
    assert dt.secret == "env-secret"
    assert dt.webhook_source == "env:DINGTALK_WEBHOOK"


def test_env_alias_webhook_url(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://alias.example/hook")

    dt = DingTalk({"enabled": True, "webhook_url": ""})

    assert dt.webhook == "https://alias.example/hook"
    assert dt.webhook_source == "env:DINGTALK_WEBHOOK_URL"


def test_primary_env_wins_over_alias(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://primary.example/hook")
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://alias.example/hook")

    dt = DingTalk({"enabled": True, "webhook_url": ""})

    assert dt.webhook == "https://primary.example/hook"


def test_config_used_when_env_absent(monkeypatch):
    _clear_env(monkeypatch)

    dt = DingTalk({"enabled": True, "webhook_url": "https://config.example/hook",
                   "secret": "config-secret"})

    assert dt.webhook == "https://config.example/hook"
    assert dt.secret == "config-secret"
    assert dt.webhook_source == "config"


# ---- 占位值与缺失 ----

def test_placeholder_treated_as_unconfigured(monkeypatch, capsys):
    _clear_env(monkeypatch)

    dt = DingTalk({"enabled": True, "webhook_url": "${DINGTALK_WEBHOOK}"})

    assert dt.webhook == ""
    assert dt.webhook_source == ""
    assert dt._configured() is False
    assert "通知已禁用" in capsys.readouterr().out


def test_missing_webhook_disabled_with_warning(monkeypatch, capsys):
    _clear_env(monkeypatch)

    dt = DingTalk({"enabled": True, "webhook_url": ""})

    assert dt.send("x") is False
    assert dt.failure_count == 0
    assert "通知已禁用" in capsys.readouterr().out


def test_placeholder_does_not_shadow_env(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://env.example/hook")

    dt = DingTalk({"enabled": True, "webhook_url": "${DINGTALK_WEBHOOK}"})

    assert dt.webhook == "https://env.example/hook"


def test_env_placeholder_treated_as_unset(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("DINGTALK_WEBHOOK", "${DINGTALK_WEBHOOK}")

    dt = DingTalk({"enabled": True, "webhook_url": "https://config.example/hook"})

    assert dt.webhook == "https://config.example/hook"
    assert dt.webhook_source == "config"


def test_env_placeholder_falls_through_to_alias(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("DINGTALK_WEBHOOK", "${DINGTALK_WEBHOOK}")
    monkeypatch.setenv("DINGTALK_WEBHOOK_URL", "https://alias.example/hook")

    dt = DingTalk({"enabled": True, "webhook_url": ""})

    assert dt.webhook == "https://alias.example/hook"
    assert dt.webhook_source == "env:DINGTALK_WEBHOOK_URL"


def test_env_placeholders_only_disabled(monkeypatch, capsys):
    _clear_env(monkeypatch)
    monkeypatch.setenv("DINGTALK_WEBHOOK", "${DINGTALK_WEBHOOK}")
    monkeypatch.setenv("DINGTALK_SECRET", "${DINGTALK_SECRET}")

    dt = DingTalk({"enabled": True, "webhook_url": ""})

    assert dt.webhook == ""
    assert dt.secret == ""
    assert "通知已禁用" in capsys.readouterr().out


# ---- 真实发送路径不变 (stub transport) ----

def test_resolved_url_passed_to_shared_notifier(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("DINGTALK_WEBHOOK", "https://env.example/hook")
    monkeypatch.setenv("DINGTALK_SECRET", "env-secret")
    recorded = {}

    class _Recorder:
        def __init__(self, url, secret, *, enabled=True, timeout=10,
                     quiet_errors=False):
            recorded.update(url=url, secret=secret, enabled=enabled, timeout=timeout,
                            quiet_errors=quiet_errors)

        def send(self, content):
            return True

    monkeypatch.setattr("src.notify.DingTalkNotifier", _Recorder)

    dt = DingTalk({"enabled": True, "webhook_url": ""})

    assert recorded == {"url": "https://env.example/hook", "secret": "env-secret",
                        "enabled": True, "timeout": 10, "quiet_errors": True}
    assert dt.send("hello") is True


def test_send_path_unchanged_with_stub_transport(monkeypatch):
    _clear_env(monkeypatch)
    dt = DingTalk({"enabled": True, "webhook_url": "https://config.example/hook"})
    sent = []
    dt._notifier.send = lambda content: sent.append(content) or True

    assert dt.send("hello") is True

    assert sent == ["hello"]
    assert dt.failure_count == 0


def test_failure_counting_unchanged(monkeypatch):
    _clear_env(monkeypatch)
    dt = DingTalk({"enabled": True, "webhook_url": "https://config.example/hook"})
    dt._notifier.send = lambda content: False

    assert dt.send("hello") is False

    assert dt.failure_count == 1
    assert dt.last_error


# ---- B1: 异常路径不得回显含 token 的 URL (quiet_errors) ----

class _BoomSession:
    def post(self, url, json=None, timeout=None):
        raise ConnectionError(f"connection failed url={url}")


def test_quiet_errors_enabled_on_shared_notifier(monkeypatch):
    _clear_env(monkeypatch)
    dt = DingTalk({"enabled": True, "webhook_url": "https://config.example/hook"})

    assert dt._notifier.quiet_errors is True


def test_send_exception_does_not_leak_token(monkeypatch, capsys):
    _clear_env(monkeypatch)
    token_url = ("https://oapi.dingtalk.com/robot/send"
                 "?access_token=SUPER_SECRET_TOKEN")
    dt = DingTalk({"enabled": True, "webhook_url": token_url})
    dt._notifier._session = _BoomSession()

    assert dt.send("x") is False

    out = capsys.readouterr().out
    assert "access_token" not in out
    assert "SUPER_SECRET_TOKEN" not in out
    assert dt.failure_count == 1
    assert dt.last_error
