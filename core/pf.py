"""平台差异都收在这里：Mac / Windows（Linux 尽量能跑）。

  文件锁        Mac fcntl.flock；Windows msvcrt.locking
  进程组        Mac 新会话 + killpg；Windows 新进程组 + taskkill /T
  密钥          Mac 钥匙串（security）；Windows DPAPI 加密后存 ~/.agentbus/secrets（只有当前用户能解开）
  打开东西      Mac open / osascript；Windows os.startfile / explorer / Windows Terminal；Linux xdg-open / gtk-launch / 常见终端
  开机启动      Mac LaunchAgent；Windows 注册表 HKCU\\...\\Run
  命令行工具    Windows 上 npm 装的是 .cmd，要先解析成完整路径；参数太长（cmd.exe 上限 8191）改成读文件
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
NAME = "windows" if IS_WIN else "mac" if IS_MAC else "linux"
HOME = Path(os.environ.get("AGENTBUS_HOME") or os.path.expanduser("~/.agentbus"))  # 和 config.HOME 同一个规则（pf 不能反过来 import config）
KEYSTORE = "Windows 凭据加密" if IS_WIN else "钥匙串" if IS_MAC else "本机文件"
FILE_MANAGER = "资源管理器" if IS_WIN else "访达" if IS_MAC else "文件管理器"
ARG_LIMIT = 7000 if IS_WIN else 200_000


# ---------- 文件锁 ----------
def try_lock(f):
    """非阻塞地锁住已打开的文件 f，成功返回 True。"""
    try:
        if IS_WIN:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def lock(f, wait=600):
    """阻塞地锁（最多等 wait 秒）。"""
    end = time.time() + wait
    while not try_lock(f):
        if time.time() > end:
            raise TimeoutError("等锁超时")
        time.sleep(0.2)


def unlock(f):
    try:
        if IS_WIN:
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_UN)
    except OSError:
        pass


# ---------- 进程 ----------
def pid_alive(pid):
    """检查进程是否仍在运行；Windows 的 os.kill(pid, 0) 也可能命中已退出但句柄未关闭的进程。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError, OverflowError):
        return False
    if pid <= 0:
        return False
    if IS_WIN:
        import ctypes
        from ctypes import wintypes

        if pid > 0xFFFFFFFF:
            return False
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE：只检查退出状态
        if not handle:
            return ctypes.get_last_error() == 5  # 拒绝访问时不能断定进程已退出
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def group_kwargs(hidden=True):
    """让子进程自成一组（看门狗要能整组杀掉），Windows 上顺便不弹黑框。"""
    if IS_WIN:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | (subprocess.CREATE_NO_WINDOW if hidden else 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def kill_tree(p):
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
        return
    import signal
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(p.pid, sig)
        except OSError:  # 已经退出了
            return
        time.sleep(3)


def kill_job(rec):
    """停一单派活：外层 agentbus ask（收到 SIGTERM 会自己停掉执行器、写回复）和实际执行器两组都发停止信号，
    5 秒后还活着的强杀。rec = running 记录（pid = 外层，worker = 执行器进程组）。"""
    import signal
    import threading
    f = rec.get("_file")
    if f:  # 先标“已取消”：外层刚起执行器、还没登记 worker 的那一瞬间，登记完一看到这个标记就自己停
        try:
            d = json.load(open(f, encoding="utf-8"))
            d["cancel"] = True
            json.dump(d, open(f, "w", encoding="utf-8"), ensure_ascii=False)
        except (OSError, ValueError):
            pass
    pids = [int(x) for x in (rec.get("pid"), rec.get("worker")) if x]
    if IS_WIN:
        for pid in pids:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return bool(pids)
    hit = False
    for pid in pids:
        try:
            os.killpg(pid, signal.SIGTERM)
            hit = True
        except OSError:
            pass

    def later():
        time.sleep(5)
        try:  # 这 5 秒里可能才登记上执行器：重读一遍，一起强杀
            w = json.load(open(f)).get("worker") if f else None
            if w and int(w) not in pids:
                pids.append(int(w))
        except (OSError, ValueError, TypeError):
            pass
        for pid in pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
    threading.Thread(target=later, daemon=True).start()
    return hit


def run_tree(argv, timeout, **kw):
    """像 subprocess.run(capture_output=True, text=True)，但超时时整组（连孙子进程）一起杀掉。"""
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, **kw, **group_kwargs())
    try:
        out, err = p.communicate(timeout=timeout)
    except BaseException:
        kill_tree(p)
        p.wait()
        raise
    return subprocess.CompletedProcess(argv, p.returncode, out, err)


def extra_paths():
    """招募时选了别的安装位置，命令行工具在这些目录里。"""
    import json
    try:
        d = json.loads((HOME / "config.json").read_text("utf-8")).get("settings", {}).get("install_dir")
    except Exception:
        d = None
    if not d:
        return []
    d = Path(os.path.expanduser(d))
    return [str(d), str(d / "bin")]


def which(cmd):
    path = os.pathsep.join([os.environ.get("PATH", "")] + extra_paths())
    return shutil.which(cmd, path=path)


