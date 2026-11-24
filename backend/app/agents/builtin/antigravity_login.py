"""Google Antigravity CLI 的双向登录助手（OAuth 授权码模式）。

Antigravity CLI 没有 ``login`` 子命令；但在**终端**里设置
``ANTIGRAVITY_REMOTE_OOB_AUTH=1`` 后，打印模式会走「OOB 认证」：

* 打印 ``Authentication required. Please visit the URL to log in:`` 与授权链接；
* 提示 ``Or, paste the authorization code here and press Enter:``；
* 从 stdin 读一行授权码，换 token 后把登录态写进自己的数据目录。

注意：这套 OOB 流程要求 stdin/stdout 是 **TTY**（管道会退回「请先运行 agy 登录」），
所以本脚本用伪终端拉起内核。随后把这条链路包成平台的通用登录协议：

1. **先探一次网络**：直连 / 代理都连不上 Google 的 token 端点时直接报错退出 ——
   授权码是一次性的，让用户白扫码再失败是最糟糕的体验；
2. 用伪终端 + ``ANTIGRAVITY_REMOTE_OOB_AUTH=1`` 拉起 ``agy -p``；
3. 抓到授权链接后渲染二维码、打印 ``QR_IMAGE=<路径>``；
4. 打印 ``INPUT_PROMPT=<提示>`` 声明「需要用户把授权码贴回来」；
5. 从 **stdin** 读入界面提交的授权码，写回伪终端；
6. 轮询数据目录里的凭据落盘，成功退出 0，失败 / 超时退出 1；
7. 失败若是**网络问题**（token 交换时 EOF / 超时），自动重跑一轮 ——
   授权码已经烧掉，只能重新扫码，但至少不用用户再从界面点一次。

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
import urllib.error
import urllib.parse
import urllib.request
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
#: 网络类失败自动重跑一轮的上限（含首次）。授权码烧掉后只能重扫，
#: 但「Google 端点抽风一次就整轮作废」对用户太不友好，补一次很便宜。
_MAX_LOGIN_ATTEMPTS = 2

#: 登录前探测的 token 端点。授权码是**一次性**的，网络不通时必须挡在扫码之前。
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_PROBE_TIMEOUT_SECONDS = 8.0
#: 代理对 Google 域名的握手本身就会偶发 EOF / 超时，多试几次再下结论，
#: 别把「抖了一下」误判成「网络不通」。
_PROBE_ATTEMPTS = 3
#: 探测失败后的间隔：代理故障常常是一两秒的「连坐」，立刻重试没意义。
_PROBE_RETRY_DELAY_SECONDS = 1.0

#: 标准的代理环境变量（``agy`` 是 Go 程序，只认这几个名字）。
_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)
#: 部署侧的「安装期代理」。手工在容器里执行登录助手时，环境里往往只有它。
_DEPLOY_PROXY_ENV = "CODA_INSTALL_PROXY"

#: 授权链接（Antigravity 用 Google 的 OAuth 授权端点）。
_AUTH_URL = re.compile(r"https://accounts\.google\.com/o/oauth2/\S+")

#: 需要用户输入的提示行。
_CODE_HINT = "paste the authorization code here"

#: 内核把「换 token 时的网络失败」原样打在 stderr 上，据此判断要不要自动重跑一轮。
#: 注意别把 ``invalid_grant``（授权码本身的问题）算成网络失败 —— 那是重扫也救不回来的。
_NETWORK_FAILURE = re.compile(
    r"token exchange failed due to network error"
    r"|token exchange failed: Post \"https://oauth2\.googleapis\.com/token\":",
    re.IGNORECASE,
)

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


def _stdin_reader(target: "queue.Queue[str | None]") -> None:
    """把 stdin 的每一行都塞进队列；读到 EOF 时放一个 ``None`` 收尾。

    只读一行是不够的：网络抖动时助手会重跑一轮，第二轮的授权码也得有人接住。
    线程是 daemon 的，进程退出时自然结束。
    """
    while True:
        try:
            line = sys.stdin.readline()
        except (OSError, ValueError) as exc:
            # 读不到就明说，别让「没有收到授权码」把真正的原因盖掉。
            print(f"读取授权码输入失败：{exc}，请重新发起登录。", flush=True)
            target.put(None)
            return
        if not line:
            # stdin 已关闭：等待方据此立刻收尾，不用干等满整个窗口。
            target.put(None)
            return
        target.put(line)


def _wait_for_code(
    target: "queue.Queue[str | None]", proc: "subprocess.Popen[bytes]", timeout: float
) -> str:
    """等用户提交授权码，最多 ``timeout`` 秒；内核先退出就立刻返回空串。

    不能直接 ``get(timeout=75)``：内核对授权窗口是 60 秒，它退出后我们还会白等十几秒，
    期间用户贴回来的码会被写进一个已经死掉的伪终端（``OSError``）。
    空行不当提交（手工执行时误按回车很常见），继续等下一个非空行。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            pending = target.get(timeout=_POLL_INTERVAL_SECONDS)
        except queue.Empty:
            if proc.poll() is not None:
                return ""
            continue
        if pending is None:
            # stdin 已关闭，不会再有输入了。
            return ""
        code = pending.strip()
        if code:
            return code
    return ""


