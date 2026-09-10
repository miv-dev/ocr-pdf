"""
Desktop launcher for the scan reader.

Runs the same Flask app as the web version, but bound to a random port on the
loopback interface and shown in a native window. Nothing leaves the machine:
the pages you open are recognized by the copy of the models on this computer.

    python desktop/app.py

Frozen builds (PyInstaller) enter through the same main().
"""

import logging
import os
import socket
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_NAME = "Occular"


# --------------------------------------------------------------------------
# Where things live
# --------------------------------------------------------------------------

def resource_dir() -> Path:
    """The folder holding index.html and vendor/ — inside the bundle when frozen."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """A writable per-user folder for model weights and caches."""
    override = os.environ.get("OCCULAR_DATA_DIR")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME.lower()
    base.mkdir(parents=True, exist_ok=True)
    return base


def prepare_environment() -> Path:
    """
    Point every cache at the user's data folder, before occular is imported.

    A frozen app's package directory is read-only (and on one-file builds it is a
    temp dir that disappears), so the library's default of writing weights next to
    its own source would either fail or re-download on every launch.
    """
    data = data_dir()
    os.environ.setdefault("HF_HOME", str(data / "models"))
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("XDG_CACHE_HOME", str(data / "cache"))
    os.environ["OCCULAR_DESKTOP"] = "1"

    # One recognition at a time, so leave the machine usable while it works.
    os.environ.setdefault("OMP_NUM_THREADS", str(max(1, (os.cpu_count() or 4) - 1)))

    for sub in ("models", "cache", "logs"):
        (data / sub).mkdir(parents=True, exist_ok=True)

    seed_bundled_weights(data)
    return data


def seed_bundled_weights(data: Path) -> None:
    """
    Copy weights shipped inside the build into the user's cache on first run.

    Builds made with OCCULAR_BUNDLE_WEIGHTS=1 carry a prefetched HuggingFace
    cache. It lives inside the (read-only) bundle, so copy it out once rather
    than pointing the library at a directory it cannot write lock files into.
    """
    source = resource_dir() / "prefetched_hub"
    if not source.is_dir():
        return
    target = Path(os.environ["HF_HOME"]) / "hub"
    if target.exists() and any(target.iterdir()):
        return
    import shutil
    print("First run: unpacking the bundled models…")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)


def start_logging(data: Path) -> Path:
    """
    Write everything to a file under the data folder.

    A windowed build has no console attached, so anything printed to stdout is
    lost — including the traceback you actually want when a user reports that
    nothing happens.
    """
    log_path = data / "logs" / "occular.log"
    handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])

    if getattr(sys, "frozen", False) and not sys.stdout:
        # PyInstaller's windowed builds hand us None for the standard streams;
        # a stray print() would then raise. Send them to the log instead.
        stream = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = stream

    return log_path


def redirect_weights(data: Path) -> None:
    """
    Send the optional reading-order weights to the writable data folder too.

    occular keeps that path in two places: model_files.READING_ORDER_DIR, which
    download_reading_order() writes to, and reading_order._RO_DIR, which the
    loader reads from. Both default to the package directory, which is read-only
    in a frozen build — so patching one and not the other downloads the model to
    somewhere the code that needs it will never look, which is what
    "Модель порядка чтения не найдена в ...\\_internal\\occular\\weights" meant.

    Both are module globals read at call time, so rebinding them here is enough.
    If a future occular renames either one, get_pipeline() falls back to the
    geometric column sort rather than failing.
    """
    try:
        from occular import model_files, reading_order
    except Exception:                                  # noqa: BLE001
        return
    if any(model_files.READING_ORDER_DIR.glob("*.onnx")):
        return                                         # shipped inside the build
    target = data / "models" / "reading_order"
    target.mkdir(parents=True, exist_ok=True)
    model_files.READING_ORDER_DIR = target
    reading_order._RO_DIR = target
    logging.info("reading-order weights redirected to %s", target)


# --------------------------------------------------------------------------
# The server
# --------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(port: int):
    """Run Flask in a daemon thread and return the server module."""
    root = resource_dir()
    sys.path.insert(0, str(root))
    import server                                       # the web app, unchanged

    server.HERE = str(root)                             # index.html and vendor/ live here

    from werkzeug.serving import make_server
    httpd = make_server("127.0.0.1", port, server.app, threaded=True)
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()

    redirect_weights(data_dir())
    server.warm_up()                                    # models load while you pick a file
    return server


def wait_until_up(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.4):
                return True
        except OSError:
            time.sleep(0.1)
    return False


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------

WINDOW_TITLE = "Selectable scan"
WINDOW_SIZE = (1280, 880)


def native_window(url: str) -> bool:
    """
    Show the page in an OS window via pywebview. True if it ran.

    Every failure here is caught, not just a missing package: on Windows the
    window is drawn by WinForms through pythonnet, and a bundle missing its
    .NET assemblies raises from deep inside the loader rather than at import.
    An unhandled error there kills the app instead of falling back, which is
    the worst of the options available to us.
    """
    try:
        import webview
    except Exception:                                  # noqa: BLE001
        logging.info("pywebview unavailable", exc_info=True)
        return False

    try:
        webview.create_window(
            WINDOW_TITLE,
            url,
            width=WINDOW_SIZE[0],
            height=WINDOW_SIZE[1],
            min_size=(900, 600),
            text_select=True,      # the whole point of the app is selecting text
        )
        webview.start()            # blocks until the window closes
        return True
    except Exception:                                  # noqa: BLE001
        logging.exception("the native window failed to start; falling back")
        return False


def find_chromium() -> str | None:
    """A Chrome or Edge binary we can borrow a window from."""
    if sys.platform == "win32":
        roots = [os.environ.get(v) for v in
                 ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        names = [
            r"Microsoft\Edge\Application\msedge.exe",
            r"Google\Chrome\Application\chrome.exe",
        ]
        for root in filter(None, roots):
            for name in names:
                path = Path(root) / name
                if path.exists():
                    return str(path)
        return None

    if sys.platform == "darwin":
        for path in (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ):
            if Path(path).exists():
                return path
        return None

    from shutil import which
    for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
        found = which(name)
        if found:
            return found
    return None


def browser_app_window(url: str, data: Path) -> bool:
    """
    A chromeless Chrome/Edge window — the fallback that still feels like an app.

    The private profile directory is not optional: without it the browser hands
    the URL to an already-running instance and exits immediately, so there is
    nothing left to wait on and the app would quit while the window is open.
    """
    exe = find_chromium()
    if not exe:
        return False

    import subprocess
    profile = data / "window-profile"
    profile.mkdir(parents=True, exist_ok=True)
    logging.info("using %s in app mode", exe)
    try:
        subprocess.run([
            exe,
            f"--app={url}",
            f"--user-data-dir={profile}",
            f"--window-size={WINDOW_SIZE[0]},{WINDOW_SIZE[1]}",
            "--no-first-run",
            "--no-default-browser-check",
        ])
        return True
    except Exception:                                  # noqa: BLE001
        logging.exception("could not open a browser window")
        return False


def open_window(url: str, data: Path) -> None:
    """Native window, else a chromeless browser window, else a plain tab."""
    if native_window(url):
        return
    if browser_app_window(url, data):
        return

    import webbrowser
    print(f"Opening {url} in your browser instead. Close this window to quit.")
    webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main() -> int:
    data = prepare_environment()
    log_path = start_logging(data)
    logging.info("%s starting; data folder %s", APP_NAME, data)
    port = int(os.environ.get("OCCULAR_PORT", 0)) or free_port()
    start_server(port)

    if not wait_until_up(port):
        logging.error("the recognition engine did not start")
        print(f"The recognition engine did not start. See {log_path}", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{port}/"
    if os.environ.get("OCCULAR_NO_WINDOW") == "1":
        # Serve without a window: used by the build's smoke test, and handy
        # when you want to point another browser at it.
        print(f"Serving {url} — no window requested.")
        logging.info("running headless on %s", url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        return 0

    open_window(url, data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
