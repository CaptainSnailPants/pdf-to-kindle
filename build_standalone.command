#!/bin/zsh
set -e

PROJECT_ROOT="${0:A:h}"
export PYINSTALLER_CONFIG_DIR="$PROJECT_ROOT/.pyinstaller-cache"
PYTHON_BIN="/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3)"
fi

"$PROJECT_ROOT/build_app.command"
"$PYTHON_BIN" -m PyInstaller \
  --clean \
  --noconfirm \
  --distpath "$PROJECT_ROOT/standalone-dist" \
  --workpath "$PROJECT_ROOT/standalone-build" \
  "$PROJECT_ROOT/standalone.spec"

STANDALONE_APP="$PROJECT_ROOT/standalone-dist/PDF to Kindle.app"
/usr/bin/codesign --force --deep --sign - "$STANDALONE_APP"
/usr/bin/codesign --verify --deep --strict "$STANDALONE_APP"

echo "Standalone app created: $STANDALONE_APP"
