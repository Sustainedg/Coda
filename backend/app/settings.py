"""应用配置：从环境变量 / .env 读取，并提供可用的本地默认值。"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger("coda")


class DataDirError(RuntimeError):
    """数据目录无法创建 / 写入：消息面向用户，说明路径、原因与可照做的处理方式。"""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CODA_", env_file=".env", extra="ignore")

    app_name: str = "Coda"
    data_dir: Path = BACKEND_ROOT / "data"
    database_url: str | None = None
    secret_key: str | None = None
    #: 服务监听端口（scripts/serve.sh 也读 CODA_PORT）。系统管理 MCP 在没显式配置
    #: ``public_base_url`` 时用它拼本机地址。
    port: int = 8010
    #: 平台对外的访问地址（例如 ``https://coda.example.com``）。留空时按
    #: ``http://127.0.0.1:<port>`` 处理，适合直接跑在本机 / 容器内的 MCP 子进程。
    public_base_url: str | None = None
    workspace_dir: Path | None = None
    cors_origins: str = "*"
    #: 对话域（普通对话）单轮运行的时限（秒）：``None`` 表示**不限时**。
    #: 需要给对话加护栏的部署才显式配置 ``CODA_RUN_TIMEOUT_SECONDS=<秒数>``。
    #: 协作域**不读这个值**：它的每一步都带自己的单步限时（``budget.step_timeout_seconds``，
    #: 默认 36000 秒，0 = 不限），由 ``collab.execution.stream_step()`` 落到 ``ActiveRun`` 上。
    run_timeout_seconds: int | None = None
    #: 安装/更新/卸载智能体运行时（CLI 内核）的单次上限；这类动作本就有明确终点。
    provision_timeout_seconds: int = 900
    #: 网络出口过滤模式：off（不限制）/ block（全部阻断）/ allowlist（只放行白名单）
    egress_mode: str = "off"
    #: 出口白名单（逗号分隔域名），allowlist 模式下生效；网关地址运行时自动加入
    egress_allowed_hosts: str = ""
    #: 自更新来源仓库（owner/repo）；默认官方仓库，私有部署可指到自己的 fork
    update_repo: str | None = None
    #: GitHub API 基址；GitHub Enterprise 部署可换自己的域名
    update_github_api: str | None = None
    #: 需要取回对话工作区的图片 URL 前缀（逗号分隔），例如 MCP 网关图床
    #: ``https://img.example.com``。命中前缀的图片会在助手消息落库前下载到
    #: ``generated-images/`` 并改写为相对路径，让对话历史自包含。
    image_ingest_prefixes: str = ""
    # ---- 多用户系统 -------------------------------------------------------
    #: 是否开启鉴权。默认开启（多用户形态）；空库首启可用 CODA_ADMIN_* 建管理员，
    #: 不配置时第一个注册的账号自动成为管理员。想要单用户开箱即用可显式设为 false。
    #: 关闭时所有接口视为「管理员」调用，不校验令牌。
    auth_enabled: bool = True
    #: 是否开放注册。默认开放；设为 false 时注册接口返回 403，只能由管理员建用户。
    registration_open: bool = True
    #: 首任管理员的登录名（首启建初始管理员用；已存在用户时不生效）
    admin_username: str | None = None
    #: 首任管理员的口令（首启建初始管理员用；已存在用户时不生效）
    admin_password: str | None = None

    @field_validator("run_timeout_seconds", mode="before")
    @classmethod
    def _empty_timeout_means_unlimited(cls, value: object) -> object:
        """``CODA_RUN_TIMEOUT_SECONDS=``（留空）按「不配置」处理。

        默认不限时以后，「把它留空」是运维最自然的写法；而空串对 ``int`` 是解析错误，
        会让服务直接起不来。空值一律折成 ``None``（= 不限时），别把一个空字符串变成启动故障。
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///{self.data_dir / 'coda.db'}"

    @property
    def workspaces_root(self) -> Path:
        return (self.workspace_dir or (self.data_dir / "workspaces")).resolve()

    @property
    def public_base_url_value(self) -> str:
        """系统管理 MCP 自动填写的平台地址（不带 ``/api``，脚本会自己归一）。"""
        value = (self.public_base_url or "").strip().rstrip("/")
        return value or f"http://127.0.0.1:{self.port}"

    @property
    def agent_homes_root(self) -> Path:
        """每个对话一份的内核隔离配置目录（技能「严格仅分配」就物化在这里）。

        放在数据目录里而不是临时目录：内核的会话记录也写在自己的配置目录下，
        每次运行都换目录会导致多轮对话无法续接。
        """
        return (self.data_dir / "agent-homes").resolve()

    @property
    def skill_libraries_root(self) -> Path:
        """用户技能库的根目录（``<用户>/<技能>`` + ``<用户>/.mounts.json``）。

        技能由平台统一托管而不是写进内核的原生技能目录：多用户下必须按用户隔离，
        而内核目录是全机器共享的。一份技能按 slug 只存一份，多个内核通过引用登记共用；
        运行时再按「智能体分配」把技能物化进各自的隔离目录，因此内核照旧只看得到
        分配给它的那几份。
        """
        return (self.data_dir / "skill-libraries").resolve()

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def egress_allowed_hosts_list(self) -> list[str]:
        return [h.strip() for h in self.egress_allowed_hosts.split(",") if h.strip()]

    @property
    def image_ingest_prefix_list(self) -> list[str]:
        return [p.strip() for p in self.image_ingest_prefixes.split(",") if p.strip()]

    def ensure_dirs(self) -> None:
        """创建数据目录布局（幂等；父目录不存在时一并创建）。

        ``mkdir(parents=True, exist_ok=True)`` 等价于 makedirs + exist_ok：目录已存在不报错，
        多层父目录一次建好。失败时抛出 :class:`DataDirError`，带上**具体路径与系统原因**，
        既不静默吞掉，也给出可照做的处理方式（换 ``CODA_DATA_DIR``）。
        """
        for label, path in (
            ("应用数据目录", self.data_dir),
            ("工作目录根", self.workspaces_root),
            ("内核隔离目录根", self.agent_homes_root),
            ("技能库根目录", self.skill_libraries_root),
        ):
            _make_dir(path, label)


