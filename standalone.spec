from pathlib import Path
import shutil
import sys

from PyInstaller.utils.hooks import collect_all


project_root = Path(SPECPATH)
vendor_dir = project_root / "PDF til Kindle.app" / "Contents" / "Resources" / "vendor"
sys.path.insert(0, str(vendor_dir))

pymupdf_datas, pymupdf_binaries, pymupdf_hiddenimports = collect_all("pymupdf")

tesseract_command = shutil.which("tesseract")
if not tesseract_command:
    raise SystemExit("Tesseract blev ikke fundet. Installér det med: brew install tesseract")
tesseract_binary = Path(tesseract_command).resolve()

tessdata_candidates = [
    tesseract_binary.parents[1] / "share" / "tessdata",
    Path("/usr/local/share/tessdata"),
    Path("/opt/homebrew/share/tessdata"),
]
tessdata_dir = next((path for path in tessdata_candidates if (path / "eng.traineddata").is_file()), None)
if tessdata_dir is None:
    raise SystemExit("Tesseracts engelske sprogdata blev ikke fundet.")

datas = [
    (str(project_root / "assets" / "app-icon.png"), "."),
    (str(tessdata_dir / "eng.traineddata"), "tessdata"),
]
for optional_file in ("osd.traineddata", "pdf.ttf"):
    if (tessdata_dir / optional_file).is_file():
        datas.append((str(tessdata_dir / optional_file), "tessdata"))
for optional_dir in ("configs", "tessconfigs"):
    if (tessdata_dir / optional_dir).is_dir():
        datas.append((str(tessdata_dir / optional_dir), f"tessdata/{optional_dir}"))
datas.extend(pymupdf_datas)

analysis = Analysis(
    [str(project_root / "app.py")],
    pathex=[str(project_root), str(vendor_dir)],
    binaries=[(str(tesseract_binary), "tools"), *pymupdf_binaries],
    datas=datas,
    hiddenimports=pymupdf_hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="PDF til Kindle",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
)

collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="PDF til Kindle",
)

app = BUNDLE(
    collection,
    name="PDF til Kindle.app",
    icon=str(project_root / "PDF til Kindle.app" / "Contents" / "Resources" / "AppIcon.icns"),
    bundle_identifier="dk.local.pdftokindle",
    info_plist={
        "CFBundleDisplayName": "PDF til Kindle",
        "CFBundleShortVersionString": "1.0.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "13.0",
        "NSHighResolutionCapable": True,
    },
)
