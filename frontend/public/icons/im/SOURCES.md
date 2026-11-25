# IM 平台图标来源

「智能体设定 → IM」标签里各平台的品牌图标。路径由后端平台元数据（`backend/app/im/base.py`
的 `PLATFORMS`）下发，**随仓库发布，运行时不外链**。

| 文件 | 平台 | 来源 | 处理 |
| --- | --- | --- | --- |
| `telegram.webp` | Telegram | 微软商店应用图标（`store-images.s-microsoft.com`），2026-11-21 抓取 | 裁透明边、补正方形，256×256 WebP q90，原色 |
| `feishu.webp` | 飞书 / Lark | 微软商店应用图标（`images-eds-ssl.xboxlive.com`），2026-11-21 抓取 | 裁透明边、补正方形，256×256 WebP q90，原色 |
| `dingtalk.webp` | 钉钉 | 钉钉官网品牌图（`gw.alicdn.com`，从 `dingtalk.com` 页面提取），2026-11-21 抓取 | 裁透明边、补正方形，256×256 WebP q90，原色 |
| `wecom.webp` | 企业微信 | 微软商店应用图标（`images-eds-ssl.xboxlive.com`），2026-11-21 抓取 | 裁透明边、补正方形，256×256 WebP q90，原色 |
| `qq.webp` | QQ | 微软商店应用图标（`images-eds-ssl.xboxlive.com`），2026-11-21 抓取 | 裁透明边、补正方形，256×256 WebP q90，原色 |
| `discord.webp` | Discord | 微软商店应用图标（`images-eds-ssl.xboxlive.com`），2026-11-21 抓取 | 裁透明边、补正方形，256×256 WebP q90，原色 |
| `slack.webp` | Slack | Pipedream「Slack v2」应用页主图标（`pipedream.com/apps/slack-v2` 的 JSON-LD `image` 与页首大图），2026-11-21 抓取 | 裁透明边、补正方形，256×256 WebP q90，原色 |
| `weixin.webp` | 微信 | 微软商店应用图标（`images-eds-ssl.xboxlive.com`），2026-11-21 抓取 | 裁透明边、补正方形，256×256 WebP q90，原色 |

## 约定

- 位图：正方形、透明底、≤ 512×512；统一 256×256 WebP q90，原色显示。
- 加载失败时界面回落为中性文字占位（🔌），不留裂图与空洞。
- 抓取与处理脚本见 `scripts/im_icons/`（`download.py` / `process.py` / `verify.py`）。
