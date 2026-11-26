"""CodeBuddy CLI 的可视化登录助手。

内核没有 ``login`` 子命令，交互登录只在 TUI 的 ``/login`` 里；但 ``--serve`` 的
本地 HTTP API 把同一条登录链路暴露成了可编程接口：

* ``POST /api/v1/auth/account/login`` → ``{success, authUrl}``（浏览器授权地址）；
* ``GET  /api/v1/auth/account/status`` → ``{authenticated, loginPending, loginError}``。

本脚本是平台**通用登录协议**的一个实现：在回环地址拉起一次 ``codebuddy --serve``，
拿到授权链接后渲染成二维码并打印 ``QR_IMAGE=<路径>``，随后轮询登录状态，直到
授权完成（退出码 0）或超时（退出码 1）。框架只认 ``QR_IMAGE=`` 这一行，不认识
CodeBuddy —— 内核相关的知识都留在适配器目录里。

站点选择（决定打开哪一个登录页）：

1. ``CODEBUDDY_LOGIN_METHOD`` 显式覆盖（``internal`` / ``external`` / ``selfhosted`` / ``iOA``）；
2. 否则跟随 ``CODEBUDDY_INTERNET_ENVIRONMENT``（``internal`` / ``ioa`` / ``cloudhosted`` /
   ``selfhosted``，与适配器的「官方网络环境」同一套取值）；
3. 再否则读 ``settings*.json`` 里 ``env`` 的同一组变量；
4. 都没有时用 ``internal``（中国版）。海外部署显式设 ``CODEBUDDY_LOGIN_METHOD=external``。

二维码依赖纯 Python 的 ``segno``（不依赖 Pillow）。没装时只打印授权链接，登录流程
照常可用 —— 这也让脚本在最小部署里不会因为缺依赖而完全不可用。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

#: 与内核自身的登录轮询上限一致：授权链接的有效期通常是 5 分钟。
_LOGIN_TIMEOUT_SECONDS = 300.0
#: ``--serve`` 冷启动要加载配置 / 插件 / MCP，慢机器上十几秒也正常。
_SERVE_START_TIMEOUT_SECONDS = 60.0
#: 登录状态的轮询间隔。
_POLL_INTERVAL_SECONDS = 1.5
#: 刚发起登录后的最短等待：避免后台 token 刷新恰好写盘，被误判成「新登录完成」。
_MIN_LOGIN_SECONDS = 1.5
#: 授权链接的最大长度：登录 URL 很短，异常超长说明内核返回了脏数据。
_MAX_AUTH_URL_LENGTH = 4096

#: 请求头：``--serve`` 要求所有非豁免接口带这个安全头（防 CSRF / DNS rebinding）。
_REQUEST_HEADERS = {"x-codebuddy-request": "1", "content-type": "application/json"}

#: 登录服务只监听回环地址；安装环境可能注入了 HTTP_PROXY，这里显式绕过代理，
#: 否则 urllib 会把 127.0.0.1 的请求也发给代理。
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

#: 账号槽位模块。以 ``login_argv`` 方式执行时没有包上下文，按文件路径加载；
#: 被注册表自动 import 时走正常的相对导入。
_ACCOUNTS_MODULE: Any = None


def _accounts() -> Any:
    global _ACCOUNTS_MODULE
    if _ACCOUNTS_MODULE is not None:
        return _ACCOUNTS_MODULE
    try:
        from . import codebuddy_accounts as module
    except ImportError:
        import importlib.util

        path = Path(__file__).resolve().with_name("codebuddy_accounts.py")
        spec = importlib.util.spec_from_file_location("codebuddy_accounts", path)
        if spec is None or spec.loader is None:  # pragma: no cover - 文件缺失才会发生
            raise RuntimeError("无法加载账号槽位模块") from None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    _ACCOUNTS_MODULE = module
    return module

#: 站点取值 → 登录接口的 ``method``。
_SITE_METHODS = {
    "internal": "internal",
    "external": "external",
    "ioa": "iOA",
    "cloudhosted": "selfhosted",
    "selfhosted": "selfhosted",
}

#: 内核用户级配置文件名（后者覆盖前者）。
_SETTINGS_FILES = ("settings.json", "settings.local.json")


def _config_dir() -> Path:
    """与适配器一致的配置目录：尊重 ``CODEBUDDY_CONFIG_DIR``，默认 ``~/.codebuddy``。"""
    override = os.environ.get("CODEBUDDY_CONFIG_DIR", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".codebuddy"


def settings_env() -> dict[str, Any]:
    """读取 ``settings*.json`` 的 ``env`` 段（读不到 / 损坏都当没有）。"""
    merged: dict[str, Any] = {}
    for name in _SETTINGS_FILES:
        try:
            data = json.loads((_config_dir() / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("env"), dict):
            merged.update(data["env"])
    return merged


def login_method() -> str:
    """本次登录使用的站点（登录接口的 ``method``）。"""
    explicit = os.environ.get("CODEBUDDY_LOGIN_METHOD", "").strip()
    if explicit:
        return explicit
    file_env = settings_env()
    for value in (
        os.environ.get("CODEBUDDY_INTERNET_ENVIRONMENT", ""),
        str(file_env.get("CODEBUDDY_INTERNET_ENVIRONMENT") or ""),
    ):
        mapped = _SITE_METHODS.get(value.strip().lower())
        if mapped:
            return mapped
    return "internal"


def _site_label(method: str) -> str:
    return {
        "internal": "中国版 · copilot.tencent.com",
        "external": "国际站 · www.codebuddy.ai",
        "iOA": "iOA 企业版",
        "selfhosted": "专享版 / 私有化部署",
    }.get(method, method)


#: 支持「切换账号」落地页的域名。国际站用的是另一套登录前端，不能改路径。
_SWITCH_HOSTS = ("copilot.tencent.com", "www.codebuddy.cn", "www.workbuddy.cn")
_SWITCH_SUFFIXES = (".codebuddy.cn", ".workbuddy.cn")


def switch_account_url(auth_url: str) -> str:
    """把授权链接改成「切换账号」落地页（仅支持的国内站点）。

    这个页面会提供「登录其他账号」按钮：先登出浏览器里的旧登录，再回到 `/login`
    用同一个 state 继续授权。已登录一个账号后再点「登录其他账号」时必须走它，
    否则浏览器会拿旧 cookie 直接把当前账号再授权一遍（用户看到的就是「怎么还是
    原来那个账号」）。
    """
    try:
        parts = urllib.parse.urlsplit(auth_url)
    except ValueError:
        return auth_url
    host = (parts.hostname or "").lower()
    if host not in _SWITCH_HOSTS and not host.endswith(_SWITCH_SUFFIXES):
        return auth_url
    if parts.path.rstrip("/") != "/login":
        return auth_url
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, "/login/switch-account", parts.query, parts.fragment)
    )


def account_uid(account: Any) -> str:
    """从状态接口的 `account` 里取稳定 uid，用于判断「登录的是不是同一个账号」。"""
    if not isinstance(account, dict):
        return ""
    for key in ("userId", "uid"):
        value = account.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def session_hash(path: Path | None) -> str | None:
    """session 文件的 sha256；没有文件 / 读不了返回 ``None``。"""
    if path is None:
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _free_port() -> int:
    """借一个当前空闲的回环端口；关掉监听后立刻交给 ``--serve``。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _serve_argv(exe: str, port: int) -> list[str]:
    """启动 ``codebuddy --serve`` 的完整命令。

    Windows 上 npm 给的是 ``.cmd`` 垫片，``subprocess`` 不能直接执行，必须交给
    ``cmd.exe /d /s /c`` 解释。

    拼法与 ``runtime/runner.py:_spawn_argv`` 保持一致：用
    ``subprocess.list2cmdline`` 把整条命令（含 ``--serve`` 那一串参数）拼成一个
    字符串再交给 ``cmd``。平台里 npm 垫片只有这一种拼法（单测钉着它），
    登录助手与真实运行才不会在引号 / 空格上产生两套行为。
    """
    args = [
        exe,
        "--serve",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        # 只给本机登录助手用：不设网关口令，也不对外监听。
        "--auth",
        "none",
    ]
    if sys.platform == "win32" and exe.lower().endswith((".cmd", ".bat")):
        return [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline(args),
        ]
    return args


