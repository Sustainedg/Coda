"""重新下载正确的 Slack 主图标与钉钉蓝色版图标。"""
import subprocess
import sys
from pathlib import Path

RAW = Path(__file__).resolve().parent / "raw"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

JOBS = {
    "slack": "https://pipedream.com/s.v0/app_M0hv7G/logo/orig",
    "dingtalk": "https://gw.alicdn.com/imgextra/i3/O1CN014ZbI6ZTEhdC0ttN2_!!6000000003783-2-tps-444-444.png",
}

for name, url in JOBS.items():
    dest = RAW / f"{name}.img"
    result = subprocess.run(
        ["curl", "-sSL", "--max-time", "60", "-A", UA, "-o", str(dest), url],
        capture_output=True,
        text=True,
    )
    data = dest.read_bytes() if dest.exists() else b""
    print(f"{name}: rc={result.returncode}, {len(data)}B <- {url}")
    if not data:
        sys.exit(1)