def _make_dir(path: Path, label: str) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        # 真实写入探测：Windows 上「目录建得出来、文件写不进去」很常见（受控文件夹访问 /
        # 只读属性 / 安全软件）。只建目录会把问题推迟到写 SQLite 时才炸，那时错误已经
        # 和「数据目录」对不上了。这里当场验证，失败原因随异常一起报给用户。
        probe = path / f".write-probe-{os.getpid()}"
        try:
            probe.write_text("", encoding="utf-8")
        finally:
            try:
                probe.unlink()
            except OSError:
                pass
    except OSError as exc:
        reason = exc.strerror or str(exc)
        raise DataDirError(
            f"无法创建或写入{label} {path}：{reason}。"
            "请检查磁盘空间与目录写入权限，或用 CODA_DATA_DIR 指定一个可写目录。"
        ) from exc


def _fallback_data_dir() -> Path:
    """默认数据目录不可用时的回退目录。

    用 ``expanduser()`` 按当前用户动态解析主目录，不写死任何用户名（不会出现
    ``C:\\Users\\admin\\...`` 这类路径）；Windows / macOS / Linux 都落到各自的家目录下。
    """
    return Path("~/.coda/data").expanduser()


def _prepare_dirs(settings: Settings, *, explicit_data_dir: bool) -> Settings:
    """创建目录；默认路径不可写时回退到用户家目录，显式配置则原样报错。

    只有**用户没有显式设置 ``CODA_DATA_DIR``** 时才回退：显式配置就该被尊重，
    悄悄换目录会让用户以为数据丢了。回退会打印警告，说明实际落点。
    """
    try:
        settings.ensure_dirs()
    except DataDirError:
        if explicit_data_dir:
            raise
        fallback = _fallback_data_dir()
        logger.warning(
            "默认数据目录 %s 不可用，回退到 %s（可用 CODA_DATA_DIR 指定可写目录）",
            settings.data_dir,
            fallback,
        )
        settings.data_dir = fallback
        settings.ensure_dirs()
    return settings


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    # 不能只查 ``os.environ``：``.env`` 里的值由 pydantic-settings 自己读取，不会写进
    # ``os.environ``（见 ``backend/.env.example`` 的 ``CODA_DATA_DIR``）。只看环境变量会把
    # 「在 .env 里显式配置」误判成「没配」→ 悄悄回退到其它目录，用户会以为数据丢了。
    # ``model_fields_set`` 记录「这个字段是否被显式赋值」，环境变量与 .env 两种来源都覆盖。
    explicit_data_dir = "data_dir" in settings.model_fields_set
    return _prepare_dirs(settings, explicit_data_dir=explicit_data_dir)