def _serve_env() -> dict[str, str]:
    env = {**os.environ}
    env.setdefault("DISABLE_TELEMETRY", "1")
    env.setdefault("DISABLE_ERROR_REPORTING", "1")
    env.setdefault("DISABLE_AUTOUPDATER", "1")
    env.setdefault("CI", "1")
    env.setdefault("NO_COLOR", "1")
    return env


def _read_json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None,
               timeout: float = 10.0) -> dict[str, Any]:
    """调一次本地 HTTP API；失败时抛出带可读原因的 ``RuntimeError``。"""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, headers=_REQUEST_HEADERS, method=method)
    try:
        with _LOCAL_OPENER.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}：{detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError("内核返回了非 JSON 响应") from exc
    return data if isinstance(data, dict) else {}


def _tail(path: Path, limit: int = 8) -> str:
    """取日志最后几行做失败诊断，不把整份日志倒进界面。"""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(line for line in lines[-limit:] if line.strip())


def _wait_ready(base: str, process: subprocess.Popen[Any], log_path: Path) -> bool:
    """等 ``--serve`` 起来；进程提前退出时把最后几行日志回显给用户。"""
    deadline = time.monotonic() + _SERVE_START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            print("CodeBuddy 登录服务启动失败。", flush=True)
            tail = _tail(log_path)
            if tail:
                print(tail, flush=True)
            return False
        try:
            _read_json(f"{base}/api/v1/auth/account/status", timeout=2.0)
            return True
        except RuntimeError:
            time.sleep(0.5)
    print("等待 CodeBuddy 登录服务就绪超时。", flush=True)
    return False


def render_qr(url: str) -> Path | None:
    """把授权链接渲染成 PNG 二维码；``segno`` 缺失时返回 ``None``。"""
    try:
        import segno
    except ImportError:
        return None
    target = Path(tempfile.mkdtemp(prefix="codebuddy-login-")) / "login-qr.png"
    try:
        segno.make(url, error="m").save(str(target), scale=8, border=3)
    except Exception:  # noqa: BLE001 - 二维码是增强项，渲染失败不能中断登录
        return None
    return target


def _stop(process: subprocess.Popen[Any]) -> None:
    """结束 ``--serve``（Windows 用 taskkill 杀进程树）。

    子进程**不**另开进程组：它和登录助手同组，框架在超时 / 取消时对助手所在进程组
    发 SIGKILL，会连同 ``--serve`` 一起收掉，不会留下孤儿。
    """
    if process.poll() is not None:
        return
    if os.name == "nt":  # pragma: no cover - 仅 Windows
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def _account_name(status: dict[str, Any]) -> str:
    account = status.get("account")
    if not isinstance(account, dict):
        return ""
    name = str(account.get("userName") or account.get("nickname") or "").strip()
    enterprise = str(account.get("enterpriseName") or "").strip()
    if name and enterprise:
        return f"{name}（{enterprise}）"
    return name or enterprise


def main() -> int:
    exe = shutil.which("codebuddy")
    if not exe:
        print("未找到命令 `codebuddy`，请先在内核管理里安装 CodeBuddy CLI。", flush=True)
        return 2

    method = login_method()
    print(f"登录站点：{_site_label(method)}", flush=True)

    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    log_dir = Path(tempfile.mkdtemp(prefix="codebuddy-serve-"))
    log_path = log_dir / "serve.log"
    qr_target: Path | None = None
    log_file = log_path.open("w", encoding="utf-8")

    print("正在启动 CodeBuddy 登录服务…", flush=True)
    try:
        process = subprocess.Popen(  # noqa: S603 - 可执行文件来自已安装的内核
            _serve_argv(exe, port),
            env=_serve_env(),
            cwd=str(log_dir),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:
        log_file.close()
        shutil.rmtree(log_dir, ignore_errors=True)
        print(f"无法启动 CodeBuddy 登录服务：{exc}", flush=True)
        return 1

    try:
        if not _wait_ready(base, process, log_path):
            return 1

        # 登录新账号前先把当前账号归档，否则它的 session 会被这次登录覆盖掉。
        accounts = _accounts()
        try:
            before = _read_json(f"{base}/api/v1/auth/account/status", timeout=5.0)
        except RuntimeError:
            before = {}
        before_data = before.get("data") if isinstance(before.get("data"), dict) else {}
        before_uid = account_uid(before_data.get("account"))
        had_account = before_data.get("authenticated") is True or (
            accounts.read_live_auth() is not None and not accounts.is_logged_out()
        )
        if before_data.get("authenticated") is True:
            saved_id = accounts.snapshot_live(before_data.get("account"))
            if saved_id:
                print("已保存当前账号，登录完成后可在账号列表里切换。", flush=True)

        # 关键：status 接口在「已有登录态」时会立刻返回 authenticated=true（旧 session），
        # 不能拿它当「新登录完成」。以 session 文件内容是否变化作为「新登录已落盘」的证据。
        before_hash = session_hash(accounts.read_live_auth())

        try:
            payload = _read_json(
                f"{base}/api/v1/auth/account/login",
                method="POST",
                payload={"method": method},
                timeout=15.0,
            )
        except RuntimeError as exc:
            print(f"获取授权链接失败：{exc}", flush=True)
            return 1
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        auth_url = str(data.get("authUrl") or "").strip()
        if not auth_url or len(auth_url) > _MAX_AUTH_URL_LENGTH or not auth_url.startswith(
            ("https://", "http://")
        ):
            reason = data.get("loginError") or "内核没有返回有效的授权链接"
            print(f"登录失败：{reason}", flush=True)
            return 1

        if had_account:
            # 已登录时浏览器会拿旧 cookie 直接把当前账号再授权一遍；换成「切换账号」
            # 落地页，让用户先登出浏览器里的旧登录，再登录另一个账号。
            switched = switch_account_url(auth_url)
            if switched != auth_url:
                auth_url = switched
                print(
                    "当前已登录一个账号：请在手机/浏览器打开的页面里点「登录其他账号」，"
                    "再完成新账号授权（不要直接确认当前账号）。",
                    flush=True,
                )

        print(f"授权链接：{auth_url}", flush=True)
        qr_target = render_qr(auth_url)
        if qr_target is not None:
            # 框架的通用登录协议：读取这个 PNG，嵌成 data URL 推给界面。
            print(f"QR_IMAGE={qr_target}", flush=True)
            print("请用手机扫描二维码，在浏览器里完成账号授权。", flush=True)
        else:
            print("未安装 segno，无法生成二维码；请在浏览器中打开上面的授权链接。", flush=True)

        login_started = time.monotonic()
        deadline = login_started + _LOGIN_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                status_payload = _read_json(f"{base}/api/v1/auth/account/status", timeout=5.0)
            except RuntimeError:
                time.sleep(_POLL_INTERVAL_SECONDS)
                continue
            status = status_payload.get("data") if isinstance(status_payload.get("data"), dict) else {}

            current_hash = session_hash(accounts.read_live_auth())
            session_changed = (
                current_hash is not None and before_hash is None
            ) or (
                current_hash is not None and before_hash is not None and current_hash != before_hash
            )
            settled = time.monotonic() - login_started >= _MIN_LOGIN_SECONDS

            if status.get("authenticated") is True and session_changed and settled:
                # session 文件已经换成新的；内核可能再过一瞬间才把新账号写进状态接口，
                # 多等半秒再确认一次，避免拿到旧账号信息（也就不会误报「同账号」）。
                time.sleep(0.5)
                try:
                    latest = _read_json(f"{base}/api/v1/auth/account/status", timeout=5.0)
                    status = latest.get("data") if isinstance(latest.get("data"), dict) else status
                except RuntimeError:
                    pass
                if status.get("authenticated") is not True:
                    time.sleep(_POLL_INTERVAL_SECONDS)
                    continue
                name = _account_name(status)
                new_uid = account_uid(status.get("account"))
                saved = accounts.snapshot_live(status.get("account"))
                same_account = bool(
                    had_account and before_uid and new_uid and new_uid == before_uid
                )
                if same_account:
                    print(
                        "⚠ 这次授权的仍是当前账号，没有新增账号。要换账号，请在授权页点"
                        "「登录其他账号」，或用无痕窗口 / 另一台手机重新扫码。",
                        flush=True,
                    )
                elif saved:
                    # 新账号也存成槽位，下次登录其他账号后还能切回来。
                    print("账号已保存，后续可在账号列表里直接切换。", flush=True)
                print(f"登录成功{f'：{name}' if name else ''}。", flush=True)
                return 0
            if status.get("loginError"):
                print(f"登录失败：{status['loginError']}", flush=True)
                return 1
            time.sleep(_POLL_INTERVAL_SECONDS)

        print(
            "登录超时（授权链接通常 5 分钟内有效），没有检测到新的登录授权；"
            "当前账号保持不变。",
            flush=True,
        )
        return 1
    finally:
        _stop(process)
        log_file.close()
        # 二维码里是短期授权链接、日志里可能带账号信息：登录结束都清掉。
        if qr_target is not None:
            try:
                qr_target.unlink()
            except OSError:
                pass
        shutil.rmtree(log_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
