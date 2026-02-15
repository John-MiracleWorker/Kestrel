#!/usr/bin/env python3
"""
Libre Bird — macOS App Launcher

Starts the FastAPI backend server and opens the app in a native macOS window
using pywebview (WebKit-backed).
"""
import os
import signal
import socket
import subprocess
import sys
import time
import threading

# Ensure we're running from the project directory
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(PROJECT_DIR)


def is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    """Check if a port is accepting connections."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def start_server():
    """Start the uvicorn server."""
    import uvicorn
    uvicorn.run(
        "server:app",
        host="127.0.0.1",
        port=8741,
        log_level="info",
    )


def main():
    PORT = 8741
    URL = f"http://127.0.0.1:{PORT}"

    # Start backend server in a background thread
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    # Wait for server to be ready (up to 120s for model loading)
    print("⏳ Starting Libre Bird server...")
    for i in range(240):
        if is_port_open(PORT):
            print(f"✓ Server ready on {URL}")
            break
        time.sleep(0.5)
    else:
        print("✗ Server failed to start within 120 seconds")
        sys.exit(1)

    # Give it a moment to finish initialization
    time.sleep(1)

    # Start global hotkey listener (⌘+Shift+Space)
    try:
        from hotkey import global_hotkey
        global_hotkey.start()
        print("⌨️  Global hotkey registered: ⌘+Shift+Space")
    except Exception as e:
        print(f"⚠️  Global hotkey unavailable: {e}")

    # Open in a native macOS window via pywebview
    try:
        import webview

        print("🪟 Opening native Libre Bird window...")
        window = webview.create_window(
            title="Libre Bird",
            url=URL,
            width=1280,
            height=820,
            min_size=(800, 500),
            text_select=True,
        )

        # When the window is closed, exit the app
        def on_closed():
            print("\n👋 Libre Bird closed")
            os._exit(0)

        window.events.closed += on_closed

        # webview.start() blocks until all windows are closed
        webview.start()

    except ImportError:
        # Fallback: open in browser if pywebview is not installed
        import webbrowser
        print("⚠️  pywebview not installed — opening in browser instead")
        print("   Install it with: pip install pywebview")
        webbrowser.open(URL)

        print("💡 Libre Bird is running. Press Ctrl+C to quit.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n👋 Libre Bird closed")
            os._exit(0)


if __name__ == "__main__":
    main()
