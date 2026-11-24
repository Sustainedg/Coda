"""Google Antigravity CLI 的双向登录助手（OAuth 授权码模式）。

Antigravity CLI 没有 ``login`` 子命令；但在**终端**里设置
``ANTIGRAVITY_REMOTE_OOB_AUTH=1`` 后，打印模式会走「OOB 认证」：

* 打印 ``Authentication required. Please visit the URL to log in:`` 与授权链接；
* 提示 ``Or, paste the authorization code here and press Enter:``；
* 从 stdin 读一行授权码，换 token 后把登录态写进自己的数据目录。

注意：这套 OOB 流程要求 stdin/stdout 是 **TTY**（管道会退回「请先运行 agy 登录」），
所以本脚本用伪终端拉起内核。随后把这条链路包成平台的通用登录协议：

1. 用伪终端 + ``ANTIGRAVITY_REMOTE_OOB_AUTH=1`` 拉起 ``agy -p``；
2. 抓到授权链接后渲染二维码、打印 ``QR_IMAGE=<路径>``；
3. 打印 ``INPUT_PROMPT=<提示>`` 声明「需要用户把授权码贴回来」；
4. 从 **stdin** 读入界面提交的授权码，写回伪终端；
5. 轮询数据目录里的凭据落盘，成功退出 0，失败 / 超时退出 1。

授权窗口约 60 秒（内核里硬编码），所以界面要提示用户先把浏览器打开好。

框架完全不认识 Antigravity：``QR_IMAGE=`` / ``INPUT_PROMPT=`` 是平台通用协议。

注意：本脚本以 ``login_argv`` 方式执行时没有包上下文（``python <路径>``），
因此只依赖标准库与可选的 ``segno``，不要 import 本包的其它模块。
"""
from __future__ import annotations

import json
import os
import queue
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

#: 内核自己的授权窗口约 60 秒；这里留一点余量，超时后给出可读提示。
_AUTH_TIMEOUT_SECONDS = 75.0
#: 等授权链接打印出来的上限（冷启动要加载配置）。
_URL_WAIT_SECONDS = 60.0
#: 提交授权码后等凭据落盘的上限。
_SUCCESS_WAIT_SECONDS = 60.0
#: 轮询伪终端 / 凭据的间隔。
_POLL_INTERVAL_SECONDS = 0.5

#: 授权链接（Antigravity 用 Google 的 OAuth 授权端点）。
_AUTH_URL = re.compile(r"https://accounts\.google\.com/o/oauth2/\S+")

#: 需要用户输入的提示行。
_CODE_HINT = "paste the authorization code here"

#: 终端控制序列：伪终端里会有光标 / 颜色控制码，直接回显会污染任务日志。
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]")

#: 凭据扫描：与适配器一致，不猜文件名，按内容找 token 层。
_SKIP_DIRS = {"log", "crashes", "conversations", "brain", "cache", "updater", "builtin"}
_MAX_CRED_BYTES = 512 * 1024


def app_data_dir() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli"


def _find_token(value: Any, depth: int = 0) -> dict[str, Any] | None:
    if depth > 4:
        return None
    if isinstance(value, dict):
        if value.get("access_token") or value.get("refresh_token"):
            return value
        for item in value.values():
            found = _find_token(item, depth + 1)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_token(item, depth + 1)
            if found is not None:
                return found
    return None


def credentials_present() -> bool:
    """数据目录里是否已有可用凭据（扫描小 JSON，不依赖文件名）。"""
    root = app_data_dir()
    if not root.is_dir():
        return False
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if not name.endswith(".json"):
                continue
            path = Path(current) / name
            try:
                if path.stat().st_size > _MAX_CRED_BYTES:
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if _find_token(data) is not None:
                return True
    return False


def render_qr(url: str) -> Path | None:
    """把授权链接渲染成 PNG 二维码；``segno`` 缺失时返回 ``None``。"""
    try:
        import segno
    except ImportError:
        return None
    target = Path(tempfile.mkdtemp(prefix="antigravity-login-")) / "login-qr.png"
    try:
        segno.make(url, error="m").save(str(target), scale=8, border=3)
    except Exception:  # noqa: BLE001 - 二维码是增强项，失败不能中断登录
        return None
    return target


def _stdin_reader(target: "queue.Queue[str]") -> None:
    try:
        line = sys.stdin.readline()
    except Exception:  # noqa: BLE001
        line = ""
    target.put(line)


def _child_env() -> dict[str, str]:
    env = dict(os.environ)
    # 关键开关：让内核走非交互的 OOB 认证（打印链接 + 从 stdin 读授权码）。
    env["ANTIGRAVITY_REMOTE_OOB_AUTH"] = "1"
    env["TERM"] = "xterm-256color"
    env["NO_COLOR"] = "1"
    # OOB 流程本身要求 TTY，不要用 CI 之类的开关把它压成「静默失败」。
    env.pop("CI", None)
    return env


