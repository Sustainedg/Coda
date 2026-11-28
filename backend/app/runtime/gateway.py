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
    #: ``True`` = 平台不注入密钥，由内核用自己已有的登录态（OAuth）直通代理。
    #: 只在强制 ``CODA_RECORDING_GATEWAY=on`` + 官方网关无 API Key 时启用（实验性）。
    passthrough: bool = False


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
    inject_key: bool = True,
    capture_trace: bool = False,
    usage_protocol: str | None = None,
) -> GatewayBinding:
    """注册一次代理目标，返回 ``record`` 模式绑定。

    ``inject`` 鉴权：上游密钥留在服务端内存注册表，子进程只拿一次性代理 token；
    代理 URL 指向本机回环（见 ``recording_gateway.proxy_url_for``）。

    ``inject_key=False``（官方 OAuth passthrough）时，平台**不**下发任何密钥，子进程
    继续用自己已有的登录态发请求，代理只负责转发与记账。
    """
    target = recording_gateway.registry.register(
        run_id=run_id,
        upstream_url=gateway.base_url,
        protocol=gateway.protocol,
        auth_style=gateway.auth_style,
        api_key=decrypt(gateway.api_key_encrypted) if inject_key else None,
        inject_key=inject_key,
        owner_id=owner_id,
        conversation_id=conversation_id,
        agent_type_id=agent_type_id,
        agent_profile_id=agent_profile_id,
        model=model,
        provider=provider,
        run_started_at=run_started_at,
        capture_trace=capture_trace,
        usage_protocol=usage_protocol,
    )
    return GatewayBinding(
        url=recording_gateway.proxy_url_for(target.token),
        # passthrough 时子进程不该拿到代理 token（它会当 API Key 用）；留空由内核自证身份。
        api_key=target.token if inject_key else None,
        auth_style=gateway.auth_style if inject_key else None,
        mode="record",
        proxy_token=target.token,
        upstream_url=target.upstream_url,
        passthrough=not inject_key,
    )


__all__ = ["GatewayBinding", "bind_gateway", "bind_recording"]
