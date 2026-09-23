"""单例锁 — 防止常驻实例与 --once/--backfill 等写盘入口并行.

锁文件为 data/.lock (JSON: pid/started_at/mode):
- 已存在且 PID 存活 → acquire 返回 False (调用方拒绝启动);
- 已存在但 PID 已死/锁文件损坏 → 视为陈旧锁, 接管重写;
- release 仅在锁仍属于本进程时删除; atexit/信号清理由调用方注册。

平台差异: POSIX 用 os.kill(pid, 0) 探测 (EPERM=存活); Windows 上
os.kill(pid, 0) 会走 TerminateProcess 强杀目标进程, 故改用
OpenProcess + GetExitCodeProcess(STILL_ACTIVE) 探测。
"""
import json
import os
from datetime import datetime


def pid_alive(pid) -> bool:
    """探测 PID 是否存活 (跨平台). 非法/不存在的 PID 返回 False."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _pid_alive_windows(pid: int) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:  # pragma: no cover - Windows 必有 ctypes
        return True
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p,
                                            ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == STILL_ACTIVE
        return True
    finally:
        kernel32.CloseHandle(handle)


class SingletonLock:
    def __init__(self, data_dir: str, lock_file: str = ".lock"):
        self.data_dir = data_dir
        self.lock_file = (os.path.join(data_dir, lock_file)
                          if not os.path.isabs(lock_file) else lock_file)
        self._held = False

    def acquire(self, mode: str = "daemon"):
        """尝试获取锁. 返回 (是否获取, 现有持有者信息或 None).

        持有者存活 → (False, holder); 无锁/陈旧锁/损坏锁 → 重写并 (True, 旧 holder);
        接管前复查持有者 (TOCTOU): 若期间已被存活进程接管, 返回 (False, 该持有者)。
        """
        os.makedirs(self.data_dir, exist_ok=True)
        holder = self.read_holder()
        if holder is not None and pid_alive(holder.get("pid")):
            return False, holder
        info = {
            "pid": os.getpid(),
            "started_at": datetime.utcnow().isoformat() + "Z",
            "mode": str(mode or ""),
        }
        try:
            fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # 复查 (TOCTOU): 首次读取后可能已有其它进程完成接管, 不得覆盖存活持有者
            holder = self.read_holder()
            if holder is not None and pid_alive(holder.get("pid")):
                return False, holder
            # 仍为陈旧/损坏锁 → 接管重写
            self._write(info)
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)
        self._held = True
        return True, holder

    def read_holder(self):
        """读取当前锁持有者; 文件不存在/损坏/非对象返回 None."""
        if not os.path.exists(self.lock_file):
            return None
        try:
            with open(self.lock_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def release(self) -> None:
        """释放锁 (幂等); 仅在锁仍属于本进程时删除, 避免误删继任者锁."""
        if not self._held:
            return
        self._held = False
        holder = self.read_holder()
        if holder is not None:
            try:
                holder_pid = int(holder.get("pid", -1))
            except (TypeError, ValueError):
                holder_pid = -1
            if holder_pid != os.getpid():
                return
        try:
            os.remove(self.lock_file)
        except OSError:
            pass

    def _write(self, info: dict) -> None:
        # tmp 名带 pid: 多进程并发接管时互不覆盖 (N9), 失败清理临时文件
        tmp = f"{self.lock_file}.tmp.{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.lock_file)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
