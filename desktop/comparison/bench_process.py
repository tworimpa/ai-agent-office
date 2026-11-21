"""Count scoped process families and close actual desktop windows through their window manager."""

import ctypes
import os
from pathlib import Path
import time

import psutil


class Family:
    def __init__(self, pid):
        self.root = psutil.Process(pid)
        self.seen = {self.root}

    def processes(self):
        live = set()
        for process in (self.root, *(self.seen - {self.root})):
            if process in live:
                continue
            try:
                if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                    live.add(process)
                    live.update(process.children(recursive=True))
            except psutil.NoSuchProcess:
                continue
        self.seen.update(live)
        return live

    def snapshot(self):
        result = []
        for process in self.processes():
            try:
                with process.oneshot():
                    if process.status() == psutil.STATUS_ZOMBIE:
                        continue
                    cpu = process.cpu_times()
                    row = dict(pid=process.pid, parent=process.ppid(), name=process.name(),
                               birth=process.create_time(), cpu=cpu.user + cpu.system)
                    try:
                        if os.name == "nt":
                            row["bytes"] = process.memory_info().private
                        else:
                            lines = Path(f"/proc/{process.pid}/smaps_rollup").read_text().splitlines()
                            row["bytes"] = next(int(s.split()[1]) * 1024 for s in lines if s.startswith("Pss:"))
                    except (PermissionError, psutil.AccessDenied):
                        # Keep the missing PID and its RSS visible. Never silently substitute RSS for PSS.
                        row["bytes"] = None
                        row["rss"] = process.memory_info().rss
                    result.append(row)
            except (FileNotFoundError, ProcessLookupError, psutil.NoSuchProcess):
                continue
        return sorted(result, key=lambda item: item["pid"])


def native_window(pid, adjustment=None):
    if os.name == "nt":
        from ctypes import wintypes
        user = ctypes.windll.user32
        handles = []
        callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback
        def collect(handle, unused):
            owner = wintypes.DWORD()
            user.GetWindowThreadProcessId(wintypes.HWND(handle), ctypes.byref(owner))
            if owner.value == pid and user.IsWindowVisible(wintypes.HWND(handle)):
                # WebView2 also creates a visible, zero-area helper HWND.
                bounds = wintypes.RECT()
                if user.GetClientRect(wintypes.HWND(handle), ctypes.byref(bounds)):
                    if bounds.right >= 300 and bounds.bottom >= 200 and user.GetWindowTextLengthW(wintypes.HWND(handle)):
                        handles.append(handle)
            return True

        user.EnumWindows(collect, 0)
        if len(handles) != 1:
            raise RuntimeError(f"Expected one visible application window for {pid}, got {len(handles)}")
        handle = wintypes.HWND(handles[0])
        if adjustment is not None:
            # Read and set physical dimensions; the browser reported CSS pixels and DPR.
            user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            previous = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            if not previous:
                raise ctypes.WinError()
            try:
                rect = wintypes.RECT()
                if not user.GetWindowRect(handle, ctypes.byref(rect)):
                    raise ctypes.WinError()
                width = rect.right - rect.left + adjustment[0]
                height = rect.bottom - rect.top + adjustment[1]
                if not user.SetWindowPos(handle, 0, 0, 0, width, height, 0x0016):
                    raise ctypes.WinError()
            finally:
                user.SetThreadDpiAwarenessContext(ctypes.c_void_p(previous))
        elif not user.PostMessageW(handle, 0x0010, 0, 0):  # WM_CLOSE
            raise ctypes.WinError()
        return

    from Xlib import X, display, error, protocol
    connection = display.Display()
    try:
        pid_atom = connection.intern_atom("_NET_WM_PID")
        protocols = connection.intern_atom("WM_PROTOCOLS")
        delete = connection.intern_atom("WM_DELETE_WINDOW")
        windows = [connection.screen().root]
        matched = []
        while windows:
            window = windows.pop()
            try:
                prop = window.get_full_property(pid_atom, X.AnyPropertyType)
                if prop and list(prop.value) == [pid] and delete in (window.get_wm_protocols() or []):
                    if window.get_attributes().map_state == X.IsViewable:
                        matched.append(window)
                else:
                    windows.extend(window.query_tree().children)
            except error.BadWindow:
                continue
        if len(matched) != 1:
            raise RuntimeError(f"Expected one mapped X11 window for {pid}, got {len(matched)}")
        window = matched[0]
        if adjustment is None:
            window.send_event(protocol.event.ClientMessage(
                window=window, client_type=protocols, data=(32, [delete, X.CurrentTime, 0, 0, 0])))
        else:
            geometry = window.get_geometry()
            window.configure(width=geometry.width + adjustment[0], height=geometry.height + adjustment[1])
        connection.sync()
    finally:
        connection.close()


def snapshot(shell, studio, setup):
    backend = studio.snapshot()
    workers = {setup.get("worker")}
    providers = {setup.get("cli")}
    tests = set()
    # Partition by ancestry; a Windows Python venv may also have a launcher.
    parents = {p["pid"]: p["parent"] for p in backend}
    if setup.get("cli"):
        # Windows invokes the fixture through cmd.exe and a venv redirector.
        # Include those two launchers in the CLI family, not the supervisor.
        pid = parents.get(setup["cli"])
        if os.name == "nt" and pid in parents:
            candidate = psutil.Process(pid)
            if any(Path(arg).name == "goal_worker.py" for arg in candidate.cmdline()):
                providers.add(pid)
                pid = parents.get(pid)
            if pid in parents and psutil.Process(pid).name().lower() == "cmd.exe":
                providers.add(pid)
    for row in backend:
        if not row["name"].lower().startswith(("pytest", "python")):
            continue
        try:
            argv = psutil.Process(row["pid"]).cmdline()
        except psutil.NoSuchProcess:
            continue
        if row["name"].lower().startswith("pytest") or any(
                flag == "-m" and module in {"pytest", "agent_office.desktop_pytest"}
                for flag, module in zip(argv, argv[1:])):
            tests.add(row["pid"])

    def group(row):
        pid = row["pid"]
        visited = set()
        while pid in parents and pid not in visited:
            if pid in tests:
                return "tests"
            if pid in providers:
                return "cli"
            if pid in workers:
                return "worker"
            visited.add(pid)
            pid = parents[pid]
        return "studio"

    groups = {"shell": shell.snapshot(), "studio": [], "worker": [], "cli": [], "tests": []}
    for row in backend:
        groups[group(row)].append(row)
    return {"observed": time.perf_counter(), "processes": groups}
