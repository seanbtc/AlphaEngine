"""WP4 N3: .gitignore 覆盖运行期产物且不误伤已跟踪 data 文件."""
import fnmatch
import subprocess
import sys
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
_GITIGNORE = _ALPHA_ROOT / ".gitignore"

_REQUIRED_PATTERNS = ["data/.lock", "data/*.corrupt.*", "data/*.bak"]

_ARTIFACTS = [
    "data/.lock",
    "data/.lock.tmp.1234",
    "data/state.json.corrupt.20260923T082622Z",
    "data/state.json.corrupt.20260923T082622Z.1",
    "data/memory.md.bak",
    "data/knowledge_base.md.bak",
]


def _patterns():
    lines = _GITIGNORE.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def _matches(path, patterns):
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def _tracked_data_files():
    try:
        out = subprocess.run(["git", "ls-files", "data/"], cwd=_ALPHA_ROOT,
                             capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("git 不可用, 跳过已跟踪文件检查")
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


def test_gitignore_has_required_patterns():
    patterns = _patterns()
    for required in _REQUIRED_PATTERNS:
        assert required in patterns


def test_gitignore_covers_runtime_artifacts():
    patterns = _patterns()
    for artifact in _ARTIFACTS:
        assert _matches(artifact, patterns), artifact


def test_gitignore_does_not_match_tracked_data_files():
    patterns = _patterns()
    tracked = _tracked_data_files()
    assert tracked
    for path in tracked:
        assert not _matches(path, patterns), path