def resolve(cmd):
    """命令列表的第一个换成完整路径（Windows 上 npm 装的 xxx.cmd 不解析就找不到）。"""
    if cmd and not os.path.isabs(cmd[0]):
        full = which(cmd[0])
        if full:
            return [full] + list(cmd[1:])
    return list(cmd)


def shell(cmd):
    """跑一条安装命令用的外壳。"""
    if IS_WIN:
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd]
    return ["/bin/zsh" if os.path.exists("/bin/zsh") else "/bin/sh", "-lc", cmd]


def cli_cmd():
    """调 agentbus 命令行自己：打包成 exe 后是 `agentbus-core cli ...`，源码运行是 `python agentbus.py ...`。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "cli"]
    return [sys.executable, str(Path(__file__).resolve().parent / "agentbus.py")]


def mcp_cmd():
    """给 Claude 注册 MCP 用的启动命令。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "mcp"]
    return [sys.executable, str(Path(__file__).resolve().parent / "mcp_server.py")]


# ---------- 密钥 ----------
KC_SERVICE = "agentbus-wallet"


def _dpapi(data, encrypt):
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    src, out = BLOB(len(data), buf), BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if encrypt else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI 失败")
    res = ctypes.string_at(out.pbData, out.cbData)
    ctypes.windll.kernel32.LocalFree(out.pbData)
    return res


def _secret_file(name):
    d = HOME / "secrets"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.bin"


def secret_set(name, value):
    if IS_MAC:
        r = subprocess.run(["security", "add-generic-password", "-U", "-s", KC_SERVICE, "-a", name, "-w", value], capture_output=True, text=True)
        if r.returncode:
            raise ValueError("存进钥匙串失败：" + r.stderr.strip()[:200])
        return
    f = _secret_file(name)
    raw = value.encode()
    f.write_bytes(_dpapi(raw, True) if IS_WIN else base64.b64encode(raw))
    if not IS_WIN:
        os.chmod(f, 0o600)


def secret_get(name):
    if IS_MAC:
        r = subprocess.run(["security", "find-generic-password", "-s", KC_SERVICE, "-a", name, "-w"], capture_output=True, text=True)
        return r.stdout.strip() or None
    f = _secret_file(name)
    if not f.exists():
        return None
    try:
        return (_dpapi(f.read_bytes(), False) if IS_WIN else base64.b64decode(f.read_bytes())).decode()
    except Exception:
        return None


def secret_delete(name):
    if IS_MAC:
        subprocess.run(["security", "delete-generic-password", "-s", KC_SERVICE, "-a", name], capture_output=True)
        return
    f = _secret_file(name)
    if f.exists():
        f.unlink()


# ---------- 打开东西 ----------
def _win_app(name):
    """Windows 上按常见安装位置找桌面端 exe。"""
    if not name:
        return None
    exe = name if name.lower().endswith(".exe") else name + ".exe"
    for base in (os.environ.get("LOCALAPPDATA", "") + r"\Programs", os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", ""),
                 os.environ.get("LOCALAPPDATA", "")):
        if not base:
            continue
        for p in (Path(base) / name / exe, Path(base) / name.replace(" ", "") / exe, Path(base) / name.lower() / exe):
            if p.exists():
                return str(p)
    return shutil.which(exe)


def app_exists(o):
    """o 是 catalog.OPENERS 里的一项。"""
    if IS_WIN:
        return bool(_win_app(o.get("win_app") or o.get("app")))
    if not IS_MAC:
        return bool(_linux_app(o.get("app")))
    return bool(o.get("app") and Path(f"/Applications/{o['app']}.app").exists())


def _linux_app(name):
    """Linux：按 .desktop 文件或命令名找桌面端，返回启动参数列表。"""
    if not name:
        return None
    low = name.lower().replace(" ", "-")
    for d in (Path.home() / ".local/share/applications", Path("/usr/share/applications"), Path("/var/lib/flatpak/exports/share/applications")):
        for f in d.glob("*.desktop") if d.exists() else []:
            if low in f.stem.lower():
                return ["gtk-launch", f.stem] if shutil.which("gtk-launch") else ["xdg-open", str(f)]
    exe = shutil.which(low) or shutil.which(name)
    return [exe] if exe else None


def open_app(o):
    if IS_WIN:
        p = _win_app(o.get("win_app") or o.get("app"))
        if not p:
            raise ValueError("本机没有它的桌面端")
        os.startfile(p)  # noqa: S606
    elif IS_MAC:
        subprocess.Popen(["open", "-a", o["app"]])
    else:
        cmd = _linux_app(o.get("app"))
        if not cmd:
            raise ValueError("本机没有它的桌面端")
        subprocess.Popen(cmd, start_new_session=True)


def open_url(url):
    if IS_MAC:
        subprocess.Popen(["open", url])
    else:
        webbrowser.open(url)


