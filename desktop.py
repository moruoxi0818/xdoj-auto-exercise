# -*- coding: utf-8 -*-
"""桌面版入口：内置本地服务 + 应用窗口

开发运行：  python desktop.py
打包运行：  PyInstaller --onefile --windowed desktop.py

窗口用 Edge/Chrome 的 --app 模式，外观与原生软件一致（无地址栏、无标签页），
不依赖 WebView2 运行时，比内嵌浏览器稳。
"""

import os
import socket
import subprocess
import sys
import threading
import time


def _appdata():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "XDOJAuto")
    os.makedirs(os.path.join(d, "data"), exist_ok=True)
    return d


def _log_path():
    return os.path.join(_appdata(), "data", "app.log")


def log(msg):
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(time.strftime("[%m-%d %H:%M:%S] ") + str(msg) + "\n")
    except Exception:
        pass
    try:
        print(msg)
    except Exception:
        pass


def _redirect_std_streams():
    """windowed 模式下 stdout/stderr 为 None，print 会崩，接管到日志文件"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        f = open(_log_path(), "a", encoding="utf-8", buffering=1)
    except Exception:
        return
    if sys.stdout is None:
        sys.stdout = f
    if sys.stderr is None:
        sys.stderr = f


_redirect_std_streams()


# ---------------- 单实例 ----------------

def acquire_lock():
    lock = os.path.join(_appdata(), ".lock")
    try:
        if os.path.exists(lock):
            with open(lock, "r", encoding="utf-8") as f:
                old = int((f.read() or "0").strip() or 0)
            if old and _alive(old):
                return False, old
    except Exception:
        pass
    try:
        with open(lock, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception:
        pass
    return True, os.getpid()


def _alive(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=8)
        return str(pid) in (out.stdout or "")
    except Exception:
        return False


def release_lock():
    try:
        os.remove(os.path.join(_appdata(), ".lock"))
    except Exception:
        pass


# ---------------- 浏览器 ----------------

def find_browser():
    import winreg
    names = ["msedge.exe", "chrome.exe"]
    roots = [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]
    for name in names:
        for root in roots:
            for view in (winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0),
                         winreg.KEY_READ | getattr(winreg, "KEY_WOW64_32KEY", 0)):
                try:
                    key = winreg.OpenKey(
                        root,
                        rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{name}",
                        0, view)
                    path, _ = winreg.QueryValueEx(key, "")
                    winreg.CloseKey(key)
                    path = path.strip('"')
                    if os.path.exists(path):
                        return path
                except Exception:
                    continue
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"):
        if os.path.exists(p):
            return p
    return None


def kill_old_windows(profile):
    """关掉本程序之前留下的浏览器窗口，避免新窗口被复用/遮挡"""
    tag = os.path.basename(os.path.dirname(profile))  # XDOJAuto
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{tag}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                        "-WindowStyle", "Hidden", "-Command", ps],
                       capture_output=True, timeout=25,
                       creationflags=0x08000000)
        log("已清理旧窗口")
        time.sleep(1.2)
    except Exception as e:
        log("清理旧窗口失败（忽略）：%r" % (e,))


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def wait_ready(port, timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.4):
                return True
        except OSError:
            time.sleep(0.15)
    return False


# ---------------- 主流程 ----------------

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve-only", action="store_true", help="只起服务，不开窗口（调试用）")
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()

    ok, other = acquire_lock()
    if not ok:
        log(f"已有实例在运行（PID {other}），本次退出")
        _warn("XDOJ 自动刷题已经在运行，请在任务栏里找一下。")
        return 0

    import paths
    import uvicorn

    import server

    paths.ensure_env()
    log("=== 启动 === frozen=%s appdir=%s" % (paths.FROZEN, paths.APP_DIR))

    port = args.port or free_port()
    url = f"http://127.0.0.1:{port}"

    cfg = uvicorn.Config(server.app, host="127.0.0.1", port=port,
                         log_level="warning", access_log=False)
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()

    if not wait_ready(port):
        log("本地服务启动失败")
        _warn("本地服务启动失败，详见日志：" + _log_path())
        return 1
    log("服务就绪 " + url)

    if args.serve_only:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0

    browser = find_browser()
    if not browser:
        log("没找到 Edge/Chrome，改用默认浏览器")
        import webbrowser
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0

    profile = os.path.join(_appdata(), "browser")
    edge = "msedge.exe" in os.path.basename(browser).lower()
    cmd = [
        browser,
        f"--app={url}",
        "--window-size=1240,900",
        "--window-position=100,40",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-extensions",
        "--lang=zh-CN",
    ]
    if edge:
        cmd.append("--disable-features=msSyncPromo,msEdgeIdentitySync,msEdgeWelcomePage")
    log("启动窗口：" + " ".join(cmd[:3]))
    kill_old_windows(profile)
    server.LAST_SEEN["t"] = time.time()
    try:
        subprocess.Popen(cmd, close_fds=True)
    except Exception as e:
        log("启动窗口失败：%r" % (e,))
        import webbrowser
        webbrowser.open(url)

    # 浏览器启动器会立刻退出、把窗口交给主进程，所以改用页面心跳判断窗口是否还开着
    GRACE = 180      # 首次加载宽限
    TIMEOUT = 180    # 无心跳多久判定窗口已关闭
    t0 = time.time()
    try:
        while True:
            time.sleep(5)
            idle = time.time() - server.LAST_SEEN["t"]
            age = time.time() - t0
            if age > 3600 * 8:
                log("运行超过 8 小时，自动退出")
                break
            # 正在刷题时不做心跳判定：页面最小化、系统休眠都会让心跳停掉，
            # 这时候杀服务等于把跑到一半的批量任务腰斩。
            if getattr(server.BUS, "running", False):
                continue
            if age > GRACE and idle > TIMEOUT:
                log(f"页面已 {idle:.0f}s 无心跳，关闭服务")
                break
    except KeyboardInterrupt:
        pass
    finally:
        srv.should_exit = True
        release_lock()
    return 0


def _warn(msg):
    """无控制台时用消息框提示"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, "XDOJ 自动刷题", 0x40)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        import traceback
        log("崩溃：\n" + traceback.format_exc())
        _warn("程序异常退出，日志：" + _log_path())
        raise