def _mask_proxy(url: str) -> str:
    """代理地址去掉账号口令：登录日志会进任务日志与 SSE，凭据不能跟着跑。"""
    try:
        parts = urllib.parse.urlsplit(url if "://" in url else f"http://{url}")
    except ValueError:
        return "（地址无法解析）"
    scheme = parts.scheme or "http"
    host = parts.hostname or ""
    return f"{scheme}://{host}:{parts.port}" if parts.port else f"{scheme}://{host}"


def _effective_env() -> dict[str, str]:
    """本次登录用的基础环境：没有标准代理变量时，沿用部署侧注入的安装期代理。

    平台发起的登录会从「设置 · 网络代理」注入 ``HTTP(S)_PROXY``；这条兜底只对
    「在容器里手工执行登录助手」有用（那种场景下环境里通常只有 ``CODA_INSTALL_PROXY``，
    而 ``agy`` 是 Go 程序，只认标准变量名）。返回副本，不去改 ``os.environ``。
    """
    env = dict(os.environ)
    if any(env.get(key) for key in _PROXY_ENV_KEYS):
        return env
    proxy = (env.get(_DEPLOY_PROXY_ENV) or "").strip()
    if proxy:
        for key in _PROXY_ENV_KEYS:
            env[key] = proxy
    return env


def _proxy_of(env: dict[str, str]) -> str | None:
    """环境里生效的代理地址；没配返回 ``None``。"""
    for key in _PROXY_ENV_KEYS:
        value = (env.get(key) or "").strip()
        if value:
            return value
    return None


def _child_env(base: dict[str, str]) -> dict[str, str]:
    env = dict(base)
    # 关键开关：让内核走非交互的 OOB 认证（打印链接 + 从 stdin 读授权码）。
    env["ANTIGRAVITY_REMOTE_OOB_AUTH"] = "1"
    env["TERM"] = "xterm-256color"
    env["NO_COLOR"] = "1"
    # OOB 流程本身要求 TTY，不要用 CI 之类的开关把它压成「静默失败」。
    env.pop("CI", None)
    return env


def _token_endpoint_opener(env: dict[str, str]) -> "urllib.request.OpenerDirector":
    """按给定环境构造 opener：代理从 ``env`` 取，不去读全局 ``os.environ``。"""
    proxy = _proxy_of(env)
    handler = (
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        if proxy
        else urllib.request.ProxyHandler({})
    )
    return urllib.request.build_opener(handler)


