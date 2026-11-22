"""查看 Slack 页面里每个 logo 候选的 HTML 上下文，判断哪个是主图标。"""
import re
from pathlib import Path

html = Path(__file__).parent / "raw" / "slack.page2.html"
text = html.read_text(encoding="utf-8", errors="replace")

for m in re.finditer(r"app_[A-Za-z0-9]+/logo/orig", text):
    start = max(0, m.start() - 400)
    ctx = re.sub(r"\s+", " ", text[start : m.end() + 120])
    print("---", m.group(0))
    print(ctx[-380:])
    print()
