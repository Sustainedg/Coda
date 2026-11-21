#!/usr/bin/env python3
"""把内核 Logo（`frontend/public/icons/kernels/*`）内嵌进前端包。

用法：python3 scripts/gen_kernel_logos.py

输出：``frontend/src/lib/kernelLogos.ts``（键 = 后端适配器声明的 ``logo`` 路径）。

**为什么要内嵌**：「内核管理」页的图标要在进入页面时立刻显示。图标文件由后端的
SPA 兜底路由返回，带 ``Cache-Control: no-cache`` —— 每次进页面都要把八张图重新
校验一遍（条件请求 + 往返），Windows 桌面端尤其明显：卡片先出来，图标后补半拍。

改成构建期把图形内嵌进前端包，运行期零请求：页面渲染直接命中已经解好码的图形。

**图形本身不归这个脚本管**：`public/icons/kernels/` 下的文件是各适配器声明的品牌图
（手工 / 厂商产物），这里只做「读文件 → 内嵌」的转换，SVG 里的注释一并剥掉以省体积。
改完图形重跑一次脚本即可；`backend/tests/test_frontend_assets.py` 会核对清单与文件一致。

**两种编码**：
- SVG 是文本 —— 存原文（打包时 gzip 能压得很好），前端用 `encodeURIComponent` 拼成 data URL；
- 位图是二进制 —— 存 base64 data URL。
"""
from __future__ import annotations

import base64
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ICON_DIR = REPO_ROOT / "frontend" / "public" / "icons" / "kernels"
OUT_FILE = REPO_ROOT / "frontend" / "src" / "lib" / "kernelLogos.ts"

#: 后端声明的 logo 前缀（`AgentDefinition.logo`，见 `app/agents/base.py`）。
LOGO_PREFIX = "/icons/kernels/"

#: 位图的 MIME（二进制，只能 base64）。
RASTER_MIME = {
    ".png": "image/png",
    ".webp": "image/webp",
}

#: SVG 里的 XML 注释：只给人看，渲染用不到，剥掉省体积。
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def build_entry(path: Path) -> tuple[str, str]:
    """一个图形文件 → （logo 路径, 内嵌内容）。"""
    suffix = path.suffix.lower()
    if suffix == ".svg":
        # SVG 是文本：剥掉注释后原样存，打包时能 gzip。
        text = COMMENT_RE.sub("", path.read_text(encoding="utf-8")).strip()
        return f"{LOGO_PREFIX}{path.name}", text
    mime = RASTER_MIME.get(suffix)
    if mime is None:
        raise ValueError(f"不支持内嵌的图形类型：{path.name}")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"{LOGO_PREFIX}{path.name}", f"data:{mime};base64,{encoded}"


def render_module() -> str:
    """生成 `kernelLogos.ts` 的完整内容（测试用它核对清单是否最新）。"""
    files = sorted(
        path for path in ICON_DIR.iterdir() if path.suffix.lower() in (".svg", *RASTER_MIME)
    )
    lines = [
        "/**",
        " * 内核 Logo 的内嵌资源（**由 `scripts/gen_kernel_logos.py` 生成，别手改**）。",
        " *",
        " * 键是后端适配器声明的 `logo` 路径（`/icons/kernels/xxx.svg`）；",
        " * 值是图形本身 —— SVG 存原文（前端拼成 data URL），位图存 data URL。",
        " * 「内核管理」页渲染图标时直接用这份内嵌资源：没有请求、没有二次解码，",
        " * 进入页面即显示。源文件在 `frontend/public/icons/kernels/`，改完图形重跑脚本。",
        " */",
        "export const KERNEL_LOGO_INLINE: Record<string, string> = {",
    ]
    for path in files:
        logo_path, content = build_entry(path)
        # 单引号：与仓库里其他前端文件的风格一致；反斜杠与换行按 JS 字符串规则转义。
        escaped = content.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        lines.append(f"  '{logo_path}': '{escaped}',")
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(render_module(), encoding="utf-8")
    count = len([p for p in ICON_DIR.iterdir() if p.suffix.lower() in (".svg", *RASTER_MIME)])
    print(f"→ 已写入 {OUT_FILE.relative_to(REPO_ROOT)}（内嵌 {count} 个图形）")


if __name__ == "__main__":
    main()
