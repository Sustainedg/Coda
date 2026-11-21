#!/usr/bin/env python3
"""把内置头像包的白底批量抠成透明背景。

空会话的大头像直接显示在页面上（不再是圆框小图），白底在深色主题下像一块
补丁 —— 这里把 `public/avatars/pack-*.webp` 统一处理成透明版，**原地覆盖**
（文件名不变；缓存失效靠前端 `avatarPresetUrl` 的内容版本号，见
`lib/agentAvatar.ts` 的 `AVATAR_PACK_REV`，重跑本脚本后记得 +1）。

两套判据，按**背景色**自动分流（人物拼图的纸白底 ≈ 245~250，卡通拼图是 255 纯白）：

**v2（人物）**：flood-fill 从四边灌入，只清「与画布边缘连通」的区域 —— 人物内部
再浅的颜色（白衬衫、白胡子）不挨着边缘，永远不会被整片掏空；候选像素同时满足
① 与相邻背景像素颜色距离 ≤ 14（局部比对，抗背景轻微渐变）、② 无彩度（RGB 极差
≤ 10）。只用①（v1）会吃掉浅色人物；只用亮度阈值也分不开人物浅色部位。
实测 60 张人物零损失（覆盖率与白底原版一致）。

**v3（纯白底卡通）**：卡通拼图的问题是「角色本身就有纯白身体」（白球 / 白幽灵）+
脚下有一层淡灰投影。v2 的局部容差会顺着白球软边一路灌进身体。v3 的步骤：
① 背景只认**纯白**（亮度 ≥ 254 且无彩度），白球自己的暗边就是屏障，灌不进去；
② 3px 闭运算把高光边上的细缝封住（否则白球身上会被灌出小孔）；
③ 填洞 —— 灌不到的白区就是身体内部，整体保留；④ 只留最大连通域；
⑤ 逐列剪投影 —— 「整列没有核心像素（彩度 / 暗部）、整列都是亮中性灰、且很矮」的
整列直接丢；剩下「核心下缘紧跟着一小截亮中性灰」的列也剪掉（脚底与投影连在一起的
情况），但只在离核心 ≤ 30% 身高时才剪，白色角色的大片白身体不会遭殃。

只依赖 Pillow。幂等：已透明的图重跑不变（但仍建议从 git 原版重跑，保证口径统一）。
"""
from __future__ import annotations

from collections import deque
from pathlib import Path

from PIL import Image, ImageFilter

REPO_ROOT = Path(__file__).resolve().parent.parent
AVATAR_DIR = REPO_ROOT / "frontend" / "public" / "avatars"

#: 与相邻背景像素的颜色距离容差（RGB 欧氏距离）。
BG_TOLERANCE = 14
#: 背景的无彩度上限（RGB 极差）：灰白背景 ≤10，人物浅色带色偏 >10。
BG_CHROMA_MAX = 10
#: 背景亮度达到它就算「纯白底」（走 v3 判据）：卡通拼图 254~255，人物拼图 245~250。
#: 不能往下松：白球 / 白幽灵的亮部就在 250~253，一旦把 253 当背景，灌水会从软边渗进去。
PURE_WHITE_MIN = 254
#: 闭运算半径（px）：白角色是软边 + 高光，纯白灌水会从边角渗进去、在身上留下小孔。
#: 先膨胀 3px 再腐蚀 3px，把那些细缝封上又不改变整体轮廓。
CLOSE_RADIUS = 3
#: 「核心像素」判据：有明显彩度，或明显暗部（淡灰投影两者都不满足）。
CORE_CHROMA = 12
CORE_DARKNESS = 185


def flood_transparent(image: Image.Image) -> Image.Image:
    """从四边 flood-fill 白底并抠成透明（就地修改并返回 RGBA）。"""
    rgba = image.convert("RGBA")
    width, height = rgba.size
    px = rgba.load()
    seen = [[False] * width for _ in range(height)]
    queue: deque[tuple[int, int, tuple[int, int, int]]] = deque()
    for x in range(width):
        for y in (0, height - 1):
            queue.append((x, y, px[x, y][:3]))
    for y in range(height):
        for x in (0, width - 1):
            queue.append((x, y, px[x, y][:3]))
    while queue:
        x, y, ref = queue.popleft()
        if not (0 <= x < width and 0 <= y < height) or seen[y][x]:
            continue
        seen[y][x] = True
        r, g, b = px[x, y][:3]
        distance = ((r - ref[0]) ** 2 + (g - ref[1]) ** 2 + (b - ref[2]) ** 2) ** 0.5
        chroma = max(r, g, b) - min(r, g, b)
        if distance <= BG_TOLERANCE and chroma <= BG_CHROMA_MAX:
            px[x, y] = (r, g, b, 0)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < width and 0 <= ny < height and not seen[ny][nx]:
                    queue.append((nx, ny, (r, g, b)))
    return rgba


