from fastapi import APIRouter

from ..collab.api import router as collab_router
from ..im.api import router as im_router
from . import (
    agents,
    agent_types,
    api_keys,
    auth,
    conversations,
    credentials,
    gateways,
    mcp,
    model_gateway,
    runtimes,
    runs,
    update,
    skillhub,
    skills,
    system,
    usage,
    usage_gateway,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(system.router)
api_router.include_router(agent_types.router)
api_router.include_router(agents.router)
api_router.include_router(gateways.router)
api_router.include_router(api_keys.router)
api_router.include_router(credentials.router)
api_router.include_router(skills.router)
api_router.include_router(mcp.router)
api_router.include_router(skillhub.router)
api_router.include_router(runtimes.router)
api_router.include_router(conversations.router)
api_router.include_router(runs.router)
api_router.include_router(usage.router)
# 记录网关：内部转发路由，不出现在公开 OpenAPI（见路由上的 include_in_schema=False）。
api_router.include_router(usage_gateway.router)
# 外部模型网关：外部客户端带 API Key 调 /api/model-gateway/...（同样不进公开文档）。
api_router.include_router(model_gateway.router)
api_router.include_router(update.router)
api_router.include_router(collab_router)
api_router.include_router(im_router)

__all__ = ["api_router"]
