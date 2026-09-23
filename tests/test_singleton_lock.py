"""WP4 ②: 单例锁 — 拒绝并行/陈旧接管/退出清理/跨平台 PID 探测."""
import json
import os
import signal as signal_mod
import subprocess
import sys
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.singleton_lock import SingletonLock, pid_alive  # noqa: E402


def _write_lock(data_dir: Path, pid, mode: str = "daemon"):
    (data_dir / ".lock").write_text(
        json.dumps({"pid": pid, "started_at": "2026-01-01T00:00:00Z", "mode": mode}),
        encoding="utf-8")


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_pid_alive_self():
    assert pid_alive(os.getpid()) is True


def test_pid_alive_dead_process():
    assert pid_alive(_dead_pid()) is False


def test_pid_alive_invalid_values():
    assert pid_alive(None) is False
    assert pid_alive("abc") is False
    assert pid_alive(0) is False
    assert pid_alive(-1) is False


def test_lock_records_pid_started_mode(tmp_path):
    lock = SingletonLock(str(tmp_path))
    ok, holder = lock.acquire("backfill")

    assert ok is True and holder is None
    data = json.loads((tmp_path / ".lock").read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert data["mode"] == "backfill"
    assert data["started_at"].endswith("Z")
    lock.release()


def test_second_instance_same_process_rejected(tmp_path):
    holder_lock = SingletonLock(str(tmp_path))
    assert holder_lock.acquire("daemon")[0] is True
    try:
        second = SingletonLock(str(tmp_path))
        ok, holder = second.acquire("once")
        assert ok is False
        assert holder["pid"] == os.getpid()
        assert holder["mode"] == "daemon"
        # 被拒实例 release 不得删除持有者锁
        second.release()
        assert (tmp_path / ".lock").exists()
        assert json.loads((tmp_path / ".lock").read_text(encoding="utf-8"))["pid"] == os.getpid()
    finally:
        holder_lock.release()


def test_alive_other_process_rejected(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_lock(tmp_path, proc.pid, mode="daemon")
        lock = SingletonLock(str(tmp_path))
        ok, holder = lock.acquire("once")
        assert ok is False
        assert holder["pid"] == proc.pid
    finally:
        proc.kill()
        proc.wait()


def test_stale_lock_taken_over(tmp_path):
    _write_lock(tmp_path, _dead_pid(), mode="daemon")
    lock = SingletonLock(str(tmp_path))

    ok, stale = lock.acquire("once")

    assert ok is True
    assert stale is not None and stale["pid"] != os.getpid()
    data = json.loads((tmp_path / ".lock").read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert data["mode"] == "once"
    assert list(tmp_path.glob(".lock.tmp.*")) == []
    lock.release()
    assert not (tmp_path / ".lock").exists()


def test_takeover_rechecks_live_holder(tmp_path, monkeypatch):
    """N1: 首次读到陈旧锁后、接管前持有者已换为存活进程 → 复查拒绝, 不覆盖."""
    live = {"pid": os.getpid(), "started_at": "2026-01-01T00:00:00Z", "mode": "daemon"}
    stale = {"pid": _dead_pid(), "started_at": "2026-01-01T00:00:00Z", "mode": "daemon"}
    (tmp_path / ".lock").write_text(json.dumps(live), encoding="utf-8")
    lock = SingletonLock(str(tmp_path))
    calls = {"n": 0}

    def _fake_read():
        calls["n"] += 1
        return stale if calls["n"] == 1 else live

    monkeypatch.setattr(lock, "read_holder", _fake_read)

    ok, holder = lock.acquire("once")

    assert ok is False
    assert holder["pid"] == os.getpid()
    assert calls["n"] == 2
    assert lock._held is False
    persisted = json.loads((tmp_path / ".lock").read_text(encoding="utf-8"))
    assert persisted == live
    assert list(tmp_path.glob(".lock.tmp.*")) == []


def test_takeover_write_failure_cleans_tmp(tmp_path, monkeypatch):
    """N9: 接管写盘中断 → 原锁保留、无 tmp 残留、未标记持有."""
    _write_lock(tmp_path, _dead_pid(), mode="daemon")
    original = (tmp_path / ".lock").read_text(encoding="utf-8")
    lock = SingletonLock(str(tmp_path))

    def _boom(src, dst):
        raise OSError("simulated interruption")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        lock.acquire("daemon")

    assert (tmp_path / ".lock").read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(".lock.tmp.*")) == []
    assert lock._held is False


def test_corrupt_lock_treated_as_stale(tmp_path):
    (tmp_path / ".lock").write_text("not-json", encoding="utf-8")
    lock = SingletonLock(str(tmp_path))

    ok, stale = lock.acquire("daemon")

    assert ok is True and stale is None
    lock.release()
    assert not (tmp_path / ".lock").exists()


def test_release_idempotent_and_creates_dir(tmp_path):
    data_dir = tmp_path / "nested" / "data"
    lock = SingletonLock(str(data_dir))
    assert lock.acquire("daemon")[0] is True
    assert (data_dir / ".lock").exists()
    lock.release()
    lock.release()
    assert not (data_dir / ".lock").exists()


def test_cli_lock_mode_mapping():
    from src.alpha import _lock_mode_for_args

    def mode(**overrides):
        args = {"once": False, "backfill": False, "bulk_only": None,
                "import_file": None, "test_ai": False, "status_only": False}
        args.update(overrides)
        return _lock_mode_for_args(**args)

    assert mode() == "daemon"
    assert mode(once=True) == "once"
    assert mode(backfill=True) == "backfill"
    assert mode(bulk_only=100) == "bulk"
    assert mode(import_file="x.jsonl") == "import"
    assert mode(test_ai=True) == ""
    assert mode(status_only=True) == ""
    assert mode(once=True, backfill=True) == "backfill"


def test_install_lock_cleanup_registers_atexit_and_sigterm(monkeypatch, tmp_path):
    from src import alpha as alpha_mod

    lock = SingletonLock(str(tmp_path))
    assert lock.acquire("daemon")[0] is True
    handlers = {}
    monkeypatch.setattr(alpha_mod.atexit, "register",
                        lambda fn: handlers.__setitem__("atexit", fn))
    monkeypatch.setattr(alpha_mod.signal, "signal",
                        lambda sig, fn: handlers.__setitem__(sig, fn))

    alpha_mod._install_lock_cleanup(lock)

    assert "atexit" in handlers
    assert signal_mod.SIGTERM in handlers
    with pytest.raises(SystemExit):
        handlers[signal_mod.SIGTERM](signal_mod.SIGTERM, None)
    assert not (tmp_path / ".lock").exists()
