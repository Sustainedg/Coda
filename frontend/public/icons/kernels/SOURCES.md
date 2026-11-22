# 内核图标来源

「内核管理」卡片上的品牌图。`logo_style = "mask"` 的单色 SVG 由界面染色；
`logo_style = "image"` 的位图按原色显示，**随仓库发布，运行时不做外链**。

| 文件 | 内核 | 来源 | 处理 |
| --- | --- | --- | --- |
| `claude.webp` | Claude Code | 微软商店应用图标（`store-images.s-microsoft.com`），2026-11-21 抓取 | 裁透明边、补正方形，256×256 WebP q90，原色 |
| `openai.webp` | Codex / OpenAI 兼容内核 | 微软商店应用图标（`store-images.s-microsoft.com`），2026-11-21 抓取 | 裁透明边、补正方形，256×256 WebP q90，原色 |
| `gemini.webp` | Gemini CLI | `geminicli.com/_astro/icon.*.png`，2026-11-21 抓取 | 裁透明边、补正方形，256×256 WebP q90，原色 |
| `deepseek.webp` | DeepSeek Harness | 微软商店应用图标（`images-eds-ssl.xboxlive.com`），2026-11-21 抓取 | 裁透明边、补正方形，256×256 WebP q90，原色 |
| `hermes.webp` | Hermes | 微软商店应用图标（`store-images.s-microsoft.com`），2026-11-21 抓取 | 裁透明边、补正方形，256×256 WebP q90，原色 |
| `cli.svg` | 自定义 CLI | 中性终端线条图形 | 24×24 单色，遮罩 |
| `codebuddy.webp` | CodeBuddy CLI | `assets.ferentin.net` 官方集成 Logo，2026-11-21 抓取 | 裁透明边、补正方形，256×256 WebP q90，原色 |
| `doubao.png` | 豆包 CLI | 豆包工作官网头图（`doubao.com/work`）的「豆包工作」品牌标记，2026-11-20 抓取 | 裁出左侧方形标记，去透明边、补正方形，128×128 PNG |

## 约定

- 位图：正方形、透明底、≤ 512×512、≤ 256 KB；优先 PNG / WebP。
- 位图只在 `logo_style = "image"` 时使用；加载失败时界面显示一个中性占位方块
  （`kernel-logo-missing`，不做旧 SVG 兜底）。
- 界面用的是**内嵌**的图形（`scripts/gen_kernel_logos.py` 生成
  `frontend/src/lib/kernelLogos.ts`）：图形会以 base64 进前端包，**优先 WebP**
  （同尺寸下比 PNG 小一个量级），改了这里的文件要重跑一次该脚本，否则界面还是旧图形。
- 新增内核时把源素材与抓取日期补进上表；商标归各自权利人所有，仅用于标识对应内核。