def _probe_token_endpoint(env: dict[str, str]) -> str | None:
    """扫码之前先探一次 Google 的 token 端点：通了返回 ``None``，否则返回可读原因。

    授权码是**一次性**的：让用户扫码授权、贴回授权码，最后卡在换 token 的网络请求上，
    是最糟糕的体验。所以先用一个空 POST 打一下端点 —— 只要 TCP + TLS 能建连（哪怕是
    400 / 401 的业务拒绝），就说明链路是通的。代理与子进程用同一份配置。
    """
    opener = _token_endpoint_opener(env)
    last = "未知网络错误"
    for _ in range(_PROBE_ATTEMPTS):
        # 每轮新建 Request：urlopen 会往对象上挂连接状态，复用同一份容易踩坑。
        request = urllib.request.Request(
            _TOKEN_ENDPOINT,
            data=b"",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with opener.open(request, timeout=_PROBE_TIMEOUT_SECONDS) as response:
                response.read(1)
            return None
        except urllib.error.HTTPError:
            # 4xx / 5xx 也是「连上了」：我们要的只是链路可达，不是业务成功。
            return None
        except Exception as exc:  # noqa: BLE001 - 探测失败只用于提示，不往外抛
            last = str(exc) or type(exc).__name__
            # 代理故障常常是一两秒的「连坐」：稍等再试，别在同一瞬连续撞墙。
            time.sleep(_PROBE_RETRY_DELAY_SECONDS)
    return last


def _run_once(
    exe: str, exec_env: dict[str, str], code_queue: "queue.Queue[str | None]"
) -> tuple[int, bool]:
    """跑一轮「打印链接 → 收授权码 → 换 token」。

    返回 ``(退出码, 是否网络类失败)``。后者的用途是决定要不要自动重跑一轮：
    网络抖动导致的失败与授权码无关，重扫一次大概率能成，不该让用户从界面再点一次。

    ``code_queue`` 由调用方持有：整轮登录只起一个读 stdin 的线程，重跑时复用同一条队列。
    """
    # 伪终端只在 POSIX 上存在。这里延迟导入：``agents/registry`` 启动时会 import
    # ``builtin/`` 下的所有模块，顶层 ``import pty`` 会让 Windows 后端直接起不来。
    import pty
    import termios

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
            env=exec_env,
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
        return 1, False
    assert slave is not None
    os.close(slave)
    assert master is not None

    buffer = ""
    url_sent = False
    code_sent = False
    network_failed = False
    qr_target: Path | None = None
    exit_code = 1
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
                if _NETWORK_FAILURE.search(line):
                    network_failed = True
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
                    code = _wait_for_code(
                        code_queue, proc, max(1.0, _AUTH_TIMEOUT_SECONDS)
                    )
                    if not code:
                        if proc.poll() is not None:
                            print(
                                "内核在授权窗口（约 60 秒）内已经退出，登录中止；"
                                "请重新发起登录，先把浏览器打开好再扫码。",
                                flush=True,
                            )
                        else:
                            print("没有收到授权码，登录中止。", flush=True)
                        return 1, False
                    # 先登记再写：万一终端回显，后面 drain_lines 也会把它抹成 ***。
                    secrets.append(code)
                    try:
                        os.write(master, (code + "\n").encode("utf-8"))
                    except OSError as exc:
                        print(
                            f"无法把授权码写回内核：{exc}。内核可能已经退出，请重新发起登录。",
                            flush=True,
                        )
                        return 1, False
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
                # 也把缓冲里最后几行（内核的报错就在这里）读完，否则判断不出是不是网络失败。
                for line in drain_lines():
                    print(line, flush=True)
                    if _NETWORK_FAILURE.search(line):
                        network_failed = True
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
            elif network_failed:
                print(
                    "登录未完成：与 Google 换取 token 时连接被中断"
                    "（EOF / TLS 握手超时 / i/o timeout）。",
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

    return exit_code, network_failed


def main() -> int:
    # 伪终端只在 POSIX 上存在；这里只做能力判断，真正的 import 在 ``_run_once`` 里。
    try:
        import pty  # noqa: F401
        import termios  # noqa: F401
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

    # 手工执行时环境里可能只有部署的安装期代理，_effective_env 会把它补成标准变量名。
    env = _effective_env()
    proxy = _proxy_of(env)
    print(f"网络代理：{_mask_proxy(proxy) if proxy else '未配置'}", flush=True)

    # 先探网络：授权码一次性，别让用户扫完码才发现换 token 的路是不通的。
    print("正在检查 Google 授权端点是否可达…", flush=True)
    unreachable = _probe_token_endpoint(env)
    if unreachable is not None:
        print(f"无法连接 Google 授权端点：{unreachable}", flush=True)
        print(
            "请先配好代理再登录：界面「设置 · 网络代理」填好代理地址；"
            "直接在本机执行时请设置 HTTPS_PROXY / HTTP_PROXY 环境变量。",
            flush=True,
        )
        print("本次未消耗任何授权码，扫码前就中止了。", flush=True)
        return 1

    #: 授权码输入队列：整轮登录只起一个读 stdin 的线程，重跑时继续用同一条队列。
    code_queue: "queue.Queue[str | None]" = queue.Queue()
    threading.Thread(target=_stdin_reader, args=(code_queue,), daemon=True).start()

    exit_code = 1
    network_failed = False
    for attempt in range(1, _MAX_LOGIN_ATTEMPTS + 1):
        if attempt > 1:
            print("网络抖动导致上一轮失败，正在重新发起授权（授权码已作废，需要重新扫码）…", flush=True)
        exit_code, network_failed = _run_once(exe, _child_env(env), code_queue)
        if exit_code == 0:
            return 0
        # 只有「网络类失败」才值得自动重跑：授权码过期 / 贴错重跑一百次也一样。
        if not network_failed or attempt >= _MAX_LOGIN_ATTEMPTS:
            break

    if exit_code != 0 and network_failed:
        print(
            "登录失败：换 token 这一步始终连不上 oauth2.googleapis.com。"
            "请检查代理节点是否稳定（同一代理对 Google 各个域名的可用性可能不同），"
            "换一个节点后再试；授权码是一次性的，每次重试都需要重新扫码。",
            flush=True,
        )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
