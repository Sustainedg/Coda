"""检查本机可用的图像处理能力（PIL / 工具）。"""
import shutil

print("curl:", shutil.which("curl"))
print("wget:", shutil.which("wget"))
print("convert:", shutil.which("convert"))
print("magick:", shutil.which("magick"))

try:
    from PIL import Image
    import PIL
    print("PIL:", PIL.__version__)
except ImportError as exc:
    print("PIL: MISSING ->", exc)
