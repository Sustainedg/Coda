#!/usr/bin/env python3
"""把头像拼图裁成一张张单人头像，输出到 `frontend/public/avatars/`。

用法：python3 scripts/gen_avatar_pack.py [拼图 …]

源图是若干张多宫格拼图（每格一个角色），文件名与**分类标签**写在 :data:`SOURCES` 里，
默认从仓库根的 `generated-images/` 读（该目录是用户产物，不进仓库）。裁好的图随前端
构建一起发布，智能体「随机生成头像」直接从中抽取——秒出、不花钱、断网也能用。

两类角色（画廊按它分组展示，编号连续、互不重叠）：

* ``person`` —— 3D 黏土风 Q 版人物半身像；
* ``cartoon`` —— 3D 黏土风卡通小生物（毛球、幽灵、星星……）。

**编号一旦发布就不能改**：数据库里 `agent_profiles.avatar_preset` 存的是编号，
新增分类只能往后追加，否则已经选过头像的智能体会集体换脸。

**为什么不按 4×5 等分**：拼图里各格的间距并不均匀，有的角色（厨师、公主）还带道具会
顶到格边。等分会切歪、甚至把邻格的人切进来。这里用**连通域**找出每个角色真实的包围盒，
再以包围盒中心取正方形（略放大留白），保证每张都是居中的单人像。

只依赖 Pillow（项目前端图标生成脚本同款思路：能脚本化的资产就脚本化，不手搓）。
"""
from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = REPO_ROOT / "frontend" / "public" / "avatars"
#: 前端清单：随机生成 / 快选都要知道「包里有哪几张」，静态文件没法在运行时枚举。
MANIFEST = REPO_ROOT / "frontend" / "src" / "lib" / "avatarPack.ts"

#: 拼图源：(相对仓库根的路径, 分类标签)。换一套风格就换这批文件，再跑一次本脚本。
#: 只收「单个角色」的拼图：带道具（盘子 / 剑 / 小动物）的格子会把邻格切进来。
#: **追加新分类只能往列表末尾加**，否则已有编号会整体错位（见模块文档）。
SOURCES: tuple[tuple[str, str], ...] = (
    ("generated-images/image-20261119-154750-180e26.png", "person"),
    ("generated-images/image-20261119-155417-310af9.png", "person"),
    ("generated-images/image-20261119-155726-e64085.png", "person"),
    ("generated-images/image-20261121-084525-cartoon.png", "cartoon"),
)

#: 分类标签 → 画廊里的分组名。顺序即分组在画廊里的顺序。
CATEGORIES: dict[str, str] = {
    "person": "人物",
    "cartoon": "卡通",
}

#: 分类 → 裁切锚点。人物是半身像居中；卡通角色脚下有淡投影，底边对齐角色下缘能把投影挤出画面。
CROP_ANCHOR: dict[str, str] = {
    "person": "center",
    "cartoon": "bottom",
}

#: 输出边长（px）。前端最大按 96px 显示，2 倍屏也够用；再大只是白占体积。
TILE_SIZE = 192
#: 输出格式：WebP（同样清晰度下体积约为 PNG 的 1/9，60 张合计约 0.4 MB）。
#: 头像上传接口同样收 WebP（见 `app/avatars.py`），所以随机生成的头像能原样回传。
OUT_FORMAT = "webp"
WEBP_QUALITY = 88
#: 包围盒外扩比例：留一点呼吸感，别让头发顶到圆框边缘。
PADDING = 1.12
#: 连通域最小像素数（缩略图尺度）：滤掉背景噪点与零星描边。
MIN_COMPONENT = 60
#: 判定「这是角色像素」的灰度阈值：背景是近白的浅灰纸纹，角色整体更深。
DARK_THRESHOLD = 232


