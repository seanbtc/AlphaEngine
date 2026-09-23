"""WP7 ⑤: 打印 ASCII 化 — print 调用不得包含 GBK 不安全字符 (控制台兼容)."""
import ast
import sys
from pathlib import Path

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))


def _first_non_gbk(text):
    for char in text:
        try:
            char.encode("gbk")
        except UnicodeEncodeError:
            return char
    return None


def _print_calls(source: str):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "print"):
            yield node


def test_print_calls_are_gbk_safe():
    offenders = []
    for path in sorted((_ALPHA_ROOT / "src").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        for node in _print_calls(source):
            segment = ast.get_source_segment(source, node) or ""
            bad = _first_non_gbk(segment)
            if bad:
                offenders.append(f"{path.name}:{node.lineno}: {bad!r}")
    assert offenders == []


def test_known_print_markers_are_ascii():
    """WP7 明确的残余点位: fetcher ✓/✗ 与 alpha 漂移告警 ⚠ 已替换."""
    fetcher = (_ALPHA_ROOT / "src" / "fetcher.py").read_text(encoding="utf-8")
    alpha = (_ALPHA_ROOT / "src" / "alpha.py").read_text(encoding="utf-8")

    assert "✓" not in fetcher and "✗" not in fetcher
    assert "[OK] 解析到" in fetcher and "[!!] 无法读取" in fetcher
    assert "[WARN] {a}" in alpha
