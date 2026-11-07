"""
FlowShort Desktop App — launch_app.py
Starts Flask in background, opens native pywebview window.
Requires: pip install pywebview
"""
import logging
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

LOG_FILE = ROOT / "flowshort_launch.log"

def _log(msg: str) -> None:
    import datetime
    line = f"[{datetime.datetime.now().isoformat(timespec='seconds')}] {msg}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass

import webview

FLASK_PORT = 5000
FLASK_URL  = f"http://localhost:{FLASK_PORT}"
LOCK_FILE  = ROOT / "flowshort.lock"


def _acquire_lock() -> bool:
    """Return True if this process is the first instance."""
    import os
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            # Check if that PID is still alive
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return False  # Already running
        except Exception:
            pass
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _start_flask() -> None:
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    from app import app
    app.run(port=FLASK_PORT, debug=False, threaded=True, use_reloader=False)


def _wait_for_flask(timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(FLASK_URL, timeout=1)
            return True
        except Exception:
            time.sleep(0.25)
    return False


def main() -> None:
    if not _acquire_lock():
        _log("Already running — ignoring second launch")
        return
    _log("=== FlowShort starting ===")
    try:
        # 1. Flask background thread (daemon → killed when window closes)
        _log("Starting Flask thread...")
        threading.Thread(target=_start_flask, daemon=True).start()

        # 2. Wait for server ready
        _log("Waiting for Flask...")
        if not _wait_for_flask():
            _log("ERROR: Flask timeout")
            try:
                import tkinter.messagebox as mb
                mb.showerror("FlowShort", "Le serveur Flask n'a pas démarré.")
            except Exception:
                pass
            sys.exit(1)

        _log("Flask OK — creating webview window...")
        # 3. Native window (Edge WebView2 on Windows 11)
        webview.create_window(
            title    = "FlowShort",
            url      = FLASK_URL,
            width    = 1280,
            height   = 820,
            min_size = (900, 600),
            resizable= True,
        )
        _log("webview.start() called")
        webview.start()
        _log("webview closed — exiting")
    except Exception:
        _log("EXCEPTION:\n" + traceback.format_exc())
        raise
    finally:
        _release_lock()


if __name__ == "__main__":
    main()
