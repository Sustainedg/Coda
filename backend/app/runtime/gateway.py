"""网关 → 一次运行的绑定：注入哪个地址、哪把密钥、用哪种鉴权头。

**官方链路 = 平台不接管路由。** ``kind="official"`` 的行只承载适配器声明的模型清单
（`default_models`），运行时不注入地址与密钥 —— 与「没选网关」完全同义，由内核按自己的
配置 / 环境处理。只有用户自建的 ``kind="custom"`` 网关才会被注入，密钥一律只经
`app/security.py` 解密，禁止出现在日志、事件与响应里。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..agents.base import AgentDefinition
from ..models import Gateway
from ..security import decrypt
from . import recording_gateway


@dataclass(slots=True)
class GatewayBinding:
    """一次运行实际生效的网关三件套（外加记录网关模式与上游信息）。"""

    url: str | None = None
    api_key: str | None = None
    auth_style: str | None = None
    #: ``direct`` = 现状（内核直连上游）；``record`` = 经本机记录网关转发并一手记账。
    mode: str = "direct"
    #: record 模式下的一次性代理 token（子进程只拿它，不拿上游密钥）。
    proxy_token: str | None = None
    #: record 模式下的真实上游地址（脱敏，可落账本）。
    upstream_url: str | None = None


def bind_gateway(definition: AgentDefinition, gateway: Gateway | None) -> GatewayBinding:
    """解析一次运行要注入的网关地址 / 密钥 / 鉴权方式（现状：不接管路由）。"""
    if gateway is None or gateway.kind != "custom":
        # 没选网关，或选的是官方链路：平台不接管路由，回落到适配器读到的本机配置。
        return GatewayBinding(url=definition.resolve_gateway(None))
    return GatewayBinding(
        url=gateway.base_url,
        api_key=decrypt(gateway.api_key_encrypted),
        auth_style=gateway.auth_style,
    )


def bind_recording(
    definition: AgentDefinition,
    gateway: Gateway,
    *,
    run_id: str,
    owner_id: str | None = None,
    conversation_id: str | None = None,
    agent_type_id: str = "",
    agent_profile_id: str | None = None,
    model: str = "",
    provider: str = "",
    run_started_at: datetime | None = None,
) -> GatewayBinding:
    """注册一次代理目标，返回 ``record`` 模式绑定。

    ``inject`` 鉴权：上游密钥留在服务端内存注册表，子进程只拿一次性代理 token；
    代理 URL 指向本机回环（见 ``recording_gateway.proxy_url_for``）。
    """
    target = recording_gateway.registry.register(
        run_id=run_id,
        upstream_url=gateway.base_url,
        protocol=gateway.protocol,
        auth_style=gateway.auth_style,
        api_key=decrypt(gateway.api_key_encrypted),
        inject_key=True,
        owner_id=owner_id,
        conversation_id=conversation_id,
        agent_type_id=agent_type_id,
        agent_profile_id=agent_profile_id,
        model=model,
        provider=provider,
        run_started_at=run_started_at,
    )
    return GatewayBinding(
        url=recording_gateway.proxy_url_for(target.token),
        api_key=target.token,
        auth_style=gateway.auth_style,
        mode="record",
        proxy_token=target.token,
        upstream_url=target.upstream_url,
    )


__all__ = ["GatewayBinding", "bind_gateway", "bind_recording"]
