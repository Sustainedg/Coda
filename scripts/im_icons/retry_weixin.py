"""重试下载微信图标（上次超时）。"""
import subprocess
import sys
from pathlib import Path

RAW = Path(__file__).resolve().parent / "raw"
URL = (
    "https://images-eds-ssl.xboxlive.com/image?url=4rt9.lXDC4H_93laV1_eHHFT949fUipzkiFOBH3fAiZZUCdYojwUyX2aTonS1aIwMrx6NUIsHfUHSLzjGJFxxl33gjvYDEfZR.qiz4WDwgYWDcAq4yeQFhSdZhaZSIx9HLE5uwY9vjDnU1thaOnDfu7shWajj6js22jlPuKphdw-&format=source&h=307"
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

for attempt in range(3):
    dest = RAW / "weixin.img"
    result = subprocess.run(
        ["curl", "-sSL", "--max-time", "120", "-A", UA, "-o", str(dest), URL],
        capture_output=True,
        text=True,
    )
    data = dest.read_bytes() if dest.exists() else b""
    print(f"尝试 {attempt + 1}: rc={result.returncode}, {len(data)} bytes")
    if data:
        sys.exit(0)
