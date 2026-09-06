from pathlib import Path
from PIL import Image


root = Path(__file__).resolve().parent
source = root / "assets" / "app-icon.png"
target = root / "PDF til Kindle.app" / "Contents" / "Resources" / "AppIcon.icns"

with Image.open(source) as image:
    icon = image.convert("RGBA")
    icon.save(target, format="ICNS", sizes=[(16, 16), (32, 32), (64, 64), (128, 128),
                                            (256, 256), (512, 512), (1024, 1024)])