def _is_pure_white(c: tuple[int, int, int]) -> bool:
    return (c[0] + c[1] + c[2]) / 3 >= PURE_WHITE_MIN and max(c) - min(c) <= 5


def _is_core(c: tuple[int, int, int]) -> bool:
    """角色核心像素：有彩度或明显暗部（投影是「亮 + 中性」的，两者都不满足）。"""
    return max(c) - min(c) > CORE_CHROMA or (c[0] + c[1] + c[2]) / 3 < CORE_DARKNESS


def _flood_white_background(px, width: int, height: int) -> list[list[bool]]:
    """从四边灌入「纯白」背景，返回 outside 掩码（灌不到的白区 = 角色内部）。"""
    outside = [[False] * width for _ in range(height)]
    queue: deque[tuple[int, int]] = deque()
    for x in range(width):
        for y in (0, height - 1):
            if _is_pure_white(px[x, y][:3]) and not outside[y][x]:
                outside[y][x] = True
                queue.append((x, y))
    for y in range(height):
        for x in (0, width - 1):
            if _is_pure_white(px[x, y][:3]) and not outside[y][x]:
                outside[y][x] = True
                queue.append((x, y))
    while queue:
        cx, cy = queue.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = cx + dx, cy + dy
            if (
                0 <= nx < width
                and 0 <= ny < height
                and not outside[ny][nx]
                and _is_pure_white(px[nx, ny][:3])
            ):
                outside[ny][nx] = True
                queue.append((nx, ny))
    return outside


def _trim_shadow_below_core(px, keep: list[list[bool]], width: int, height: int) -> None:
    """逐列剪掉「核心像素下缘以下」的亮中性灰（脚底投影）。

    投影是贴地的一层淡灰：无彩度、亮度 185~250、且总在角色的**最下面**。所以对每一列
    找到最后一个「核心像素」（有彩度或明显暗部），它下面的亮中性灰全是投影，直接剪掉。
    整列都没有核心像素的（只可能是投影或纯背景）也一并剪掉。

    白色角色（白球 / 白幽灵）不会被误剪：它们的身体下缘本身就有明暗（球底、褶边），
    那圈阴影像素满足「核心」判据，正好挡住往下剪的边界。
    """
    for x in range(width):
        ys = [y for y in range(height) if keep[y][x]]
        if not ys:
            continue
        top, bottom = min(ys), max(ys)
        last_core = -1
        for y in range(top, bottom + 1):
            if _is_core(px[x, y][:3]):
                last_core = y
        for y in range(last_core + 1, bottom + 1):
            r, g, b = px[x, y][:3]
            # 投影是「浅灰」：彩度低、亮度中上（带上一点暗边，150 起）。
            # 彩度门槛放到 22：投影常被角色颜色染一点点（粉 / 绿的淡淡反光）。
            if max(r, g, b) - min(r, g, b) <= 22 and (r + g + b) / 3 >= 150:
                keep[y][x] = False


