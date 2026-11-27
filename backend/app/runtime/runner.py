"""执行层：把 :class:`Invocation` 或 API 内核转换成规范化的 AgentEvent。"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import AsyncIterator, Callable

from ..agents import AgentContext, AgentDefinition, AgentRun, Runtime
from ..agents.events import AgentEvent, error, log, run_finished, run_started
from ..credentials import scrub, scrub_event
from ..egress import proxy_env_for_subprocess
from ..settings import get_settings
from .manager import ActiveRun
from .processes import kill_tree_async
from .trace import RawCollector


#: 读取子进程输出时每次取回的字节数。
_STREAM_CHUNK_SIZE = 1 << 16

#: 单行输出的内存上限。内核（如 Claude Code）会把整段代码或工具结果塞进一行 JSONL，
#: 远超 :class:`asyncio.StreamReader` 默认的 64 KiB，因此这里自己拆行并把水位放宽到 64 MiB；
#: 真越界时丢弃该行剩余内容而不是让整轮运行崩掉（铁律 10：长任务不能因一处噪声中断）。
_MAX_LINE_BYTES = 64 << 20

#: 子进程退出后，再等多久把管道里的残余输出读完就收工。
#: 内核可能留下**握着 stdout 的后台孙进程**：Codex 会拉起
#: ``git ls-remote https://github.com/openai/plugins.git`` 刷新插件市场，网络不通时它会挂住，
#: 于是 ``readline`` 永远等不到 EOF —— 不能因为一个背景任务把整轮运行卡在收尾（铁律 10）。
#: 子进程退出时该写的都已经进了管道缓冲，这里只需要一点宽限收尾。
_EXIT_DRAIN_SECONDS = 0.5

#: 读 stdout 的轮询间隔：进程在跑时 0.5 秒一看（兼顾取消 / 超时的响应），
#: 已经退出就收得更紧，好在残余读完后立刻收工。
_STREAM_POLL_SECONDS = 0.5
_EXIT_POLL_SECONDS = 0.2

#: 收尾时等 stderr 读完的宽限。后台孙进程同样继承 stderr，不能为等它 EOF 再拖几秒；
#: 正常退出（`returncode == 0`）不需要 stderr，直接放行。
_EXIT_STDERR_SECONDS = 0.5

#: 关闭 stdin 之后，等内核自己退出的上限。常驻型内核（如 Codex app-server）看到 EOF 会正常收工；
#: 万一它不理会，也不能让整轮运行永远挂在 `process.wait()` 上（铁律 10）。
_EXIT_WAIT_SECONDS = 5.0

#: 写 stdin 的等待上限：内核若不读 stdin（管道塞满或卡住），不能把整轮运行挂死（铁律 10）。
_STDIN_DRAIN_SECONDS = 5.0


def _spawn_argv(
    argv: list[str],
    *,
    os_name: str | None = None,
    resolver: Callable[[str], str | None] | None = None,
) -> list[str]:
    """把 ``argv`` 整理成当前平台能真正启动的形态。

    Windows 上有个容易踩的坑：npm / pipx 生成的 ``claude.cmd`` / ``codex.cmd`` 垫片，
    ``CreateProcess`` **不能**直接执行（它只认可执行文件），必须交给
    ``cmd.exe /d /s /c`` 解释；而 ``shutil.which("claude")`` 返回的就是
    ``...\\claude.cmd``，原样丢给 ``create_subprocess_exec`` 会以
    「不是有效的应用程序」失败 —— 桌面版上所有 npm 分发内核都会中招。
    POSIX 不需要这层处理：exec 自己按 PATH 找，原样返回。

    ``os_name`` / ``resolver`` 只为单测注入（在 Linux 上验证 Windows 分支），
    生产调用不传。
    """
    if (os_name or os.name) != "nt" or not argv:
        return argv
    finder = resolver or shutil.which
    resolved = finder(argv[0])
    if not resolved:
        return argv
    if str(resolved).lower().endswith((".cmd", ".bat")):
        return [
            os.environ.get("COMSPEC", "cmd.exe"),
            "/d",
            "/s",
            "/c",
            subprocess.list2cmdline(argv),
        ]
    return [str(resolved), *argv[1:]]


def _run_secrets(ctx: AgentContext) -> list[str]:
    """本次运行要从事件文本里擦掉的敏感值：智能体的凭据 + 网关 API 密钥。

    两者都可能被子进程回显（stderr、工具输出），因此都要过 :func:`scrub_event`，
    否则会经 log / error 事件进 SSE 与数据库（铁律 6：明文密钥禁止进日志 / SSE payload）。
    """
    secrets = list(ctx.credentials.values()) if ctx.credentials else []
    if ctx.api_key:
        secrets.append(ctx.api_key)
    return secrets


class _StreamLines:
    """按行切分子进程输出，单行长度不受 ``StreamReader`` 默认 64 KiB 缓冲上限约束。

    ``StreamReader.readline()`` 在单行超过缓冲区上限时会抛 ``ValueError``
    （消息即「Separator is found, but chunk is longer than limit」），一次工具调用就能踩中，
    所以框架层必须自己拆行，不能把内核输出的行长当成已知量。
    """

    __slots__ = ("_buffer", "_stream", "overflowed")

    def __init__(self, stream: asyncio.StreamReader) -> None:
        self._stream = stream
        self._buffer = bytearray()
        #: 上一次读取是否因单行过大丢弃了内容；调用方据此产出 log 事件，不静默吞掉。
        self.overflowed = False

    async def readline(self) -> bytes | None:
        """返回一行（不含换行符）；流已结束且无残留内容时返回 ``None``。"""
        while True:
            index = self._buffer.find(b"\n")
            if index >= 0:
                line = bytes(self._buffer[:index])
                del self._buffer[: index + 1]
                return line
            if len(self._buffer) >= _MAX_LINE_BYTES:
                await self._drop_overlong_line()
                continue
            chunk = await self._stream.read(_STREAM_CHUNK_SIZE)
            if not chunk:
                if not self._buffer:
                    return None
                line = bytes(self._buffer)
                self._buffer.clear()
                return line
            self._buffer.extend(chunk)

    async def _drop_overlong_line(self) -> None:
        """丢弃超长行的剩余内容（读到下一处换行），保证内存占用有上限。"""
        self._buffer.clear()
        self.overflowed = True
        while True:
            chunk = await self._stream.read(_STREAM_CHUNK_SIZE)
            if not chunk:
                return
            index = chunk.find(b"\n")
            if index >= 0:
                self._buffer.extend(chunk[index + 1 :])
                return

    def take_leftover(self) -> bytes:
        """取出缓冲区里还没被换行收尾的残余内容（提前收工前调用，避免丢掉最后一行）。"""
        if not self._buffer:
            return b""
        line = bytes(self._buffer)
        self._buffer.clear()
        return line


def resolve_timeout_limit(run: ActiveRun) -> int | None:
    """这次运行的时限（秒）；``None`` 表示不限时。

    优先级：``run.timeout_seconds``（协作域的单步限时）→ 全局 ``run_timeout_seconds``。
    ``run.timeout_seconds`` 为 0 表示「这次没指定」，交给全局默认；全局默认为空即不限时——
    普通对话本就不该被一个平台级硬时限掐断，需要护栏的部署自行配置。

    非正数一律视为不限时：0 秒的时限没有意义，若当成真值会让运行在启动瞬间就被判超时
    （``deadline = now``），这里直接挡掉，别让一个误填的 0 变成「一开跑就失败」。
    """
    if run.timeout_seconds:
        limit = run.timeout_seconds
    else:
        limit = get_settings().run_timeout_seconds
    if limit is None or limit <= 0:
        return None
    return limit


async def stream_run(
    definition: AgentDefinition,
    ctx: AgentContext,
    run: ActiveRun,
) -> AsyncIterator[AgentEvent]:
    """按 runtime 分发到 CLI 或 API 的执行路径。

    ``run.timeout_seconds``（协作域的「单步限时」）为正时覆盖全局 ``run_timeout_seconds``；
    0 表示沿用全局默认（默认为空 = 不限时）；负数表示本次不限时。
    """
    yield run_started(run.id)
    limit = resolve_timeout_limit(run)
    deadline = None if limit is None else asyncio.get_event_loop().time() + limit
    if definition.runtime is Runtime.API:
        timed_out = False
        async for event in definition.stream(ctx):
            if run.cancel_event.is_set():
                break
            if deadline is not None and asyncio.get_event_loop().time() > deadline:
                timed_out = True
                yield error(f"运行已超过 {limit} 秒上限，被强制终止")
                break
            yield scrub_event(event, _run_secrets(ctx))
        if timed_out:
            yield run_finished("error")
        else:
            yield run_finished("cancelled" if run.cancel_event.is_set() else "ok")
        return
    async for event in stream_cli_run(definition, ctx, run, timeout_seconds=limit):
        yield event


async def stream_cli_run(
    definition: AgentDefinition,
    ctx: AgentContext,
    run: ActiveRun,
    *,
    timeout_seconds: int | None = None,
) -> AsyncIterator[AgentEvent]:
    """执行一次 CLI 内核运行。

    ``timeout_seconds`` 为正时按它限死这次运行；``None`` 或负数表示不限时。
    """
    #: None 表示不限时
    deadline: float | None = (
        None
        if not timeout_seconds or timeout_seconds < 0
        else asyncio.get_event_loop().time() + timeout_seconds
    )
    agent_run: AgentRun = definition.create_run(ctx)
    trace = ctx.trace
    #: 旁路累积 stdout 原始行，运行结束/取消时作为一条 response 落轨迹。
    raw_lines = RawCollector()
    try:
        invocation = agent_run.build_invocation()
    except Exception as exc:  # noqa: BLE001 - 把配置错误显式暴露给用户
        yield error(f"无法构建启动命令：{exc}")
        yield run_finished("error")
        return

    run_dir = Path(tempfile.mkdtemp(prefix=f"coda-{run.id}-"))
    for relative, content in (invocation.files or {}).items():
        target = run_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    cwd = invocation.cwd or ctx.workspace_dir or os.getcwd()
    Path(cwd).mkdir(parents=True, exist_ok=True)

    # 凭据按环境变量注入（值绝不进日志/事件）；适配器/网关给的同名变量优先级更高，
    # 网关始终是模型密钥的唯一来源。
    env = {**os.environ, **proxy_env_for_subprocess(), **ctx.credentials, **invocation.env}
    #: 用户安装脚本常把可执行文件放到 ~/.local/bin，服务进程的 PATH 可能不含该目录，
    #: 这里补上，让 hermes 等用户安装的 CLI 内核能被找到。
    home = env.get("HOME", "")
    if home:
        local_bin = os.path.join(home, ".local", "bin")
        path = env.get("PATH", "")
        if local_bin not in path:
            # 分隔符必须走 os.pathsep：Windows 上是分号，写死冒号会把整条 PATH
            # 粘成一条，内核连同 node 都找不到（路径里出现「C:\x:C:\y」这种）。
            env["PATH"] = f"{local_bin}{os.pathsep}{path}" if path else local_bin
    env = {k: v.replace("{{RUN_DIR}}", str(run_dir)) for k, v in env.items()}

    executable = shutil.which(invocation.argv[0]) if invocation.argv else None
    if executable is None:
        yield log(f"正在启动 {invocation.argv[0]}", "info")
    preview = " ".join(_redact(invocation.argv, env, *_run_secrets(ctx)))
    if len(preview) > 400:
        preview = preview[:400] + " …"
    yield log(preview, "command")

    async def trace_response(exit_code: int | None, stderr_text: str = "", error_text: str | None = None) -> None:
        """把一次子进程运行的结果（原始 stdout 行 + 退出码）落成一条 response。"""
        if trace is None:
            return
        secrets = _run_secrets(ctx)
        payload: dict = {
            "transport": "cli",
            "granularity": "run",
            "raw_lines": raw_lines.lines(),
            "exit_code": exit_code,
        }
        tail = scrub(stderr_text[-4000:], secrets) if stderr_text else None
        if tail:
            payload["stderr_tail"] = tail
        if error_text:
            payload["error"] = error_text
        await trace.response(payload, error=error_text, truncated=raw_lines.truncated)

    # 启动前先记一条 request：即使子进程起不来，也能回看真实启动参数（不是模型 HTTP 请求体）。
    if trace is not None:
        await trace.request(
            {
                "transport": "cli",
                "invocation": {
                    "argv": _redact(invocation.argv, env, *_run_secrets(ctx)),
                    "stdin": invocation.stdin,
                    "env_keys": sorted({*invocation.env, *(ctx.credentials or {})}),
                    "files": sorted((invocation.files or {}).keys()),
                    "cwd": str(cwd),
                },
            },
            immediate=True,
        )

    try:
        process = await asyncio.create_subprocess_exec(
            # Windows 上 .cmd 垫片要经 cmd.exe 启动（npm 分发内核的默认形态）
            *_spawn_argv(invocation.argv),
            cwd=str(cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE if invocation.stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        message = f"`{invocation.argv[0]}` 未安装或不在 PATH 中"
        await trace_response(None, error_text=message)
        yield error(message)
        yield run_finished("error")
        return
    except PermissionError as exc:
        message = f"无法执行 `{invocation.argv[0]}`：{exc}"
        await trace_response(None, error_text=message)
        yield error(message)
        yield run_finished("error")
        return

    run.process = process

    async def write_stdin(payload: str) -> bool:
        """把一行写进内核 stdin；管道已断或写不动返回 False（内核已退出 / 不读了）。"""
        if process.stdin is None or process.stdin.is_closing():
            return False
        try:
            process.stdin.write(payload.encode())
            await asyncio.wait_for(process.stdin.drain(), timeout=_STDIN_DRAIN_SECONDS)
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False
        except asyncio.TimeoutError:
            # 内核不再读 stdin（管道塞满 / 卡住）：丢掉还没写出去的内容、别再投递，
            # 剩下的交给收尾逻辑（否则传输层会在管道断开时抛一颗没人接的异常）。
            transport = getattr(process.stdin, "transport", None)
            if transport is not None:
                with contextlib.suppress(OSError, RuntimeError):
                    transport.abort()
            return False

    def close_stdin() -> None:
        """尽力关闭 stdin（内核可能已经退出）；关不掉也不该影响收尾。"""
        if process.stdin is None or process.stdin.is_closing():
            return
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            process.stdin.close()

    if invocation.stdin is not None:
        wrote = await write_stdin(invocation.stdin)
        if wrote and not invocation.stdin_hold:
            close_stdin()

    stderr_chunks: list[str] = []
    assert process.stderr is not None
    stderr_lines = _StreamLines(process.stderr)

    async def drain_stderr() -> None:
        while True:
            line = await stderr_lines.readline()
            if line is None:
                break
            stderr_chunks.append(line.decode("utf-8", "replace") + "\n")

    stderr_task = asyncio.create_task(drain_stderr())
    cancelled = False

    try:
        assert process.stdout is not None
        stdout_lines = _StreamLines(process.stdout)
        drain_deadline: float | None = None
        while True:
            if run.cancel_event.is_set():
                cancelled = True
                await kill_tree_async(process)
                break
            if deadline is not None and asyncio.get_event_loop().time() > deadline:
                await kill_tree_async(process)
                yield error(f"运行已超过 {timeout_seconds} 秒上限，被强制终止")
                break
            try:
                # 进程已退出就缩短轮询间隔，尽快确认残余读完、别把收尾拖成整秒级。
                timeout = (
                    _EXIT_POLL_SECONDS if process.returncode is not None else _STREAM_POLL_SECONDS
                )
                line = await asyncio.wait_for(stdout_lines.readline(), timeout=timeout)
            except asyncio.TimeoutError:
                # 子进程已经退出、却还读不到 EOF：多半是它留下的后台孙进程握着管道不放
                # （见 `_EXIT_DRAIN_SECONDS`）。先把残余读完，再等一小会儿就收工，
                # 不让一个背景任务把整轮运行拖到超时。
                if process.returncode is not None:
                    if drain_deadline is None:
                        drain_deadline = asyncio.get_event_loop().time() + _EXIT_DRAIN_SECONDS
                    elif asyncio.get_event_loop().time() >= drain_deadline:
                        break
                continue
            if stdout_lines.overflowed:
                stdout_lines.overflowed = False
                yield log(
                    f"内核单行输出超过 {_MAX_LINE_BYTES >> 20} MiB，该行内容过长已丢弃",
                    "warn",
                )
            if line is None:
                break
            text_line = line.decode("utf-8", "replace").rstrip("\n")
            raw_lines.add(text_line)
            for event in agent_run.feed(text_line):
                yield scrub_event(event, _run_secrets(ctx))
            # 适配器可能在解析这一行后排出了要写回内核的请求（双向 JSON-RPC：先 initialize，
            # 拿到 thread id 再 turn/start）。运行层统一代写，管道断了就当作内核已收工。
            alive = True
            outbound = agent_run.take_outbound()
            for payload in outbound:
                alive = await write_stdin(payload)
                if not alive:
                    break
            if outbound and trace is not None:
                # 双向 JSON-RPC 的写回也属于本次运行的「请求」：如实记在 request.outbound，
                # 不冒充模型 HTTP 请求体（方案 §10 偏差 1）。
                await trace.request(
                    {"transport": "cli", "outbound": outbound},
                    immediate=True,
                )
            if not alive or agent_run.done:
                break

        leftover = stdout_lines.take_leftover()
        if leftover:
            # 提前收工时可能还剩一条没写换行的尾巴，别丢掉（EOF 路径下这里必为空）。
            left_text = leftover.decode("utf-8", "replace")
            raw_lines.add(left_text)
            for event in agent_run.feed(left_text):
                yield scrub_event(event, _run_secrets(ctx))
        for event in agent_run.finish():
            yield scrub_event(event, _run_secrets(ctx))
        # 常驻内核要收到 EOF 才会退出；先关 stdin，再有上限地等它收工。
        close_stdin()
        try:
            await asyncio.wait_for(process.wait(), timeout=_EXIT_WAIT_SECONDS)
        except asyncio.TimeoutError:
            await kill_tree_async(process)
            await process.wait()
    finally:
        # 适配器可能从内核任务工具归出了一份平台卡片块：复制到运行对象，收尾时兜底应用。
        # 放在 finally 里，取消 / 异常也保留已经解析出的快照；适配器没产出时不要覆盖
        # 运行层已有的值（API 适配器 / 测试可以预置）。
        if agent_run.task_card_block is not None:
            run.task_card_block = agent_run.task_card_block
        close_stdin()
        if process.returncode is None:
            await kill_tree_async(process)
            await process.wait()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(
                stderr_task,
                timeout=0.05 if process.returncode == 0 else _EXIT_STDERR_SECONDS,
            )
        stderr_text = "".join(stderr_chunks).strip()
        shutil.rmtree(run_dir, ignore_errors=True)

    # 运行结束 / 取消：把原始 stdout 行与退出码落成一条 response（子进程内部的 HTTP 不可见）。
    await trace_response(process.returncode, stderr_text)

    if stderr_text and process.returncode not in (0, None):
        yield scrub_event(log(stderr_text[-4000:], "stderr"), _run_secrets(ctx))

    if cancelled or run.cancel_event.is_set():
        yield run_finished("cancelled", exit_code=process.returncode)
    elif process.returncode == 0:
        yield run_finished("ok", exit_code=0)
    else:
        if stderr_text and not agent_run.emitted_text:
            yield scrub_event(error(stderr_text[-800:]), _run_secrets(ctx))
        yield scrub_event(
            run_finished(
                "error",
                exit_code=process.returncode,
                detail=stderr_text[-800:] if stderr_text else None,
            ),
            _run_secrets(ctx),
        )


def _redact(argv: list[str], env: dict[str, str], *extra_secrets: str | None) -> list[str]:
    secrets = {
        value
        for key, value in env.items()
        if value and any(token in key.upper() for token in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    }
    # 适配器可能把密钥直接拼进 argv（如 custom-cli 的 {api_key} 占位符），
    # 此时它不一定出现在 env 里，需要显式补上。
    secrets.update(value for value in extra_secrets if value)
    out = []
    for part in argv:
        masked = part
        for secret in secrets:
            if secret and secret in masked:
                masked = masked.replace(secret, "***")
        out.append(masked)
    return out
