"""WP4 ④: 通知失败可观测 — 计数/状态行/主流程继续."""
import sys
from pathlib import Path

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.alpha import print_status  # noqa: E402
from src.notify import DingTalk  # noqa: E402
from src.state_manager import StateManager  # noqa: E402


class _StubNotifier:
    def __init__(self, result=False, exc=None):
        self.result = result
        self.exc = exc
        self.calls = 0

    def send(self, content):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


def _dingtalk(result=False, exc=None):
    dt = DingTalk({"enabled": True, "webhook_url": "http://example.invalid"})
    dt._notifier = _StubNotifier(result=result, exc=exc)
    return dt


def test_send_false_counts_failure():
    dt = _dingtalk(result=False)

    assert dt.send("x") is False

    assert dt.failure_count == 1
    assert dt.last_error
    assert dt.last_failure_at.endswith("Z")


def test_send_exception_counted_and_swallowed():
    dt = _dingtalk(exc=RuntimeError("network down"))

    assert dt.send("x") is False

    assert dt.failure_count == 1
    assert "network down" in dt.last_error


def test_send_success_does_not_count():
    dt = _dingtalk(result=True)

    assert dt.send("x") is True

    assert dt.failure_count == 0
    assert dt.last_error == ""
    assert dt.last_failure_at == ""


def test_failure_count_accumulates():
    dt = _dingtalk(result=False)

    dt.send("a")
    dt.send("b")
    dt.send("c")

    assert dt.failure_count == 3


def test_disabled_not_counted():
    dt = DingTalk({"enabled": False, "webhook_url": "http://example.invalid"})

    assert dt.send("x") is False

    assert dt.failure_count == 0
    assert dt.last_error == ""
    assert dt.last_failure_at == ""


def test_empty_webhook_not_counted():
    dt = DingTalk({"enabled": True, "webhook_url": ""})

    assert dt.send("x") is False

    assert dt.failure_count == 0


def test_configured_real_failure_still_counted():
    dt = _dingtalk(result=False)
    assert dt._configured() is True

    assert dt.send("x") is False

    assert dt.failure_count == 1


def test_unconfigured_status_line_silent(tmp_path, capsys):
    sm = StateManager(str(tmp_path), "state.json")
    dt = DingTalk({"enabled": False, "webhook_url": "http://example.invalid"})
    dt.send("x")

    print_status({"state": sm, "dingtalk": dt})

    assert "Notify失败" not in capsys.readouterr().out


def test_templates_share_failure_counter():
    dt = _dingtalk(result=False)

    dt.regime_change("BEAR", "BEAR_DEEP", "reason", -0.3, -0.3)
    dt.alpha_change(-0.3, -0.5, "BEAR_DEEP", 60000, -0.5)
    dt.analysis("summary", "BEAR_DEEP", "high", -0.5, [])
    dt.alert("title", "body")

    assert dt.failure_count == 4


def test_print_status_shows_notify_failures(tmp_path, capsys):
    sm = StateManager(str(tmp_path), "state.json")
    dt = _dingtalk(result=False)
    dt.send("x")

    print_status({"state": sm, "dingtalk": dt})

    out = capsys.readouterr().out
    assert "Notify失败=1次" in out
    assert dt.last_failure_at in out


def test_print_status_silent_without_failures(tmp_path, capsys):
    sm = StateManager(str(tmp_path), "state.json")
    dt = _dingtalk(result=True)

    print_status({"state": sm, "dingtalk": dt})

    assert "Notify失败" not in capsys.readouterr().out


def test_print_status_without_dingtalk(tmp_path, capsys):
    sm = StateManager(str(tmp_path), "state.json")

    print_status({"state": sm})

    assert "Notify失败" not in capsys.readouterr().out