def _components(gray: Image.Image) -> list[tuple[int, int, int, int]]:
    """在缩略图上跑连通域，返回每个角色的包围盒（原图坐标）。"""
    width, height = gray.size
    scale = 160 / width
    small = gray.resize((160, round(height * scale)), Image.LANCZOS)
    sw, sh = small.size
    px = small.load()
    mask = [[px[x, y] < DARK_THRESHOLD for x in range(sw)] for y in range(sh)]
    seen = [[False] * sw for _ in range(sh)]
    boxes: list[tuple[int, int, int, int]] = []
    for y in range(sh):
        for x in range(sw):
            if not mask[y][x] or seen[y][x]:
                continue
            queue = deque([(x, y)])
            seen[y][x] = True
            min_x = max_x = x
            min_y = max_y = y
            count = 0
            while queue:
                cx, cy = queue.popleft()
                count += 1
                min_x, max_x = min(min_x, cx), max(max_x, cx)
                min_y, max_y = min(min_y, cy), max(max_y, cy)
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < sw and 0 <= ny < sh and mask[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        queue.append((nx, ny))
            if count >= MIN_COMPONENT:
                boxes.append((min_x, min_y, max_x, max_y))
    # 按「行 → 列」排序，保证输出的编号稳定（重跑不会打乱顺序）。
    boxes.sort(key=lambda b: (round(b[1] * scale / 220), b[0]))
    return [(round(x0 / scale), round(y0 / scale), round(x1 / scale), round(y1 / scale)) for x0, y0, x1, y1 in boxes]


def _paste_on_white(image: Image.Image, left: int, top: int, size: int) -> Image.Image:
    """把源图 ``(left, top)`` 起的正方形区域贴到白画布上（越界处留白，而不是补黑）。"""
    width, height = image.size
    canvas = Image.new("RGB", (size, size), "white")
    l, t = max(0, left), max(0, top)
    r, b = min(width, left + size), min(height, top + size)
    if r > l and b > t:
        canvas.paste(image.crop((l, t, r, b)), (l - left, t - top))
    return canvas


def _square_crop(
    image: Image.Image, box: tuple[int, int, int, int], anchor: str = "center"
) -> Image.Image:
    """以包围盒为中心裁一个正方形，超出画布的部分用白底补齐。

    ``anchor="bottom"`` 时正方形**底边对齐角色的核心下缘**：卡通拼图里角色脚下有一层
    很淡的地面投影，居中裁会把投影留在画面里（深色主题下像一块灰斑）。按核心下缘
    对齐能把投影挤到画面外 —— 白色角色（白球 / 白幽灵）自己的下缘就是核心，不会被误切。
    """
    x0, y0, x1, y1 = box
    if anchor == "bottom":
        core_box = _core_box(image, box)
        cx0, cy0, cx1, cy1 = core_box
        side = round(max(cx1 - cx0, cy1 - cy0) * PADDING)
        left = round((cx0 + cx1) / 2 - side / 2)
        return _paste_on_white(image, left, cy1 + 2 - side, side).resize(
            (TILE_SIZE, TILE_SIZE), Image.LANCZOS
        )

    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half = max(x1 - x0, y1 - y0) * PADDING / 2
    size = round(half * 2)
    return _paste_on_white(image, round(cx - half), round(cy - half), size).resize(
        (TILE_SIZE, TILE_SIZE), Image.LANCZOS
    )


def _core_box(image: Image.Image, box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """包围盒内部的「角色核心」包围盒：有彩度或明显暗部（淡灰投影不算）。"""
    x0, y0, x1, y1 = box
    px = image.convert("RGB").load()
    min_x, min_y, max_x, max_y = x1, y1, x0, y0
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            r, g, b = px[x, y][:3]
            if max(r, g, b) - min(r, g, b) > 12 or (r + g + b) / 3 < 185:
                min_x, min_y = min(min_x, x), min(min_y, y)
                max_x, max_y = max(max_x, x), max(max_y, y)
    if min_x > max_x or min_y > max_y:  # 整格都是浅灰（不该发生）：退回原包围盒
        return box
    return (min_x, min_y, max_x, max_y)


MANIFEST_HEADER = """/**
 * 内置头像包清单（**由 `scripts/gen_avatar_pack.py` 生成，别手改**）。
 *
 * 「随机生成头像」从这里抽取：3D 黏土风角色，文件在 `public/avatars/`，
 * 随前端构建一起发布。数据库里 `agent_profiles.avatar_preset` 存编号
 * （如 `pack-007`），显示时拼成 `/avatars/pack-007.webp`。
 *
 * 画廊按 `AVATAR_GROUPS` 分组展示（人物 / 卡通……），编号连续、只往后追加。
 */
export const AVATAR_PRESETS = [
"""

MANIFEST_FOOTER = """] as const

/** 分类标签 → 画廊分组名（顺序即展示顺序，由生成脚本维护）。 */
export const AVATAR_CATEGORY_LABELS = {
"""

MANIFEST_TAIL = """} as const

/** 每个编号所属的分类（与 `AVATAR_PRESETS` 一一对应）。 */
export const AVATAR_PRESET_CATEGORIES: Record<string, string> = {
"""

MANIFEST_END = """}

/** 画廊分组：按分类把编号切成若干段（空分类不会出现）。 */
export const AVATAR_GROUPS: { key: string; label: string; presets: string[] }[] = Object.entries(
  AVATAR_CATEGORY_LABELS,
).map(([key, label]) => ({
  key,
  label,
  presets: AVATAR_PRESETS.filter((item) => AVATAR_PRESET_CATEGORIES[item] === key),
})).filter((group) => group.presets.length > 0)

export type AvatarPreset = (typeof AVATAR_PRESETS)[number]
"""


def build(
    sources: tuple[tuple[str, str], ...] = SOURCES, out_dir: Path = OUT_DIR
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    # 先清掉上一次的产物：换了风格重跑时，多出来的旧图不该继续被随机抽到。
    for stale in out_dir.glob(f"pack-*.{OUT_FORMAT}"):
        stale.unlink()
    written: list[Path] = []
    categories: dict[str, str] = {}
    index = 1
    for relative, category in sources:
        if category not in CATEGORIES:
            raise SystemExit(f"未知分类 {category!r}（先在 CATEGORIES 里登记分组名）")
        source = REPO_ROOT / relative
        if not source.is_file():
            raise SystemExit(f"缺少拼图源：{source}（先把它放进 generated-images/ 或改 SOURCES）")
        image = Image.open(source).convert("RGB")
        anchor = CROP_ANCHOR.get(category, "center")
        for box in _components(image.convert("L")):
            target = out_dir / f"pack-{index:03d}.{OUT_FORMAT}"
            _square_crop(image, box, anchor).save(
                target, "WEBP", quality=WEBP_QUALITY, method=6
            )
            written.append(target)
            categories[target.stem] = category
            index += 1
    # 清单与图片必须同步：多一张少一张，随机生成都会撞上 404。
    ids = [f"  '{path.stem}'," for path in written]
    labels = [f"  {key}: '{label}'," for key, label in CATEGORIES.items()]
    mapping = [f"  '{path.stem}': '{categories[path.stem]}'," for path in written]
    MANIFEST.write_text(
        MANIFEST_HEADER
        + "\n".join(ids)
        + "\n"
        + MANIFEST_FOOTER
        + "\n".join(labels)
        + "\n"
        + MANIFEST_TAIL
        + "\n".join(mapping)
        + "\n"
        + MANIFEST_END
    )
    return written


if __name__ == "__main__":
    # 命令行可临时覆盖源图（分类沿用 SOURCES 里的，或统一按 person 处理）。
    overrides = tuple(sys.argv[1:])
    sources = tuple((path, "person") for path in overrides) if overrides else SOURCES
    files = build(sources)
    total = sum(path.stat().st_size for path in files)
    print(f"已生成 {len(files)} 张头像 → {OUT_DIR}（共 {total / 1024:.0f} KB）")
