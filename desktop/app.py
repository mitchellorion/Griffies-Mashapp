"""Griffies desktop launcher.

Starts the local server on a free 127.0.0.1 port and opens it in a native,
resizable window (pywebview) that remembers its size and position. Falls back
to the default browser with --browser or if pywebview is unavailable.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

WINDOW_STATE = Path.home() / ".config" / "sniffies-messages" / "window.json"


def _free_port(preferred: int = 8765) -> int:
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return sock.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("No free port")


def _wait_ready(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=1):
                return
        except Exception:
            time.sleep(0.15)


def _load_window_state() -> dict:
    try:
        data = json.loads(WINDOW_STATE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_window_state(win) -> None:
    try:
        WINDOW_STATE.parent.mkdir(parents=True, exist_ok=True)
        WINDOW_STATE.write_text(json.dumps({"width": win.width, "height": win.height, "x": win.x, "y": win.y}))
    except Exception:
        pass


def _initial_geometry(webview) -> dict:
    """Saved size/position if it still fits a screen, else ~80% of the main screen, centered."""
    try:
        screen = webview.screens[0]
        sw, sh = int(screen.width), int(screen.height)
    except Exception:
        sw, sh = 1440, 900
    saved = _load_window_state()
    w = int(saved.get("width") or 0)
    h = int(saved.get("height") or 0)
    if 480 <= w <= sw and 560 <= h <= sh:
        geo = {"width": w, "height": h}
        x, y = saved.get("x"), saved.get("y")
        if isinstance(x, int) and isinstance(y, int) and 0 <= x < sw - 100 and 0 <= y < sh - 100:
            geo.update(x=x, y=y)
        return geo
    w = max(480, min(1400, int(sw * 0.8)))
    h = max(560, min(1000, int(sh * 0.8)))
    return {"width": w, "height": h, "x": max(0, (sw - w) // 2), "y": max(0, (sh - h) // 2)}


def main() -> None:
    port = _free_port()
    os.environ["SNIFFIES_PORT"] = str(port)
    # server.py lives one folder up in source checkouts; PyInstaller bundles it as a module.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import server  # noqa: E402

    httpd = server.start_server(port)
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    _wait_ready(url)

    if "--browser" not in sys.argv:
        try:
            import webview  # pywebview

            webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
            win = webview.create_window(
                "Griffies",
                url,
                resizable=True,
                fullscreen=False,
                min_size=(420, 560),
                background_color="#0b0b0b",
                **_initial_geometry(webview),
            )
            win.events.closing += lambda: _save_window_state(win)
            webview.start()
            httpd.shutdown()
            return
        except Exception as exc:  # no GUI backend: fall back to the browser
            print(f"Native window unavailable ({exc}); opening your browser instead.")

    webbrowser.open(url)
    print(f"Griffies is running at {url}. Press Ctrl+C to quit.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        httpd.shutdown()


if __name__ == "__main__":
    main()
