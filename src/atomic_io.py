"""原子写工具 — 先写临时文件再 os.replace 覆盖, 避免中断产生半文件."""
import json
import os
import shutil
from typing import Any


def atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    """原子写入文本: 先写 <path>.tmp, 再 os.replace 覆盖目标; 失败清理临时文件."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding=encoding) as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        _remove_quiet(tmp)
        raise


def atomic_write_json(path: str, data: Any, *, ensure_ascii: bool = False,
                      indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=ensure_ascii, indent=indent))


def atomic_copy(src: str, dst: str) -> None:
    """原子复制: 先复制到 <dst>.tmp, 再 os.replace; 失败清理临时文件."""
    directory = os.path.dirname(os.path.abspath(dst))
    os.makedirs(directory, exist_ok=True)
    tmp = dst + ".tmp"
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        _remove_quiet(tmp)
        raise


def _remove_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
