"""协作域的节点级技能 / MCP 临时分配（V0.1.3 之后的「协作专属设定」）。

语义：
- ``NodeSpec.skill_ids`` / ``NodeSpec.mcp_ids`` 为 ``None``（默认）：完全跟随智能体页面的分配；
- 给了列表（含空列表）：以节点为准，只影响协作运行，**不写回**智能体页面的分配。

这里覆盖两层：
1. 契约层：旧图（没有这两个字段）能解析，默认是 None；
2. 执行层：``collab.execution._build_context`` 会把节点的覆盖传给物化（用假物化函数验证调用参数）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.collab.execution import _build_context, _resolve_profile
from app.collab.schemas import NodeSpec
from app.runtime import mcp as mcp_service
from app.runtime import skills as skill_service


# --- 契约层 -------------------------------------------------------------------


def test_node_overrides_default_to_none_for_old_graphs():
    """旧图没有 skill_ids / mcp_ids：解析后是 None（跟随智能体页面），不是空列表。"""
    node = NodeSpec.model_validate(
        {"key": "dev", "agent_profile_id": "a1", "duty": "engineer"}
    )
    assert node.skill_ids is None
    assert node.mcp_ids is None


def test_node_overrides_accept_explicit_lists():
    """显式给了列表就按协作专属分配存（空列表 = 这个协作里一个都不给）。"""
    node = NodeSpec.model_validate(
        {
            "key": "dev",
            "agent_profile_id": "a1",
            "skill_ids": ["claude-code:lib:local:pdf"],
            "mcp_ids": [],
        }
    )
    assert node.skill_ids == ["claude-code:lib:local:pdf"]
    assert node.mcp_ids == []
    # 落库再读回（JSON 往返）：None 仍是 None，列表仍是列表。
    storable = node.model_dump(mode="json")
    assert storable["skill_ids"] == ["claude-code:lib:local:pdf"]
    assert storable["mcp_ids"] == []
    roundtrip = NodeSpec.model_validate(storable)
    assert roundtrip.skill_ids == node.skill_ids
    assert roundtrip.mcp_ids == node.mcp_ids


# --- 执行层 -------------------------------------------------------------------


class _FakeProfile:
    """最小可用的智能体档案：_build_context 只读这些字段。"""

    id = "a1"
    name = "测试智能体"
    system_prompt = None
    #: 多账号槽位：不指定（_build_context 会读它决定是否切换内核登录态）。
    account_id = None


@pytest.mark.asyncio
async def test_build_context_passes_node_overrides_to_materialization(monkeypatch: pytest.MonkeyPatch):
    """节点带了 skill_ids / mcp_ids 时，物化收到的是节点的列表；没带时是 None（走默认）。"""
    import app.collab.execution as execution

    calls: dict[str, list | None] = {}

    async def fake_skill_home(_session, _definition, _profile, _conversation, _ctx, *, scope_id=None, override_ids=None):
        calls["skills"] = override_ids
        return []

    async def fake_mcp_home(_session, _definition, _profile, _conversation, _ctx, *, scope_id=None, override_ids=None):
        calls["mcp"] = override_ids
        return []

    monkeypatch.setattr(execution, "prepare_skill_home", fake_skill_home)
    monkeypatch.setattr(execution, "prepare_mcp_home", fake_mcp_home)

    node = NodeSpec.model_validate(
        {"key": "dev", "agent_profile_id": "a1", "skill_ids": ["s1", "s2"], "mcp_ids": None}
    )
    await _build_context(
        None,
        resolved=execution.ResolvedProfile(
            profile=_FakeProfile(),  # type: ignore[arg-type]
            definition=execution.get_agent("claude-code"),
            options={},
            model=None,
            gateway=None,
            gateway_kind=None,
            api_key=None,
            auth_style=None,
            credentials={},
            cwd="/tmp",
            scope=execution.WorkspaceScope.WRITE,
        ),
        home_scope="run-a1-step",
        prompt="干活",
        node=node,
    )
    assert calls == {"skills": ["s1", "s2"], "mcp": None}, (
        "节点的 skill_ids 要传给技能物化；mcp_ids 为 None 时保持 None（跟随智能体页面）"
    )

    # 没带覆盖的节点：两处都是 None。
    plain = NodeSpec.model_validate({"key": "rev", "agent_profile_id": "a1", "duty": "reviewer"})
    await _build_context(
        None,
        resolved=execution.ResolvedProfile(
            profile=_FakeProfile(),  # type: ignore[arg-type]
            definition=execution.get_agent("claude-code"),
            options={},
            model=None,
            gateway=None,
            gateway_kind=None,
            api_key=None,
            auth_style=None,
            credentials={},
            cwd="/tmp",
            scope=execution.WorkspaceScope.READ,
        ),
        home_scope="run-a1-step",
        prompt="审查",
        node=plain,
    )
    assert calls == {"skills": None, "mcp": None}


def test_runtime_lookup_helpers_accept_override_ids():
    """runtime 侧的查询签名带 override_ids（协作执行层靠它覆盖默认来源）。"""
    import inspect

    skill_params = inspect.signature(skill_service.skills_for_agent).parameters
    assert "override_ids" in skill_params
    mcp_params = inspect.signature(mcp_service.mcp_servers_for_agent).parameters
    assert "override_ids" in mcp_params
    # prepare_*_home 也一样（协作执行层直接调的是它们）。
    assert "override_ids" in inspect.signature(skill_service.prepare_skill_home).parameters
    assert "override_ids" in inspect.signature(mcp_service.prepare_mcp_home).parameters


# --- 节点级覆盖的归属边界（跨用户引用必须在写入与运行时双重拦截）-------------------


async def test_mcp_override_filters_out_another_users_server(client):
    """节点级 MCP 覆盖不能借到别人的服务器：归属不符的行在物化前就被过滤掉。

    否则协作图可以写一个别人的平台通用 MCP id，把它的 env / headers（含密钥）
    解密后写进本次运行的内核配置。
    """
    import app.runtime.mcp as mcp_service
    from app.db import SessionLocal
    from app.models import AgentProfile, McpServer

    async with SessionLocal() as session:
        mine = AgentProfile(name="A 的智能体", type_id="custom-cli", owner_id="u-a")
        theirs = AgentProfile(name="B 的智能体", type_id="custom-cli", owner_id="u-b")
        session.add(mine)
        session.add(theirs)
        await session.commit()
        await session.refresh(mine)

        session.add(McpServer(id="mcp-mine", name="我的服务器", slug="mine", owner_id="u-a"))
        session.add(McpServer(id="mcp-theirs", name="别人的服务器", slug="theirs", owner_id="u-b"))
        await session.commit()

        specs = await mcp_service.mcp_servers_for_agent(
            session, mine, override_ids=["mcp-mine", "mcp-theirs"]
        )
        assert [spec.slug for spec in specs] == ["mine"], (
            "节点级覆盖引用别人的 MCP 服务器时必须被过滤掉，"
            "否则对方 env / headers 的密钥会进入本次运行"
        )


async def test_team_write_rejects_refs_to_other_users_assets(client):
    """协作图引用别人的技能 / MCP：写入时就拒绝（422），不能只靠运行时悄悄过滤。"""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.collab.api import _assert_graph_refs_owned
    from app.collab.schemas import GraphSpec, NodeSpec
    from app.db import SessionLocal
    from app.models import McpServer, Skill

    async with SessionLocal() as session:
        session.add(Skill(id="skill-mine", type_id="claude-code", slug="mine", name="我的技能", owner_id="u-a"))
        session.add(Skill(id="skill-theirs", type_id="claude-code", slug="theirs", name="别人的技能", owner_id="u-b"))
        session.add(McpServer(id="mcp-mine", name="我的服务器", slug="mine", owner_id="u-a"))
        session.add(McpServer(id="mcp-theirs", name="别人的服务器", slug="theirs", owner_id="u-b"))
        await session.commit()

        user = SimpleNamespace(id="u-a", role="user")
        foreign = NodeSpec(
            key="dev", agent_profile_id="a1", skill_ids=["skill-theirs"], mcp_ids=["mcp-theirs"]
        )
        with pytest.raises(HTTPException) as exc:
            await _assert_graph_refs_owned(session, user, GraphSpec(nodes=[foreign]))
        assert exc.value.status_code == 422
        assert "不属于你" in exc.value.detail

        # 换成自己的引用（或共享技能 / 共享 MCP）应当放行。
        own = NodeSpec(key="dev", agent_profile_id="a1", skill_ids=["skill-mine"], mcp_ids=["mcp-mine"])
        await _assert_graph_refs_owned(session, user, GraphSpec(nodes=[own]))