def white_bg_transparent(image: Image.Image) -> Image.Image:
    """纯白底抠图（v3）：保留纯白身体、丢掉地面投影。"""
    rgba = image.convert("RGBA")
    width, height = rgba.size
    px = rgba.load()

    outside = _flood_white_background(px, width, height)
    fg = [[not outside[y][x] for x in range(width)] for y in range(height)]

    # 闭运算：先膨胀再腐蚀，封住软边上的细缝（否则白球内部会被灌出小孔），
    # 轮廓整体不变。
    raw = Image.new("L", (width, height), 0)
    rpx = raw.load()
    for y in range(height):
        for x in range(width):
            if fg[y][x]:
                rpx[x, y] = 255
    size = CLOSE_RADIUS * 2 + 1
    closed = raw.filter(ImageFilter.MaxFilter(size)).filter(ImageFilter.MinFilter(size))
    cpx = closed.load()

    # 填洞：从四边灌「掩膜里的空」，灌不到的空白 = 角色内部（白球 / 白幽灵的肚子）
    hole = [[False] * width for _ in range(height)]
    queue: deque[tuple[int, int]] = deque()
    for x in range(width):
        for y in (0, height - 1):
            if cpx[x, y] == 0 and not hole[y][x]:
                hole[y][x] = True
                queue.append((x, y))
    for y in range(height):
        for x in (0, width - 1):
            if cpx[x, y] == 0 and not hole[y][x]:
                hole[y][x] = True
                queue.append((x, y))
    while queue:
        cx, cy = queue.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = cx + dx, cy + dy
            if 0 <= nx < width and 0 <= ny < height and cpx[nx, ny] == 0 and not hole[ny][nx]:
                hole[ny][nx] = True
                queue.append((nx, ny))
    for y in range(height):
        for x in range(width):
            if not hole[y][x]:
                cpx[x, y] = 255

    # 只留最大连通域：投影 / 碎屑都是小孤岛
    seen = [[False] * width for _ in range(height)]
    comps: list[list[tuple[int, int]]] = []
    for y in range(height):
        for x in range(width):
            if cpx[x, y] == 0 or seen[y][x]:
                continue
            queue = deque([(x, y)])
            seen[y][x] = True
            pts = []
            while queue:
                cx, cy = queue.popleft()
                pts.append((cx, cy))
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx, cy + dy
                    if (
                        0 <= nx < width
                        and 0 <= ny < height
                        and cpx[nx, ny] > 0
                        and not seen[ny][nx]
                    ):
                        seen[ny][nx] = True
                        queue.append((nx, ny))
            comps.append(pts)
    main = max(comps, key=len)
    keep = [[False] * width for _ in range(height)]
    for x, y in main:
        keep[y][x] = True
    _trim_shadow_below_core(px, keep, width, height)

    mask = Image.new("L", (width, height), 0)
    mpx = mask.load()
    for y in range(height):
        for x in range(width):
            if keep[y][x]:
                mpx[x, y] = 255
    mask = mask.filter(ImageFilter.GaussianBlur(1.0))
    out = rgba.copy()
    opx = out.load()
    for y in range(height):
        for x in range(width):
            r, g, b = opx[x, y][:3]
            opx[x, y] = (r, g, b, mask.getpixel((x, y)))
    return out


def _background_is_pure_white(image: Image.Image) -> bool:
    """看上边 + 左右两边（**不含下边**）的中位亮度：纯白卡通用 v3，纸白人物用 v2。

    不含下边：卡通是「底边对齐角色下缘」裁的，下边那一行可能正好压着角色或投影，
    取样会把它拉成灰的，误判成人物底（走了 v2 就会把白球灌穿）。
    """
    width, height = image.size
    px = image.load()
    samples = [px[x, 0][:3] for x in range(0, width, 3)]
    samples += [px[0, y][:3] for y in range(0, height, 3)]
    samples += [px[width - 1, y][:3] for y in range(0, height, 3)]
    samples.sort(key=lambda c: sum(c))
    median = samples[len(samples) // 2]
    return (median[0] + median[1] + median[2]) / 3 >= PURE_WHITE_MIN


def main() -> None:
    avatars = sorted(AVATAR_DIR.glob("pack-*.webp"))
    if not avatars:
        raise SystemExit(f"没有找到头像包：{AVATAR_DIR}")
    for path in avatars:
        source = Image.open(path).convert("RGBA")
        # 已经透明过的图（四角全透明）不重复写，重跑保持幂等。
        alpha = source.getchannel("A")
        if all(
            alpha.getpixel(p) == 0
            for p in [
                (2, 2),
                (source.width - 3, 2),
                (2, source.height - 3),
                (source.width - 3, source.height - 3),
            ]
        ):
            continue
        if _background_is_pure_white(source):
            white_bg_transparent(source).save(path, "WEBP", quality=92, method=6)
        else:
            flood_transparent(source).save(path, "WEBP", quality=92, method=6)
        print(f"  {path.name} → 透明背景 ✓")
    print(f"完成：{len(avatars)} 张头像已处理")


if __name__ == "__main__":
    main()
