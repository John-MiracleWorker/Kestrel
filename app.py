#!/usr/bin/env python3
"""
Libre Bird — Native macOS App Launcher

Starts the FastAPI backend server and opens a native pywebview window.
"""
import multiprocessing
import os
import signal
import socket
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

    # Open native window
    import webview

    window = webview.create_window(
        title="Libre Bird",
        url=URL,
        width=1200,
        height=800,
        min_size=(800, 600),
        text_select=True,
    )

    # Start the webview (this blocks until window is closed)
    webview.start(
        debug=False,
        private_mode=False,
    )

    # Window closed — exit cleanly
    print("👋 Libre Bird closed")
    os._exit(0)


if __name__ == "__main__":
    main()
