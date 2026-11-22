"""下载 8 个 IM 平台图标。

前 6 个是直链图片（微软商店 / Xbox Live CDN）；Slack 与钉钉给的是网页地址，
抓取页面后从 HTML 里提取真实的图片链接再下载。
"""
import re
import subprocess
import sys
from pathlib import Path

RAW = Path(__file__).resolve().parent / "raw"
RAW.mkdir(exist_ok=True)

# 直链图片：(文件名, 下载地址)
DIRECT = {
    "telegram": "https://store-images.s-microsoft.com/image/apps.28846.14473651905739879.7f43362d-1933-408d-9e83-d87d2265f4cf.a6be247d-0433-4b8a-ad2f-536e801f6b61?h=307",
    "feishu": "https://images-eds-ssl.xboxlive.com/image?url=4rt9.lXDC4H_93laV1_eHM0OYfiFeMI2p9MWie0CvL99U4GA1gf6_kayTt_kBblFwHwo8BW8JXlqfnYxKPmmBfvahpcN7qcKvs1xEfEG0kin.EKI1XdwMSTvCpHa1m2YqozUHcFZ7IHeBKW0l3_GNGG6p_0ZKVgDiYufAc1aL_c-&format=webp&h=307",
    "wecom": "https://images-eds-ssl.xboxlive.com/image?url=4rt9.lXDC4H_93laV1_eHHFT949fUipzkiFOBH3fAiZZUCdYojwUyX2aTonS1aIwMrx6NUIsHfUHSLzjGJFxxjF79EViCpN_HWZ14wldz8E1QGCM94D6CUChetj8pc1nQjRW3HLTth.BR4itrvFyV2zpApqiqv_TzasD5I2hJjQ-&format=source&h=307",
    "discord": "https://images-eds-ssl.xboxlive.com/image?url=4rt9.lXDC4H_93laV1_eHM0OYfiFeMI2p9MWie0CvL99U4GA1gf6_kayTt_kBblFwHwo8BW8JXlqfnYxKPmmBevsdZpJiIhrXJKvOSYipsYbqdUBBn6r6Hb.keWYwuyu5QJ84NCtr5ij3JMrlPglnBeeDch3kJBTCQneZnfl9dA-&format=source&h=307",
    "qq": "https://images-eds-ssl.xboxlive.com/image?url=4rt9.lXDC4H_93laV1_eHM0OYfiFeMI2p9MWie0CvL99U4GA1gf6_kayTt_kBblFwHwo8BW8JXlqfnYxKPmmBWynjZdhLvb3YALx62jmzbB6V.84cWHDfnsqNi5fUNtNyYSA9lSCUqKhmpWKMmUFKucZiy09x9jhTueeiXjW_WM-&format=webp&h=307",
    "weixin": "https://images-eds-ssl.xboxlive.com/image?url=4rt9.lXDC4H_93laV1_eHHFT949fUipzkiFOBH3fAiZZUCdYojwUyX2aTonS1aIwMrx6NUIsHfUHSLzjGJFxxl33gjvYDEfZR.qiz4WDwgYWDcAq4yeQFhSdZhaZSIx9HLE5uwY9vjDnU1thaOnDfu7shWajj6js22jlPuKphdw-&format=source&h=307",
}

# 网页地址：(文件名, 页面 URL)
PAGES = {
    "slack": "https://pipedream.com/apps/slack-v2",
    "dingtalk": "https://alibaba-group.cn.aptoide.com/app",
}

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def fetch(url: str, dest: Path) -> bytes:
    """curl 下载；返回字节。"""
    result = subprocess.run(
        ["curl", "-sSL", "--max-time", "60", "-A", UA, "-o", str(dest), url],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"curl 失败({result.returncode}): {url}\n{result.stderr}")
    data = dest.read_bytes()
    if not data:
        raise RuntimeError(f"下载为空: {url}")
    return data


def pick_image_from_page(name: str, page_url: str) -> str:
    """从网页里挑出最像平台图标的图片 URL。"""
    html = fetch(page_url, RAW / f"{name}.page.html").decode("utf-8", "replace")
    # 收集候选：og:image、icon 链接、<img> 的 src
    candidates: list[str] = []
    for pattern in (
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        r'<link[^>]+rel=["\'][^"\']*icon["\'][^>]+href=["\']([^"\']+)',
        r'<img[^>]+src=["\']([^"\']+)',
    ):
        candidates.extend(re.findall(pattern, html, re.I))
    for url in candidates:
        if any(bad in url.lower() for bad in (".js", ".css", ".svg", "sprite", "loader", "blank")):
            continue
        full = url if url.startswith("http") else "https://pipedream.com" + url
        try:
            data = fetch(full, RAW / f"{name}.candidate")
        except Exception as exc:  # noqa: BLE001
            print(f"  候选失败 {full}: {exc}")
            continue
        # 图标一般是正方形小图：尺寸落在 32~512 之间
        try:
            from PIL import Image
            import io

            with Image.open(io.BytesIO(data)) as img:
                w, h = img.size
            if 48 <= w == h <= 512:
                return full
            print(f"  候选尺寸 {w}x{h} 不合: {full}")
        except Exception as exc:  # noqa: BLE001
            print(f"  候选不是图片 {full}: {exc}")
    raise RuntimeError(f"页面里没找到合适的图标图: {page_url}")


def main() -> int:
    for name, url in DIRECT.items():
        dest = RAW / f"{name}.img"
        data = fetch(url, dest)
        print(f"[direct] {name}: {len(data)} bytes")
    for name, page_url in PAGES.items():
        url = pick_image_from_page(name, page_url)
        data = fetch(url, RAW / f"{name}.img")
        print(f"[page]   {name}: {len(data)} bytes <- {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
