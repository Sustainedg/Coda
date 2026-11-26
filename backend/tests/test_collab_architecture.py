"""协作域的架构守卫：把「物理分层、互不污染」固化下来。

这些断言失败通常意味着有人（很可能是 AI）图省事把协作逻辑塞进了框架层，
或者让协作模块反向依赖了上层。遇到失败请先读 AGENTS.md §1/§2 与
``docs/collaboration-design.md`` §2。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlmodel import SQLModel

from app.agents.events import EventType
from app.collab.protocol import CollabEventType, notice

APP_DIR = Path(__file__).resolve().parent.parent / "app"
COLLAB_DIR = APP_DIR / "collab"

#: 协作域允许依赖的框架模块（其余一律禁止）
COLLAB_ALLOWED_FRAMEWORK = {"agents.events"}
#: 只有这个文件可以碰执行层与适配器注册表
COLLAB_EXECUTION_EDGE = "execution.py"
#: 必须是纯函数（不许碰存储、HTTP、ORM）的协作模块
COLLAB_PURE_MODULES = ("tickets.py", "graph.py", "prompt.py")
PURE_FORBIDDEN_ROOTS = {"sqlalchemy", "sqlmodel", "fastapi", "httpx", "starlette"}

COLLAB_TABLES = {
    "collab_teams",
    "collab_runs",
    "collab_tickets",
    "collab_steps",
    "collab_messages",
}


def _refs(path: Path) -> list[str]:
    """把文件里的 import 解析成 app 包内的点分模块路径。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = list(path.resolve().relative_to(APP_DIR).parts[:-1])
    refs: list[str] = []

    def resolve(level: int, module: str) -> str:
        if level == 0:
            return module[4:] if module.startswith("app.") else module
        base = package[: len(package) - (level - 1)]
        return ".".join([*base, *([module] if module else [])])

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            refs.extend(resolve(0, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            refs.append(resolve(node.level, node.module or ""))
    return refs


def _collab_files() -> list[Path]:
    return sorted(COLLAB_DIR.glob("*.py"))


@pytest.mark.parametrize("path", _collab_files(), ids=lambda p: p.name)
def test_collab_modules_stay_inside_their_layer(path: Path):
    """协作模块不得依赖 api / runtime 与上层的 agents（事件契约除外）。"""
    if path.name == COLLAB_EXECUTION_EDGE:
        pytest.skip("execution.py 是唯一允许接触执行层的边界（见下一个用例）")
    offenders = [
        ref
        for ref in _refs(path)
        if ref == "api"
        or ref.startswith("api.")
        or ref == "runtime"
        or ref.startswith("runtime.")
        or (ref.startswith("agents.") and ref not in COLLAB_ALLOWED_FRAMEWORK)
    ]
    assert offenders == [], (
        f"{path.name} 越层依赖了：{offenders}。协作域只能依赖 agents.events（事件契约）、"
        "models/db/settings、app.attachments（附件工具）与自身模块；"
        "执行层请通过 execution.py 访问。"
    )


def test_execution_is_the_only_door_to_the_runtime():
    """执行层边界必须存在且只有它一处：防止框架知识散落到引擎与路由里。"""
    execution = COLLAB_DIR / COLLAB_EXECUTION_EDGE
    if not execution.exists():
        # M0 只冻结契约，执行边界在 M1 落地（docs/collaboration-design.md §11）
        pytest.skip("execution.py 属于 M1 里程碑")
    refs = _refs(execution)
    assert any(ref.startswith("runtime") for ref in refs), "execution.py 应当调用 runtime 的执行入口"
    for path in _collab_files():
        if path.name == COLLAB_EXECUTION_EDGE or path.name == "protocol.py":
            continue
        assert not any(ref.startswith("runtime") for ref in _refs(path)), (
            f"{path.name} 绕过 execution.py 直接用了 runtime"
        )


def test_framework_layers_do_not_depend_on_collab():
    """框架层不得反向依赖协作域（协作是旁路功能，不是框架的一部分）。"""
    offenders: list[str] = []
    targets = [p for p in (APP_DIR / "api").glob("*.py") if p.name != "__init__.py"]
    targets += list((APP_DIR / "runtime").glob("*.py"))
    targets += list((APP_DIR / "agents").glob("*.py")) + list((APP_DIR / "agents" / "builtin").glob("*.py"))
    for path in sorted(targets):
        for ref in _refs(path):
            if ref == "collab" or ref.startswith("collab."):
                offenders.append(f"{path.relative_to(APP_DIR)} → {ref}")
    assert offenders == [], "框架层出现了对协作域的依赖：" + "；".join(offenders)


@pytest.mark.parametrize("name", COLLAB_PURE_MODULES)
def test_pure_modules_do_not_touch_storage_or_http(name: str):
    path = COLLAB_DIR / name
    if not path.exists():  # prompt.py 在 M1 才出现
        pytest.skip(f"{name} 尚未实现")
    offenders = [
        ref for ref in _refs(path) if ref.split(".")[0] in PURE_FORBIDDEN_ROOTS
    ]
    assert offenders == [], f"{name} 必须是纯函数模块，不得依赖：{offenders}"


def test_collab_tables_are_registered_for_create_all():
    import app.collab.tables  # noqa: F401 - 触发注册

    missing = COLLAB_TABLES - set(SQLModel.metadata.tables)
    assert missing == set(), (
        f"这些协作表没有注册到 SQLModel.metadata：{missing}；"
        "请确认 db.init_db 里 import 了 app.collab.tables"
    )


def test_sse_transport_is_shared_not_owned_by_api():
    """sse 已下沉为通用传输设施：api 下不该再有同名模块，两处用同一份实现。"""
    assert not (APP_DIR / "api" / "sse.py").exists()
    assert (APP_DIR / "transport" / "sse.py").exists()
    assert any(ref == "transport.sse" for ref in _refs(APP_DIR / "api" / "conversations.py"))


def test_collab_envelope_does_not_extend_the_agent_protocol():
    """协作信封是独立的一层：不往 agents/events.py 的 EventType 里加成员。"""
    agent_values = {member.value for member in EventType}
    collab_values = {member.value for member in CollabEventType}
    assert agent_values == {
        "run.started",
        "message.delta",
        "thinking.delta",
        "tool.call",
        "tool.result",
        "usage",
        "context.usage",
        "context.window",
        "context.compacted",
        "session",
        "log",
        "error",
        "run.finished",
    }
    assert agent_values & collab_values == set()
    assert all(value.startswith("collab.") for value in collab_values)
    # 信封必须能安全序列化（SSE 直接吃 dict）
    payload = notice("预算快用完了", "warn").to_dict()
    assert payload["type"] == "collab.notice" and payload["level"] == "warn"