def main() -> int:
    # 伪终端只在 POSIX 上存在。这里延迟导入：``agents/registry`` 启动时会 import
    # ``builtin/`` 下的所有模块，顶层 ``import pty`` 会让 Windows 后端直接起不来。
    try:
        import pty
        import termios
    except ImportError:
        print(
            "当前系统不支持伪终端（Windows），Antigravity 的授权码登录只能在 Linux / macOS 完成。",
            flush=True,
        )
        return 2

    exe = shutil.which("agy")
    if not exe:
        print("未找到命令 `agy`，请先在内核管理里安装 Antigravity CLI。", flush=True)
        return 2

    if credentials_present():
        print("检测到已有登录态，本次将重新发起授权。", flush=True)

    started = time.monotonic()
    print("正在启动 Antigravity 登录流程…", flush=True)

    master: int | None = None
    slave: int | None = None
    try:
        master, slave = pty.openpty()
        # 关闭终端回显：写进主端的授权码会被 pty 原样回显，若被下面的 drain_lines
        # 打印出去就会进入任务日志 / SSE（凭据泄露）。ECHO 只影响回显，不影响读取。
        try:
            attrs = termios.tcgetattr(slave)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
        except OSError as exc:
            # 关不掉回显也不中断登录：下面还有一层「打印前抹掉授权码」的兜底。
            print(f"警告：无法关闭终端回显（{exc}），将按提交内容脱敏日志。", flush=True)
        proc = subprocess.Popen(
            [exe, "-p", "hi"],
            env=_child_env(),
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
    except OSError as exc:
        print(f"无法启动 Antigravity CLI：{exc}", flush=True)
        for fd in (master, slave):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        return 1
    assert slave is not None
    os.close(slave)
    assert master is not None

    buffer = ""
    url_sent = False
    code_sent = False
    qr_target: Path | None = None
    exit_code = 1
    code_queue: "queue.Queue[str]" = queue.Queue()
    #: 已提交的授权码：伪终端即便回显（关闭 ECHO 失败时），也不能让它出现在日志里。
    secrets: list[str] = []

    def drain_lines() -> list[str]:
        """把伪终端里已到达的内容按行切出来（同时把 \\r\\n 归一成 \\n）。"""
        nonlocal buffer
        ready, _, _ = select.select([master], [], [], _POLL_INTERVAL_SECONDS)
        if ready:
            try:
                data = os.read(master, 65536)
            except OSError:
                data = b""
            if data:
                buffer += _ANSI.sub("", data.decode("utf-8", "replace")).replace("\r\n", "\n").replace("\r", "\n")
        lines: list[str] = []
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            if not line.strip():
                continue
            for secret in secrets:
                if secret and secret in line:
                    line = line.replace(secret, "***")
            lines.append(line)
        return lines

    try:
        deadline = started + _URL_WAIT_SECONDS
        while time.monotonic() < deadline:
            for line in drain_lines():
                print(line, flush=True)
                if not url_sent:
                    match = _AUTH_URL.search(line)
                    if match:
                        url = match.group(0).rstrip(".,)")
                        qr_target = render_qr(url)
                        if qr_target is not None:
                            print(f"QR_IMAGE={qr_target}", flush=True)
                        url_sent = True
                if url_sent and not code_sent and _CODE_HINT in line.lower():
                    print(
                        "INPUT_PROMPT=请把浏览器里显示的授权码粘贴到这里（授权窗口约 60 秒）",
                        flush=True,
                    )
                    threading.Thread(
                        target=_stdin_reader, args=(code_queue,), daemon=True
                    ).start()
                    try:
                        code = code_queue.get(timeout=max(1.0, _AUTH_TIMEOUT_SECONDS))
                    except queue.Empty:
                        code = ""
                    code = (code or "").strip()
                    if not code:
                        print("没有收到授权码，登录中止。", flush=True)
                        return 1
                    # 先登记再写：万一终端回显，后面 drain_lines 也会把它抹成 ***。
                    secrets.append(code)
                    os.write(master, (code + "\n").encode("utf-8"))
                    code_sent = True
                    # 提交后重新给一个「等凭据」的窗口。
                    deadline = time.monotonic() + _SUCCESS_WAIT_SECONDS
                    break
            if code_sent and credentials_present():
                exit_code = 0
                print("登录成功，凭据已保存。", flush=True)
                # 抢在退出前把内核后续输出（真实 stream-json 样本）带进任务日志，
                # 便于之后校准适配器的解析；不阻塞太久。
                grace = time.monotonic() + 3.0
                while time.monotonic() < grace:
                    for line in drain_lines():
                        print(line, flush=True)
                break
            if proc.poll() is not None:
                # 进程退出前可能已经把凭据写完了，收尾再确认一次。
                if code_sent and credentials_present():
                    exit_code = 0
                    print("登录成功，凭据已保存。", flush=True)
                break

        if exit_code != 0:
            if not url_sent:
                print(
                    "等待授权链接超时：可能是内核版本变化或代理不可达"
                    "（Google 域名需要 HTTPS_PROXY）。",
                    flush=True,
                )
            else:
                print(
                    "登录未完成：没有检测到凭据落盘（授权码可能无效或已超时）。",
                    flush=True,
                )
    finally:
        try:
            os.close(master)
        except OSError:
            pass
        try:
            if proc.poll() is None:
                proc.terminate()
                time.sleep(0.3)
                proc.kill()
        except OSError:
            pass
        if qr_target is not None:
            try:
                qr_target.unlink()
            except OSError:
                pass
            shutil.rmtree(qr_target.parent, ignore_errors=True)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
