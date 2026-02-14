#!/usr/bin/env python3
"""Build Libre Bird.app macOS bundle."""
import os
import shutil
import struct
import subprocess
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "Libre Bird"
APP_DIR = os.path.join(os.path.expanduser("~"), "Desktop", f"{APP_NAME}.app")
CONTENTS = os.path.join(APP_DIR, "Contents")
MACOS = os.path.join(CONTENTS, "MacOS")
RESOURCES = os.path.join(CONTENTS, "Resources")

ICON_SRC = os.path.join(
    os.path.expanduser("~"),
    ".gemini/antigravity/brain/8ab06dc2-e5f9-4d60-a24b-0b7aad2a3066",
    "libre_bird_icon_1770851859290.png",
)


def create_iconset():
    """Create .icns from source PNG using sips + iconutil."""
    iconset = os.path.join(PROJECT_DIR, "AppIcon.iconset")
    os.makedirs(iconset, exist_ok=True)

    sizes = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]

    for size, name in sizes:
        out = os.path.join(iconset, name)
        subprocess.run(
            ["sips", "-z", str(size), str(size), ICON_SRC, "--out", out],
            capture_output=True, timeout=10,
        )

    icns_path = os.path.join(RESOURCES, "AppIcon.icns")
    result = subprocess.run(
        ["iconutil", "-c", "icns", iconset, "-o", icns_path],
        capture_output=True, timeout=10,
    )
    shutil.rmtree(iconset, ignore_errors=True)

    if result.returncode == 0:
        print(f"✓ Icon created: {icns_path}")
    else:
        print(f"⚠ Icon creation failed: {result.stderr.decode()}")
        # Copy PNG as fallback
        shutil.copy2(ICON_SRC, os.path.join(RESOURCES, "AppIcon.png"))
        print("  Using PNG fallback")


def create_info_plist():
    """Create Info.plist."""
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>{APP_NAME}</string>
    <key>CFBundleDisplayName</key>
    <string>{APP_NAME}</string>
    <key>CFBundleIdentifier</key>
    <string>com.librebird.app</string>
    <key>CFBundleVersion</key>
    <string>1.0.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0.0</string>
    <key>CFBundleExecutable</key>
    <string>launcher</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSUIElement</key>
    <false/>
    <key>NSAppleEventsUsageDescription</key>
    <string>Libre Bird needs accessibility access to see your screen context.</string>
</dict>
</plist>"""
    path = os.path.join(CONTENTS, "Info.plist")
    with open(path, "w") as f:
        f.write(plist)
    print(f"✓ Info.plist created")


def create_launcher():
    """Create shell launcher script."""
    launcher = f"""#!/bin/bash
# Libre Bird macOS launcher
DIR="{PROJECT_DIR}"

# Activate venv and run the app
cd "$DIR"
source .venv/bin/activate
exec python3 app.py
"""
    path = os.path.join(MACOS, "launcher")
    with open(path, "w") as f:
        f.write(launcher)
    os.chmod(path, 0o755)
    print(f"✓ Launcher script created")


def main():
    print(f"Building {APP_NAME}.app...")

    # Clean previous build
    if os.path.exists(APP_DIR):
        shutil.rmtree(APP_DIR)

    # Create directory structure
    os.makedirs(MACOS, exist_ok=True)
    os.makedirs(RESOURCES, exist_ok=True)

    create_info_plist()
    create_launcher()
    create_iconset()

    print(f"\n✅ {APP_NAME}.app built successfully at:\n   {APP_DIR}")


if __name__ == "__main__":
    main()
