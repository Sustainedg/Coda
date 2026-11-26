"""CodeBuddy 可视化登录助手的行为测试。

助手本身是一个独立的登录命令：它拉起 ``codebuddy --serve``，把授权链接渲染成二维码，
再轮询登录状态。这里不启动真正的内核：用一个本地 HTTP 假服务验证「拿链接 → 出二维码 →
轮询到已登录」的主链路，以及站点选择、二维码渲染这些纯函数。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.agents.builtin import codebuddy_accounts, codebuddy_login

LIVE_NAME = "Tencent-Cloud.coding-copilot.info"

COCO = {"userId": "u-coco", "userName": "coco", "enterpriseName": "Tencent"}
NIHILO = {"userId": "u-nihilo", "userName": "nihilo", "enterpriseName": "Tencent"}
BOB = {"userId": "u-bob", "userName": "bob", "enterpriseName": "Acme"}


class _FakeServe(BaseHTTPRequestHandler):
    """模拟 ``codebuddy --serve`` 的账号登录接口。

    ``pre_authenticated`` 控制登录前是否已有账号（已登录时浏览器会拿旧 cookie 再授权
    一次，助手要能识别这一点）；``next_account`` 是「本次授权得到的账号」。
    """

    pre_authenticated = False
    login_done = False
    account: dict = dict(COCO)
    next_account: dict | None = None
    seen_headers: list[dict[str, str]] = []
    #: 模拟「真的完成了一次授权，内核把新 session 写盘」
    write_session_on_login = True
    login_sequence = 0

    def log_message(self, *_args):  # noqa: D401 - 测试里不需要访问日志
        return

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler 的约定
        type(self).seen_headers.append(dict(self.headers))
        if not self.path.endswith("/api/v1/auth/account/status"):
            self.send_error(404)
            return
        if type(self).login_done or type(self).pre_authenticated:
            self._send({"data": {"authenticated": True, "account": dict(type(self).account)}})
        else:
            self._send({"data": {"authenticated": False, "loginPending": True}})

    def do_POST(self):  # noqa: N802
        type(self).seen_headers.append(dict(self.headers))
        if not self.path.endswith("/api/v1/auth/account/login"):
            self.send_error(404)
            return
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        if type(self).next_account is not None:
            type(self).account = dict(type(self).next_account)
        type(self).login_done = True
        if type(self).write_session_on_login:
            # 模拟内核把新 session 写盘（即使还是同一个账号，token 也会变，字节不同）。
            type(self).login_sequence += 1
            live = codebuddy_accounts.auth_store_dir() / LIVE_NAME
            live.parent.mkdir(parents=True, exist_ok=True)
            live.write_bytes(
                f"session-{type(self).account['userId']}-{type(self).login_sequence}".encode()
            )
        self._send(
            {
                "data": {
                    "success": True,
                    "authUrl": "https://copilot.tencent.com/login?platform=CLI&state=abc",
                }
            }
        )


def _reset_fake(
    pre_authenticated: bool,
    account: dict,
    next_account: dict | None,
    *,
    write_session: bool = True,
) -> None:
    _FakeServe.pre_authenticated = pre_authenticated
    _FakeServe.login_done = False
    _FakeServe.account = dict(account)
    _FakeServe.next_account = None if next_account is None else dict(next_account)
    _FakeServe.seen_headers = []
    _FakeServe.write_session_on_login = write_session
    _FakeServe.login_sequence = 0


@pytest.fixture()
def fake_serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeServe)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


class _FakeProcess:
    pid = 424242

    def poll(self):
        return None


def test_login_method_precedence(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEBUDDY_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEBUDDY_LOGIN_METHOD", raising=False)
    monkeypatch.delenv("CODEBUDDY_INTERNET_ENVIRONMENT", raising=False)

    # 没有任何配置时默认中国版
    assert codebuddy_login.login_method() == "internal"

    # 适配器的「官方网络环境」同一套取值
    monkeypatch.setenv("CODEBUDDY_INTERNET_ENVIRONMENT", "ioa")
    assert codebuddy_login.login_method() == "iOA"
    monkeypatch.setenv("CODEBUDDY_INTERNET_ENVIRONMENT", "selfhosted")
    assert codebuddy_login.login_method() == "selfhosted"

    # settings*.json 的 env 同样生效
    monkeypatch.delenv("CODEBUDDY_INTERNET_ENVIRONMENT")
    cfg = tmp_path / ".codebuddy"
    cfg.mkdir()
    (cfg / "settings.json").write_text(
        json.dumps({"env": {"CODEBUDDY_INTERNET_ENVIRONMENT": "external"}}), encoding="utf-8"
    )
    assert codebuddy_login.login_method() == "external"

    # 显式覆盖优先级最高
    monkeypatch.setenv("CODEBUDDY_LOGIN_METHOD", "external")
    assert codebuddy_login.login_method() == "external"


def test_render_qr_writes_png(tmp_path, monkeypatch):
    monkeypatch.setattr(codebuddy_login.tempfile, "mkdtemp", lambda **_: str(tmp_path))
    target = codebuddy_login.render_qr("https://copilot.tencent.com/login?state=abc")
    assert target is not None
    assert target.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def _prepare(monkeypatch, tmp_path, fake_serve) -> None:
    """把助手切换到「假服务 + 隔离 HOME + 不真起进程」的模式。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in (
        "CODEBUDDY_CONFIG_DIR",
        "CODEBUDDY_LOGIN_METHOD",
        "CODEBUDDY_INTERNET_ENVIRONMENT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(codebuddy_login.shutil, "which", lambda _name: "/usr/bin/codebuddy")
    monkeypatch.setattr(codebuddy_login, "_free_port", lambda: fake_serve.server_address[1])
    monkeypatch.setattr(codebuddy_login.subprocess, "Popen", lambda *a, **k: _FakeProcess())
    monkeypatch.setattr(codebuddy_login, "_stop", lambda _process: None)
    monkeypatch.setattr(codebuddy_login, "_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(codebuddy_login, "_MIN_LOGIN_SECONDS", 0)
    monkeypatch.setattr(codebuddy_login, "render_qr", lambda _url: tmp_path / "qr.png")
    (tmp_path / "qr.png").write_bytes(b"\x89PNG\r\n\x1a\n")


def _write_live(uid: str) -> None:
    live = codebuddy_accounts.auth_store_dir() / LIVE_NAME
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(f"session-{uid}".encode())


def test_switch_account_url_only_rewrites_domestic_login():
    url = "https://copilot.tencent.com/login?platform=CLI&state=s1&version=2"
    switched = codebuddy_login.switch_account_url(url)
    assert switched == (
        "https://copilot.tencent.com/login/switch-account?platform=CLI&state=s1&version=2"
    )
    # 国际站是另一套登录前端，不能改路径
    intl = "https://www.codebuddy.ai/login?state=s1"
    assert codebuddy_login.switch_account_url(intl) == intl
    # 非 /login 路径不动
    other = "https://copilot.tencent.com/other?state=s1"
    assert codebuddy_login.switch_account_url(other) == other


def test_main_first_login_prints_qr_and_saves_account(
    fake_serve, monkeypatch, capsys, tmp_path
):
    _reset_fake(pre_authenticated=False, account=COCO, next_account=None)
    _prepare(monkeypatch, tmp_path, fake_serve)

    assert codebuddy_login.main() == 0

    out = capsys.readouterr().out
    assert "QR_IMAGE=" in out
    assert "https://copilot.tencent.com/login?" in out
    assert "登录成功" in out
    assert "coco" in out
    # 首次登录不需要「切换账号」落地页
    assert "switch-account" not in out
    # 新登录的账号自动归档，之后可以切回来
    assert [item["label"] for item in codebuddy_accounts.list_saved()] == ["coco · Tencent"]

    # 每次调用都带上了防 CSRF 的安全头（HTTP 头名大小写不敏感）
    assert _FakeServe.seen_headers
    assert all(
        any(key.lower() == "x-codebuddy-request" and value == "1" for key, value in headers.items())
        for headers in _FakeServe.seen_headers
    )


def test_main_warns_when_same_account_is_authorized_again(
    fake_serve, monkeypatch, capsys, tmp_path
):
    """浏览器拿旧 cookie 再授权一次时，不能假装「新增了账号」。"""
    _reset_fake(pre_authenticated=True, account=NIHILO, next_account=NIHILO)
    _prepare(monkeypatch, tmp_path, fake_serve)
    _write_live("u-nihilo")

    assert codebuddy_login.main() == 0

    out = capsys.readouterr().out
    # 已登录时会改成「切换账号」落地页，并提示先点「登录其他账号」
    assert "/login/switch-account" in out
    assert "当前已登录一个账号" in out
    assert "仍是当前账号" in out
    assert [item["label"] for item in codebuddy_accounts.list_saved()] == ["nihilo · Tencent"]


def test_main_does_not_report_success_before_any_scan(
    fake_serve, monkeypatch, capsys, tmp_path
):
    """status 在已有登录态时会立刻返回 authenticated —— 没扫码不能算登录成功。"""
    _reset_fake(
        pre_authenticated=True,
        account=NIHILO,
        next_account=NIHILO,
        write_session=False,
    )
    _prepare(monkeypatch, tmp_path, fake_serve)
    _write_live("u-nihilo")
    monkeypatch.setattr(codebuddy_login, "_LOGIN_TIMEOUT_SECONDS", 0.15)

    assert codebuddy_login.main() == 1

    out = capsys.readouterr().out
    assert "登录成功" not in out
    assert "没有检测到新的登录授权" in out
    # 原账号的 session 文件没有被改动
    assert (codebuddy_accounts.auth_store_dir() / LIVE_NAME).read_bytes() == b"session-u-nihilo"


def test_main_saves_second_account_after_real_switch(
    fake_serve, monkeypatch, capsys, tmp_path
):
    _reset_fake(pre_authenticated=True, account=NIHILO, next_account=BOB)
    _prepare(monkeypatch, tmp_path, fake_serve)
    _write_live("u-nihilo")

    assert codebuddy_login.main() == 0

    out = capsys.readouterr().out
    assert "仍是当前账号" not in out
    assert {item["label"] for item in codebuddy_accounts.list_saved()} == {
        "nihilo · Tencent",
        "bob · Acme",
    }


def test_main_reports_missing_executable(monkeypatch, capsys):
    monkeypatch.setattr(codebuddy_login.shutil, "which", lambda _name: None)
    assert codebuddy_login.main() == 2
    assert "未找到命令" in capsys.readouterr().out


def test_module_import_has_no_side_effects():
    """适配器目录会被自动 import：助手模块只能定义函数，不能有启动副作用。"""
    assert Path(codebuddy_login.__file__).name == "codebuddy_login.py"
    assert callable(codebuddy_login.main)


def test_serve_argv_on_windows_passes_one_command_string(monkeypatch):
    """Windows 上登录服务要走 ``cmd.exe``，且与真实运行用同一套拼法。

    平台里 npm 垫片只有 ``subprocess.list2cmdline`` 拼整串这一种形态
    （``runtime/runner.py:_spawn_argv`` 及其单测钉着它）。助手另起一种拼法，
    就会在引号 / 空格上出现两套行为：登录服务起不来，扫码永远完不成。
    """
    monkeypatch.setattr(codebuddy_login.sys, "platform", "win32")
    exe = r"C:\Users\John Doe\AppData\Roaming\Coda\node\global\codebuddy.cmd"

    argv = codebuddy_login._serve_argv(exe, 54321)

    assert argv[:4] == [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c"]
    # 整条命令作为一个参数交给 cmd.exe，参数里的空格不会被拆开
    assert argv[4] == subprocess.list2cmdline(
        [exe, "--serve", "--host", "127.0.0.1", "--port", "54321", "--auth", "none"]
    )


def test_serve_argv_on_posix_keeps_plain_argv(monkeypatch):
    """POSIX 上直接 exec，不经 cmd.exe。"""
    monkeypatch.setattr(codebuddy_login.sys, "platform", "linux")
    exe = "/usr/local/bin/codebuddy"

    assert codebuddy_login._serve_argv(exe, 54321) == [
        exe,
        "--serve",
        "--host",
        "127.0.0.1",
        "--port",
        "54321",
        "--auth",
        "none",
    ]
