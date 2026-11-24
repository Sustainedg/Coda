"""在宿主机上安装 / 更新 / 卸载智能体运行时（CLI 内核）。

安装与卸载是「改本机环境」的长任务，因此与一次对话运行同等对待：
受控子进程、逐行流式回显、总超时、命令脱敏；客户端断开连接时生成器被关闭，
``finally`` 里杀掉子进程——与对话一样天然可取消（铁律 10）。

本模块只认识适配器声明的 :class:`~app.agents.base.RuntimeProvisioner`，
不知道任何具体内核：装哪个包、用什么包管理器、动作前要清掉什么残留，
都是适配器的知识（``prepare_argv``）—— 这里只负责照声明执行。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from ..agents.base import AgentDefinition, RuntimeProvisioner
from ..settings import get_settings
from . import availability as availability_service
from .processes import kill_tree_async
from .runner import _StreamLines, _redact, _spawn_argv

Action = Literal["install", "update", "uninstall", "login"]

#: 动作的中文名，用于产出面向用户的提示。
ACTION_LABELS: dict[str, str] = {
    "install": "安装",
    "update": "更新",
    "uninstall": "卸载",
    "login": "登录",
}

#: 登录命令输出二维码的约定：``QR_IMAGE=<图片路径>``（豆包 CLI 的 ``login --qr=png``）。
#: 这是账号型内核的**通用**登录协议，框架不认识具体内核。等号两侧允许空白。
_QR_LINE = re.compile(r"^QR_IMAGE\s*=\s*(.+)$")

#: 登录命令请求用户输入的约定：``INPUT_PROMPT=<提示文本>``。
#: 有些 OAuth 内核必须把浏览器里的授权码贴回登录进程（Antigravity CLI 就是这种），
#: 框架据此把「需要输入」推给界面，并把用户提交的文本写回子进程 stdin。
#: 同样是**通用**协议，框架不认识具体内核。等号两侧允许空白。
_INPUT_PROMPT_LINE = re.compile(r"^INPUT_PROMPT\s*=\s*(.+)$")

#: 单次输入的长度上限：OAuth 授权码远小于这个值，超过按异常截断，避免脏数据灌进子进程。
_MAX_LOGIN_INPUT = 8192

#: 二维码图片上限：一张登录二维码远小于这个值，超过就按异常处理，避免把大文件塞进 SSE。
_QR_MAX_BYTES = 2 * 1024 * 1024

#: 允许的二维码图片格式（魔数 → MIME）。不做 SVG：SVG 可以内嵌脚本。
_QR_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
)


def _qr_data_url(raw_path: str, base_dir: str | None = None) -> tuple[str | None, str | None]:
    """把登录命令写出的二维码图片读成 ``data:`` URL。

    返回 ``(data_url, error)``。只认 PNG / JPEG / WebP（按魔数），限制大小；
    路径来自内核输出，属于受信任的本地命令，但仍不把原始路径回显给浏览器。
    相对路径按登录命令的工作目录解析。
    """
    path = (raw_path or "").strip().strip('"').strip("'")
    if path.startswith("file://"):
        path = path[len("file://"):]
    if not path:
        return None, "登录命令没有给出二维码路径"
    target = Path(path).expanduser()
    if not target.is_absolute() and base_dir:
        target = Path(base_dir) / target
    try:
        # 先量大小再读，避免把超大文件整块读进内存；错误提示不带路径，不回显本机路径。
        if target.stat().st_size > _QR_MAX_BYTES:
            return None, f"二维码图片超过 {_QR_MAX_BYTES // 1024 // 1024} MB"
        blob = target.read_bytes()
    except OSError:
        return None, "读取二维码失败（文件不存在或不可读）"
    if not blob:
        return None, "二维码文件为空"
    mime = None
    for signature, kind in _QR_SIGNATURES:
        if blob.startswith(signature):
            mime = kind
            break
    if mime is None and blob.startswith(b"RIFF") and blob[8:12] == b"WEBP":
        mime = "image/webp"
    if mime is None:
        return None, "二维码文件不是受支持的图片格式（PNG / JPEG / WebP）"
    encoded = base64.b64encode(blob).decode("ascii")
    return f"data:{mime};base64,{encoded}", None

#: 「查询最新可用版本」是很轻的元数据请求，给它一个单独的短超时。
_LATEST_TIMEOUT_SECONDS = 30.0

#: 失败提示里回显命令的长度上限。
_HINT_COMMAND_LIMIT = 120


def _missing_command_error(command: str, label: str, manager: str | None = None) -> str:
    """命令根本没起来时的用户文案。

    框架**不认识任何具体包管理器**（铁律 1：「用哪个包管理器、包名叫什么」是适配器的知识），
    所以这里只回显适配器声明的 ``manager``，不写死 node / npm，也不假设部署形态
    （同一个后端既跑桌面版也跑 Docker，桌面专属的路径提示只放在桌面文档里）。
    """
    message = f"未找到命令 `{command}`，无法执行{label}"
    if manager:
        message += f"（管理方式：{manager}；请确认相关命令与运行时已安装并在 PATH 中）"
    return message


def _event(kind: str, **payload: Any) -> dict[str, Any]:
    """产出一条 ``provision.*`` 事件。

    这是安装器自有的信封，**不是**适配器事件协议（铁律 5 不适用于它），
    与协作域 ``collab.*`` 同理：只在运行时页内部消费。
    """
    return {"type": f"provision.{kind}", **payload}


#: 展示用的命令长度上限。npm 内核的安装 / 更新是内联 Node 脚本（要处理本机既有的同名
#: 可执行文件），压成一行仍有上千字符；原样回显会把整段安装日志淹没。
_DISPLAY_LIMIT = 300


def _display(argv: list[str]) -> str:
    """把命令拼成一行展示。

    包装命令可能是多行的脚本（例如 npm 内核的安装 / 更新命令要先处理本机既有的
    同名可执行文件），压成一行才不会把日志与提示切断；过长再截断。
    """
    text = " ".join(" ".join(argv).split())
    return text if len(text) <= _DISPLAY_LIMIT else f"{text[:_DISPLAY_LIMIT]} …"


def _hint(display: str) -> str:
    """失败提示里回显命令：太长就截断（完整命令在日志与 ``provision.started`` 里）。"""
    return display if len(display) <= _HINT_COMMAND_LIMIT else f"{display[:_HINT_COMMAND_LIMIT]}…"


def _command_env() -> dict[str, str]:
    """安装命令的运行环境：继承进程环境，并关掉交互式提示与彩色输出。"""
    env = {**os.environ}
    env.setdefault("CI", "1")
    env.setdefault("NO_COLOR", "1")
    return env


async def _provision_env() -> dict[str, str]:
    """安装 / 更新 / 卸载 / 查询版本的运行环境：基础环境 + PATH 补齐 + 代理。"""
    env = _command_env()
    #: 用户安装脚本常把可执行文件放到 ~/.local/bin，服务进程的 PATH 可能不含该目录，
    #: 这里补上，让安装后的内核命令（hermes update --check 等）能被找到。
    home = env.get("HOME", "")
    if home:
        local_bin = os.path.join(home, ".local", "bin")
        path = env.get("PATH", "")
        if local_bin not in path:
            env["PATH"] = f"{local_bin}:{path}" if path else local_bin
    #: 从数据库读取代理设置，注入到子进程环境变量里，
    #: 让安装 / 更新 / 查询版本等命令能通过代理访问 GitHub / npm registry 等。
    try:
        from ..api.system import get_proxy_url

        proxy = await get_proxy_url()
    except Exception:
        proxy = None
    if proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                     "http_proxy", "https_proxy", "all_proxy"):
            env[key] = proxy
        #: git 单独认 http.proxy 配置，环境变量不够；通过 GIT_CONFIG_* 注入。
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.proxy"
        env["GIT_CONFIG_VALUE_0"] = proxy
    return env


def _workdir() -> str | None:
    """在用户主目录下执行安装，避免包管理器把产物写进本仓库。"""
    home = os.environ.get("HOME")
    return home if home and os.path.isdir(home) else None


class LoginInputChannel:
    """登录动作的双向输入通道：把界面提交的文本写回登录子进程的 stdin。

    大多数扫码型内核是单向的（框架只把二维码推出去，登录命令自己轮询完成）。
    OAuth 授权码型内核（例如 Antigravity CLI）必须在浏览器授权后把授权码贴回来：
    登录命令用 ``INPUT_PROMPT=`` 声明自己要读输入，框架把子进程的 stdin 挂到这里，
    API 层再通过 ``ProvisionJob.send_input()`` 把用户提交的文本送进去。

    通道与具体内核无关 —— 适配器只负责在自己的登录助手里打印约定行、从 stdin 读文本。
    """

    def __init__(self) -> None:
        self._writer: asyncio.StreamWriter | None = None

    def attach(self, process: asyncio.subprocess.Process) -> None:
        """子进程启动后挂上它的 stdin 写入端。"""
        self._writer = process.stdin

    def detach(self) -> None:
        self._writer = None

    @property
    def available(self) -> bool:
        """登录进程当前是否能接收输入（已启动且 stdin 未关闭）。"""
        writer = self._writer
        return writer is not None and not writer.is_closing()

    async def send(self, text: str) -> bool:
        """把一段文本写进登录进程；进程已退出 / stdin 已关闭时返回 ``False``。"""
        writer = self._writer
        if not self.available or writer is None:
            return False
        # 授权码是单行文本：统一按行提交，内核那边 ``readline()`` 才收得到。
        payload = text[:_MAX_LOGIN_INPUT].replace("\r", "").rstrip("\n") + "\n"
        try:
            writer.write(payload.encode("utf-8"))
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError):
            self._writer = None
            return False
        return True


@dataclass(slots=True)
class _CommandOutcome:
    """一条受控命令的执行结论。"""

    returncode: int | None = None
    #: 超过了本次动作的总时限，进程被强制终止
    timed_out: bool = False
    #: 命令根本没启动起来时的现成文案（已含动作名，面向用户）；正常启动则为 None
    error: str | None = None


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """结束一条安装命令：POSIX 杀整个进程组，Windows 杀进程树。

    包装命令自己会再起 npm（Node 的 ``spawnSync``），只杀直接子进程会留下仍在
    写文件的孤儿 —— 那正是「取消之后下一次安装永久失败在 ENOTEMPTY」的来源。
    """
    if process.returncode is not None:
        return
    if os.name == "nt":
        await kill_tree_async(process)
        return
    pid = process.pid
    if pid:
        try:
            # start_new_session=True 建的是独立进程组，负 pid = 整组
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    with contextlib.suppress(ProcessLookupError):
        process.kill()


async def _run_command(
    argv: list[str],
    env: dict[str, str],
    deadline: float,
    runtime_id: str,
    label: str,
    outcome: _CommandOutcome,
    *,
    qr: bool = False,
    manager: str | None = None,
    login_input: LoginInputChannel | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """在受控子进程里跑一条命令，逐行产出 ``provision.output``。

    结论（退出码 / 是否超时 / 是否压根没起来）写进调用方传入的 ``outcome``。
    这里**不**产出 ``provision.finished``：收尾要重测安装状态、判定成败，由调用方决定。
    超时会杀掉子进程；客户端断开时生成器被关闭，``finally`` 同样杀掉它（铁律 10）。

    传入 ``login_input`` 时子进程的 stdin 保持打开（双向管道），登录助手据此把用户
    提交的授权码读回去；否则沿用 ``DEVNULL``，避免普通命令误等输入。
    """
    try:
        process = await asyncio.create_subprocess_exec(
            # Windows 上 npm 分发的内核是 .cmd 垫片，CreateProcess 不能直接执行。
            *_spawn_argv(argv),
            cwd=_workdir(),
            env=env,
            stdin=asyncio.subprocess.PIPE if login_input is not None else asyncio.subprocess.DEVNULL,
            # 合并 stderr 到 stdout：安装日志里两者交错，合并后既能保证顺序，
            # 也避免两条流各自读取带来的死锁风险。
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # 包装命令（Node）自己会再起 npm / 安装器：放进独立进程组，
            # 取消 / 超时才能一锅端（POSIX 用 killpg，Windows 用 taskkill /T）。
            start_new_session=(os.name != "nt"),
        )
    except FileNotFoundError:
        outcome.error = _missing_command_error(argv[0], label, manager)
        return
    except PermissionError as exc:
        outcome.error = f"无法执行 `{argv[0]}`：{exc}"
        return

    try:
        assert process.stdout is not None
        # 双向登录：把子进程的 stdin 挂到通道上，界面提交的授权码从这里写回去。
        if login_input is not None:
            login_input.attach(process)
        lines = _StreamLines(process.stdout)
        while True:
            if asyncio.get_event_loop().time() > deadline:
                outcome.timed_out = True
                await _terminate(process)
                break
            try:
                line = await asyncio.wait_for(lines.readline(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if lines.overflowed:
                lines.overflowed = False
                yield _event("output", runtime_id=runtime_id, text="单行输出过长，已丢弃该行", level="warn")
            if line is None:
                break
            text = line.decode("utf-8", "replace")
            match = _QR_LINE.match(text.strip()) if qr else None
            if match:
                data_url, qr_error = _qr_data_url(match.group(1), _workdir())
                if data_url:
                    yield _event("qr", runtime_id=runtime_id, data_url=data_url)
                    yield _event(
                        "output",
                        runtime_id=runtime_id,
                        text="已生成登录二维码，请用手机扫码；扫码完成后会自动结束。",
                    )
                else:
                    yield _event(
                        "output",
                        runtime_id=runtime_id,
                        text=f"登录二维码不可用：{qr_error}",
                        level="warn",
                    )
                continue
            # 双向登录：登录助手声明要读输入时，把提示推给界面，等用户提交。
            prompt_match = _INPUT_PROMPT_LINE.match(text.strip()) if login_input is not None else None
            if prompt_match:
                yield _event(
                    "input",
                    runtime_id=runtime_id,
                    prompt=prompt_match.group(1).strip(),
                )
                continue
            yield _event("output", runtime_id=runtime_id, text=text)
        await process.wait()
    finally:
        if login_input is not None:
            login_input.detach()
        if process.returncode is None:
            await _terminate(process)
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=5)
    outcome.returncode = process.returncode


async def stream_provision(
    definition: AgentDefinition,
    action: Action,
    *,
    login_input: LoginInputChannel | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """执行一次安装 / 更新 / 卸载，并流式回显输出。

    终态始终是一条 ``provision.finished`` 事件（含 ``ok`` / ``status`` / ``version``），
    运行过程中是若干 ``provision.output``。缺命令等前置错误同样以 ``finished`` 收尾，
    保证调用方永远能看到结论（铁律 11）。

    适配器声明了 ``prepare_argv`` 时，先跑一遍清扫命令（清掉上一次被中断的动作留下的
    残留），再跑动作本身；两者共用同一个总时限。

    ``login_input`` 只在登录动作里有意义：它让登录子进程的 stdin 保持打开，
    并把 ``INPUT_PROMPT=`` 约定行翻译成 ``provision.input`` 事件（见
    :class:`LoginInputChannel`）。非登录动作传进来也不会用到。
    """
    provisioner = definition.provisioner
    label = ACTION_LABELS.get(action, action)
    if provisioner is None:
        yield _event("started", runtime_id=definition.id, action=action, command=None)
        yield _event(
            "finished",
            runtime_id=definition.id,
            action=action,
            ok=False,
            status="error",
            error=f"{definition.name} 不由平台管理，无法{label}",
        )
        return

    argv = provisioner.argv_for(action)
    if not argv:
        yield _event("started", runtime_id=definition.id, action=action, command=None)
        yield _event(
            "finished",
            runtime_id=definition.id,
            action=action,
            ok=False,
            status="error",
            error=f"{definition.name} 未声明「{label}」命令",
        )
        return

    env = await _provision_env()
    display = _display(_redact(argv, env))
    yield _event(
        "started",
        runtime_id=definition.id,
        action=action,
        command=display,
        manager=provisioner.manager,
    )

    timeout = float(get_settings().provision_timeout_seconds)
    deadline = asyncio.get_event_loop().time() + timeout

    # 动作之前先跑适配器声明的「清扫」命令：上一次动作被中断（超时、服务重启）可能留下
    # 让包管理器再也无法覆写安装目录的残留（见 `RuntimeProvisioner.prepare_argv`）。
    # 它只是清场，失败了也照常往下走 —— 成败只由动作自己的退出码决定。
    if provisioner.prepare_argv and action != "login":
        prepare_display = _display(_redact(provisioner.prepare_argv, env))
        yield _event("output", runtime_id=definition.id, text=f"清扫命令：{prepare_display}")
        prepare = _CommandOutcome()
        # aclosing：客户端断开时框架关闭的是本生成器，`async for` 不像 `yield from`
        # 那样代为关闭被迭代的生成器 —— 必须显式透传，否则子进程成孤儿（铁律 10）。
        async with contextlib.aclosing(
            _run_command(
                provisioner.prepare_argv,
                env,
                deadline,
                definition.id,
                "清扫",
                prepare,
                manager=provisioner.manager,
            )
        ) as prepare_stream:
            async for event in prepare_stream:
                yield event
        if prepare.error or prepare.timed_out or prepare.returncode:
            reason = prepare.error or ("超时" if prepare.timed_out else f"退出码 {prepare.returncode}")
            yield _event(
                "output",
                runtime_id=definition.id,
                text=f"清扫未完成（{reason}），继续执行{label}",
                level="warn",
            )

    outcome = _CommandOutcome()
    async with contextlib.aclosing(
        _run_command(
            argv, env, deadline, definition.id, label, outcome,
            qr=(action == "login"),
            manager=provisioner.manager,
            login_input=login_input if action == "login" else None,
        )
    ) as command_stream:
        async for event in command_stream:
            yield event
    if outcome.error:
        yield _event(
            "finished",
            runtime_id=definition.id,
            action=action,
            ok=False,
            status="error",
            error=outcome.error,
        )
        return

    if outcome.timed_out:
        yield _event(
            "output",
            runtime_id=definition.id,
            text=f"已超过 {int(timeout)} 秒上限，被强制终止",
            level="warn",
        )

    if action == "login":
        # 登录不改安装状态：直接重新探测一次账号登录态，把结论跟 finished 一起回给界面，
        # 前端不用再多打一次接口就能把「未登录」翻成「已登录」。
        auth = await asyncio.to_thread(definition.auth_status)
        # 登录进程刚退出时登录态可能还没落盘：最多再看两次，别把「刚扫完」判成未登录。
        for _ in range(2):
            if auth is None or auth.logged_in is not False:
                break
            await asyncio.sleep(1)
            auth = await asyncio.to_thread(definition.auth_status)
        ok = outcome.returncode == 0 and not outcome.timed_out
        if ok and auth is not None and auth.logged_in is False:
            ok = False
        error = None
        if not ok:
            if outcome.timed_out:
                error = f"已超过 {int(timeout)} 秒上限，被强制终止"
            elif auth is not None and auth.logged_in is False:
                error = auth.detail or "扫码未完成（超时或已取消）"
            else:
                error = f"`{_hint(display)}` 退出码 {outcome.returncode}"
        # 与 `GET /runtimes/{id}/auth` 的响应同构：前端把 `supported` 当必填字段，
        # 登录结束时也要一起回，否则类型契约对不上。
        auth_payload = {"supported": True, **auth.to_dict()} if auth is not None else None
        yield _event(
            "finished",
            runtime_id=definition.id,
            action="login",
            ok=ok,
            status="ok" if ok else "error",
            exit_code=outcome.returncode,
            auth=auth_payload,
            error=error,
        )
        return

    # 安装 / 更新 / 卸载都会动本机那份可执行文件，所以这里顺手重测一遍缓存
    # （探测在后台线程里跑，不占用事件循环），并用新结论收尾。
    status = await availability_service.refresh_definition(definition)
    ok = outcome.returncode == 0 and not outcome.timed_out
    # 装完 / 更新完立刻播种内核自带技能：多数内核的内置技能要「首次运行」才落盘，
    # 不补这一步，「技能」页在用户装完内核后仍只有市场技能。
    # 卸载后不播种（没什么可播的），失败也只记一条警告，绝不改变安装结论。
    if ok and action in ("install", "update") and status.installed:
        try:
            actions = await asyncio.to_thread(definition.seed_skills)
            if actions:
                yield _event(
                    "output",
                    runtime_id=definition.id,
                    text="已播种内核内置技能：" + "；".join(actions),
                    level="info",
                )
        except Exception:  # noqa: BLE001 - 播种失败不影响安装结果
            yield _event(
                "output",
                runtime_id=definition.id,
                text="内核内置技能播种失败，可在「技能」页点击重新扫描重试",
                level="warn",
            )
    yield _event(
        "finished",
        runtime_id=definition.id,
        action=action,
        ok=ok,
        status="ok" if ok else "error",
        exit_code=outcome.returncode,
        installed=status.installed,
        version=status.version,
        path=status.detail if status.installed else None,
        error=None if ok else f"`{_hint(display)}` 退出码 {outcome.returncode}",
    )


async def latest_version(definition: AgentDefinition) -> dict[str, Any]:
    """运行「查询最新版本」命令，返回远端可用的最新版本号。

    只是给界面「检查更新」按钮用的元数据探测，任何失败都如实回报，绝不抛出。
    """
    provisioner: RuntimeProvisioner | None = definition.provisioner
    if provisioner is None or not provisioner.latest_argv:
        return {"supported": False, "latest": None, "command": None, "error": "该运行时不支持查询最新版本"}

    argv = provisioner.latest_argv
    env = await _provision_env()
    command = _display(_redact(argv, env))
    try:
        process = await asyncio.create_subprocess_exec(
            # Windows 上 npm 分发的内核是 .cmd 垫片，CreateProcess 不能直接执行。
            *_spawn_argv(argv),
            cwd=_workdir(),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return {
            "supported": True,
            "latest": None,
            "command": command,
            "error": _missing_command_error(argv[0], "查询最新版本", provisioner.manager),
        }
    except PermissionError as exc:
        return {"supported": True, "latest": None, "command": command, "error": f"无法执行 `{argv[0]}`：{exc}"}

    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=_LATEST_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(process.wait(), timeout=5)
        return {"supported": True, "latest": None, "command": command, "error": "查询超时"}

    text = (out or b"").decode("utf-8", "replace").strip()
    latest = next((line.strip() for line in reversed(text.splitlines()) if line.strip()), None)
    ok = process.returncode == 0 and latest is not None
    return {
        "supported": True,
        "latest": latest if ok else None,
        "command": command,
        "error": None if ok else (text[-400:] or f"查询命令退出码 {process.returncode}"),
    }


# ---------------------------------------------------------------------------
# 后台任务跟踪：安装 / 更新 / 卸载在后台执行，客户端断线后可重连续看进度
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _ProvisionLogLine:
    """一条安装日志行。"""

    text: str
    level: str | None = None


@dataclass(slots=True)
class _ProvisionQr:
    """一次登录动作产出的二维码（data URL）。

    和日志行放在同一条订阅队列里，重连时能把二维码补发回去；
    不落日志、不落库 —— 二维码是短期凭据，扫完就没用了。
    """

    data_url: str


@dataclass(slots=True)
class _ProvisionInput:
    """一次登录动作请求用户输入的提示（``INPUT_PROMPT=`` 的翻译结果）。

    与二维码同理放在订阅队列里，重连后界面仍能看到输入框，不必重新发起登录。
    提示文本里不含凭据，落日志是安全的。
    """

    prompt: str


class ProvisionJob:
    """一次安装 / 更新 / 卸载任务的状态，支持断线重连。

    后台任务把输出行追加到 ``lines`` 并推送给所有订阅者；
    客户端断线后可重新订阅，先收已有行、再实时收新行。
    """

    def __init__(self, runtime_id: str, name: str, action: Action) -> None:
        self.runtime_id = runtime_id
        self.name = name
        self.action = action
        self.started_at = time.time()
        self.status: str = "running"
        self.error: str | None = None
        #: 登录动作结束时的登录态（``AuthStatus.to_dict()``）；其它动作恒为 None。
        self.auth: dict[str, Any] | None = None
        #: 双向登录的输入通道（OAuth 授权码这类需要贴回文本的内核才用得上）。
        self.login_input = LoginInputChannel()
        self.lines: list[_ProvisionLogLine | _ProvisionQr | _ProvisionInput] = []
        self._subscribers: list[asyncio.Queue] = []
        self._done = asyncio.Event()

    def add_line(self, text: str, level: str | None = None) -> None:
        line = _ProvisionLogLine(text=text, level=level)
        self.lines.append(line)
        for queue in self._subscribers:
            queue.put_nowait(line)

    def add_qr(self, data_url: str) -> None:
        item = _ProvisionQr(data_url=data_url)
        self.lines.append(item)
        for queue in self._subscribers:
            queue.put_nowait(item)

    def add_input(self, prompt: str) -> None:
        item = _ProvisionInput(prompt=prompt)
        self.lines.append(item)
        for queue in self._subscribers:
            queue.put_nowait(item)

    async def send_input(self, text: str) -> bool:
        """把界面提交的文本写进登录进程；进程已结束或不收输入时返回 ``False``。"""
        return await self.login_input.send(text)

    def finish(
        self,
        status: str,
        error: str | None = None,
        auth: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.error = error
        if auth is not None:
            self.auth = auth
        self._done.set()
        for queue in self._subscribers:
            queue.put_nowait(None)

    async def subscribe(self) -> AsyncIterator[_ProvisionLogLine | _ProvisionQr | _ProvisionInput | None]:
        """订阅输出行：先发已有行，再实时发新行，最后发 None 表示结束。"""
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(queue)
        try:
            n = len(self.lines)
            for i in range(n):
                yield self.lines[i]
            if self._done.is_set():
                yield None
                return
            while True:
                item = await queue.get()
                yield item
                if item is None:
                    break
        finally:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(queue)

    def snapshot(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "name": self.name,
            "action": self.action,
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at,
        }


class _ProvisionJobRegistry:
    """按内核 id 跟踪进行中 / 刚完成的安装任务。"""

    def __init__(self) -> None:
        self._jobs: dict[str, ProvisionJob] = {}

    def get(self, runtime_id: str) -> ProvisionJob | None:
        return self._jobs.get(runtime_id)

    def start(self, runtime_id: str, name: str, action: Action) -> ProvisionJob:
        job = ProvisionJob(runtime_id=runtime_id, name=name, action=action)
        self._jobs[runtime_id] = job
        return job

    def clear(self, runtime_id: str) -> None:
        self._jobs.pop(runtime_id, None)

    def list(self) -> list[dict[str, Any]]:
        return [job.snapshot() for job in self._jobs.values()]


job_registry = _ProvisionJobRegistry()


async def run_provision_background(definition: AgentDefinition, action: Action) -> ProvisionJob:
    """在后台启动一次安装 / 更新 / 卸载，返回可订阅的任务对象。

    后台任务与 SSE 连接解耦：客户端断线只是关掉订阅者，子进程继续跑，
    客户端重连后能从头看到全部输出。
    """
    job = job_registry.start(definition.id, definition.name, action)
    asyncio.create_task(_execute_provision(definition, action, job))
    return job


async def _execute_provision(
    definition: AgentDefinition, action: Action, job: ProvisionJob
) -> None:
    """执行安装任务，将输出写入 job 对象。"""
    try:
        async for event in stream_provision(definition, action, login_input=job.login_input):
            event_type = event.get("type", "")
            if event_type == "provision.started":
                command = event.get("command")
                if command:
                    job.add_line(f"执行命令：{command}")
            elif event_type == "provision.output":
                job.add_line(event.get("text", ""), event.get("level"))
            elif event_type == "provision.qr":
                data_url = event.get("data_url")
                if data_url:
                    job.add_qr(str(data_url))
            elif event_type == "provision.input":
                prompt = event.get("prompt")
                if prompt:
                    job.add_input(str(prompt))
            elif event_type == "provision.finished":
                job.finish(
                    "ok" if event.get("ok") else "error",
                    event.get("error"),
                    event.get("auth"),
                )
    except Exception as exc:
        job.finish("error", str(exc))


async def stream_job(job: ProvisionJob) -> AsyncIterator[dict[str, Any]]:
    """订阅一个任务的输出，产出 SSE 事件（用于断线重连）。"""
    yield _event("started", runtime_id=job.runtime_id, action=job.action, command=None)
    async for item in job.subscribe():
        if item is None:
            break
        if isinstance(item, _ProvisionQr):
            yield _event("qr", runtime_id=job.runtime_id, data_url=item.data_url)
        elif isinstance(item, _ProvisionInput):
            yield _event("input", runtime_id=job.runtime_id, prompt=item.prompt)
        else:
            yield _event("output", runtime_id=job.runtime_id, text=item.text, level=item.level)
    yield _event(
        "finished",
        runtime_id=job.runtime_id,
        action=job.action,
        ok=job.status == "ok",
        status=job.status,
        error=job.error,
        auth=job.auth,
    )
