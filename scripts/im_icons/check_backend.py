"""验证后端平台元数据的 icon 字段与前端资源一一对应。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.im.base import PLATFORMS  # noqa: E402

DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

ok = True
for platform in PLATFORMS:
    icon = platform.icon
    exists = (DIST / icon.lstrip("/")).is_file() if icon.startswith("/") else False
    flag = "✔" if exists else "✘"
    if not exists:
        ok = False
    print(f"{flag} {platform.id:10s} {icon}")

print("全部就绪" if ok else "存在缺失")
sys.exit(0 if ok else 1)
