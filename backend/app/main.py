"""FastAPI 应用入口。"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, collab
from .agents import all_agents
from .im import im_manager
from .api import api_router
from .bootstrap import (
    active_admin,
    backfill_ownership,
    ensure_default_agent,
    seed_if_empty,
    seed_initial_admin,
)
from .db import SessionLocal, init_db
from .runtime import availability as availability_service
from .runtime import mcp as mcp_service
from .runtime import skills as skill_service
from .settings import get_settings
from .transport.gzip import SSEExemptGZipMiddleware

logger = logging.getLogger("coda")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# httpx 在 INFO 级别会把**完整请求 URL** 打进日志。Telegram 的 bot token 在路径里
# （``/bot<id>:<token>/``）、钉钉 / 飞书的凭据曾以 query 形式出现，因此把它的日志压到
# WARNING：正常请求不再回显 URL，真正的错误仍会由适配器产出可读的 error 状态。
logging.getLogger("httpx").setLevel(logging.WARNING)

FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"

#: 少数静态资源的 MIME 需要显式指定：宿主机不一定装了完整的 mime 表（Python 3.11 的
#: ``mimetypes`` 在部分发行版上查不到 WebP），否则会按 ``text/plain`` 下发，浏览器开了
#: ``nosniff`` 就画不出图。内置头像包全是 WebP。
STATIC_MEDIA_TYPES = {".webp": "image/webp"}


async def _start_im() -> None:
    """启动时把已启用的 IM 连接接上；单条连不上只记状态，不拦服务启动。"""
    try:
        await im_manager.start_all()
    except Exception:  # noqa: BLE001 - IM 是旁路，不能因为连不上就起不来服务
        logger.warning("IM 连接启动失败，可在「设置 → IM」里重试", exc_info=True)


async def _scan_skills(session) -> None:
    """启动时把各内核的技能同步进统一目录；失败不影响服务启动。

    先让各内核把自己的**内置技能**落盘（:meth:`AgentDefinition.seed_skills`），
    再统一编目 —— 否则内核刚装好、还没人跑过它时，「技能」页只有市场技能，
    看不到内核自带的那批。播种走线程（会起子进程），绝不阻塞事件循环。
    """
    for definition in all_agents():
        try:
            actions = await asyncio.to_thread(definition.seed_skills)
            if actions:
                logger.info("内核 %s 播种内置技能：%s", definition.id, "；".join(actions))
        except Exception:  # noqa: BLE001 - 播种是尽力而为，失败只少几个技能
            logger.warning("内核 %s 播种内置技能失败", definition.id, exc_info=True)
    try:
        result = await skill_service.rescan(session)
        summary = (
            f"技能编目完成：新增 {result['added']}、更新 {result['updated']}、"
            f"清理已消失 {result['removed']}、解除分配 {result['unassigned']}、"
            f"保留失效 {result['missing']}、共 {result['total']}"
        )
        logger.info("%s", summary)
    except Exception:  # noqa: BLE001 - 界面里还有「重新扫描」按钮可以重试
        logger.warning("技能编目失败，可在「技能」页点击重新扫描重试", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_db()
    async with SessionLocal() as session:
        await seed_if_empty(session, all_agents())
        # 老库升级上来时可能一颗星都没有：补一次，保证「新建对话」直接可用
        await ensure_default_agent(session)
        # 多用户系统：首启按环境变量建首任管理员，并把老库无归属的存量数据回填给它。
        # 已有用户 / 已有管理员时也要检查一次：鉴权关闭期间创建的行可能是无归属的。
        admin = await seed_initial_admin(session) or await active_admin(session)
        if admin is not None:
            await backfill_ownership(session, admin)
        # 内置 MCP 服务器：每个用户各一条「图片生成」「系统管理」，开箱即用；
        # 业务参数由用户在「MCP」页填写。
        builtin_mcp = await mcp_service.ensure_builtin_servers_for_all(session)
        if builtin_mcp:
            logger.info("内置 MCP 播种完成：新增 %s 个", builtin_mcp)
        await _scan_skills(session)
        pinned = await collab.backfill_run_graphs(session)
        if pinned:
            logger.info("协作：%s 次老运行补上了编排图快照", pinned)
        recovered = await collab.recover_interrupted(session)
        if recovered:
            logger.info("协作：%s 次运行因重启被标记为 interrupted", recovered)
    logger.info("%s %s 已就绪 — 数据目录 %s", settings.app_name, __version__, settings.data_dir)
    # 运行时可用性（内核装没装、什么版本）是缓存 + 定时检测的，绝不放在请求路径上：
    # 探测要起一次 `内核 --version` 子进程，同步跑会冻住整个事件循环。
    detector = asyncio.create_task(availability_service.scheduler())
    # IM 连接：只连「启用了、凭据齐全」的那些；连不上不影响服务启动（状态显示在设置页）。
    im_task = asyncio.create_task(_start_im())
    try:
        yield
    finally:
        detector.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await detector
        await im_manager.stop_all()
        if not im_task.done():
            im_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await im_task


app = FastAPI(
    title="Coda 多智能体控制台",
    version=__version__,
    description="多智能体对话控制台：把 Claude Code、Codex、Gemini CLI 等 "
                "内核配成智能体，给它们挑选网关、模型与人格后进行对话。",
    lifespan=lifespan,
)

_settings = get_settings()
# gzip 放在 CORS 内侧：先压缩再补 CORS 头，浏览器才能正确解压跨域响应。
# 压缩只对普通响应生效：大 JSON（协作详情等）从几十 MB 裸传降到约 1/3。
# SSE（text/event-stream）由 SSEExemptGZipMiddleware 显式豁免：starlette 的
# GZipResponder 用 zlib 的 Z_NO_FLUSH 写流，中间分块取不到数据，事件、心跳与
# 打字机 delta 会被攒到响应收尾才下发；豁免后消息原样逐块透传，实时到达。
app.add_middleware(SSEExemptGZipMiddleware, minimum_size=1024, compresslevel=6)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.exception_handler(Exception)
async def unhandled(request, exc):  # pragma: no cover - 兜底保护
    logger.exception("未捕获的异常：%s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": str(exc)})


def resolve_spa_target(dist: Path, full_path: str) -> Path:
    """把前端深链解析到真实存在的 HTML 入口。

    桌面端（`/`）与移动端（`/m/`）是两条物理独立的链接，各有自己的构建入口：
    静态文件（构建产物、manifest、Service Worker）优先原样返回，其余路径回落到
    对应入口的 index.html，交给前端路由接管。
    """
    if full_path:
        candidate = dist / full_path
        if candidate.is_file():
            return candidate
    if full_path == "m" or full_path.startswith("m/"):
        mobile_entry = dist / "m" / "index.html"
        if mobile_entry.is_file():
            return mobile_entry
    return dist / "index.html"


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> FileResponse:
        """前端深链交给入口 HTML；但 `/api/` 下的漏网请求必须显式报 404。

        否则「不存在的接口」会拿到 200 + index.html（HTML）：前端 `response.json()`
        解析失败，WebKit 抛出的还是一句和业务毫无关系的
        "The string did not match the expected pattern."，极难排查。
        明确回一个 JSON 404，客户端就能直接告诉你「接口不存在」。
        """
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail=f"接口不存在：/{full_path}")
        # 外壳（入口 HTML / sw.js / manifest）每次都回源校验：PWA 装到主屏后不会手动刷新，
        # 让浏览器或中间代理直接命中旧外壳，新构建就永远「看起来没生效」。
        # 带哈希的构建产物走 /assets 的静态挂载，不在这里，可以继续长缓存。
        target = resolve_spa_target(FRONTEND_DIST, full_path)
        return FileResponse(
            target,
            media_type=STATIC_MEDIA_TYPES.get(target.suffix.lower()),
            headers={"Cache-Control": "no-cache"},
        )
