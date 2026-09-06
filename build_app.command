#!/bin/zsh
set -e
PROJECT_ROOT="${0:A:h}"
RESOURCE_DIR="$PROJECT_ROOT/PDF til Kindle.app/Contents/Resources"
mkdir -p "$RESOURCE_DIR"
cp "$PROJECT_ROOT/app.py" "$RESOURCE_DIR/app.py"
cp "$PROJECT_ROOT/kindle_converter.py" "$RESOURCE_DIR/kindle_converter.py"
cp "$PROJECT_ROOT/assets/app-icon.png" "$RESOURCE_DIR/AppIcon.png"

python3 "$PROJECT_ROOT/make_icon.py"
chmod +x "$PROJECT_ROOT/PDF til Kindle.app/Contents/MacOS/pdf-til-kindle"
touch "$PROJECT_ROOT/PDF til Kindle.app"
echo "PDF til Kindle.app er opdateret."