def open_terminal(cwd, line):
    """开一个终端窗口，进项目文件夹再跑 line。"""
    if IS_WIN:
        if shutil.which("wt"):
            subprocess.Popen(["wt", "-d", str(cwd), "cmd", "/k", line])
        else:
            subprocess.Popen(["cmd", "/c", "start", "cmd", "/k", f'cd /d "{cwd}" && {line}'])
        return
    import json
    import shlex
    if not IS_MAC:  # Linux：常见终端挨个试
        run = f"cd {shlex.quote(str(cwd))} && {line}; exec $SHELL"
        for term, args in (("x-terminal-emulator", ["-e", "bash", "-lc", run]), ("gnome-terminal", ["--", "bash", "-lc", run]),
                           ("konsole", ["-e", "bash", "-lc", run]), ("xfce4-terminal", ["-e", f"bash -lc {shlex.quote(run)}"]), ("xterm", ["-e", "bash", "-lc", run])):
            if shutil.which(term):
                subprocess.Popen([term, *args], start_new_session=True)
                return
        raise ValueError("没找到终端程序（x-terminal-emulator / gnome-terminal / konsole）")
    script = f'tell application "Terminal"\n activate\n do script {json.dumps(f"cd {shlex.quote(str(cwd))} && {line}")}\nend tell'
    subprocess.Popen(["osascript", "-e", script])


def reveal(path):
    """在访达 / 资源管理器里选中一个文件。"""
    if IS_WIN:
        subprocess.Popen(["explorer", "/select,", str(path)])
    elif IS_MAC:
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent)])


def open_agentbus():
    """打开 agentbus 应用；没装应用就开网页。返回说明文字。"""
    url = "http://127.0.0.1:" + (os.environ.get("AGENTBUS_PORT") or "8790")
    if IS_MAC:
        if subprocess.run(["open", "-a", "agentbus"], capture_output=True).returncode == 0:
            return "已打开 agentbus"
    elif IS_WIN:
        exe = os.environ.get("AGENTBUS_APP_EXE") or _win_app("agentbus")
        if exe and os.path.exists(exe):
            os.startfile(exe)  # noqa: S606
            return "已打开 agentbus"
    else:
        exe = os.environ.get("AGENTBUS_APP_EXE") or shutil.which("agentbus-app") or (_linux_app("agentbus") or [None])[0]
        if exe and os.path.exists(exe):
            subprocess.Popen([exe], start_new_session=True)
            return "已打开 agentbus"
    open_url(url)
    return f"没找到 agentbus 应用，已在浏览器打开 {url}"


# ---------- 开机启动 ----------
LOGIN_PLIST = Path.home() / "Library/LaunchAgents/app.agentbus.login.plist"


def set_login(on):
    if IS_WIN:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        try:
            if on:
                exe = os.environ.get("AGENTBUS_APP_EXE") or _win_app("agentbus")
                if not exe:
                    raise ValueError("没找到 agentbus.exe，先从应用里打开一次")
                winreg.SetValueEx(k, "agentbus", 0, winreg.REG_SZ, f'"{exe}"')
            else:
                try:
                    winreg.DeleteValue(k, "agentbus")
                except FileNotFoundError:
                    pass
        finally:
            winreg.CloseKey(k)
        return
    if not IS_MAC:  # Linux：~/.config/autostart 里放一个 .desktop
        f = Path.home() / ".config/autostart/agentbus.desktop"
        if not on:
            f.unlink(missing_ok=True)
            return
        exe = os.environ.get("AGENTBUS_APP_EXE") or shutil.which("agentbus-app")
        if not exe:
            raise ValueError("没找到 agentbus 应用，先从应用里打开一次")
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"[Desktop Entry]\nType=Application\nName=agentbus\nExec={exe}\nX-GNOME-Autostart-enabled=true\n", "utf-8")
        return
    if on:
        LOGIN_PLIST.parent.mkdir(parents=True, exist_ok=True)
        LOGIN_PLIST.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>app.agentbus.login</string>
<key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-a</string><string>agentbus</string></array>
<key>RunAtLoad</key><true/>
</dict></plist>
""")
    elif LOGIN_PLIST.exists():
        LOGIN_PLIST.unlink()


def peer_uid(peer_port, server_port):
    """本机 TCP 连接对面是哪个系统用户（按对方的本地端口找）。找不到返回 None，查不了（Windows / 没有 lsof）返回 -1。
    用来挡住“同一台电脑上别的用户”直接调 agentbus 的接口：同一个用户的程序本来就能读主人的配置，挡不挡没区别。"""
    if IS_WIN:
        return -1
    if sys.platform.startswith("linux"):
        want = f"{peer_port:04X}"
        for f in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                for line in open(f).read().splitlines()[1:]:
                    p = line.split()
                    if p[1].rsplit(":", 1)[1] == want and p[2].rsplit(":", 1)[1] == f"{server_port:04X}":
                        return int(p[7])
            except (OSError, IndexError, ValueError):
                continue
        return None
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{peer_port}", "-sTCP:ESTABLISHED", "-Fun"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return -1
    uid = None
    for line in out.splitlines():  # p<pid> / u<uid> / f<fd> / n<本端->对端>：只认本端是 peer_port、对端是服务端口的那一条
        if line.startswith("u"):
            uid = int(line[1:])
        elif line.startswith("n") and line[1:].split("->")[0].endswith(f":{peer_port}") and line.endswith(f":{server_port}"):
            return uid
    return None
