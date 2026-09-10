"""
Checks that need no network and no model weights.

Run from the repository root:

    python desktop/test_desktop.py

Both cases here are shipped bugs. The build's smoke test cannot catch them: it
would have to download 400 MB of weights before anything is loaded, so it stops
at /health. These run in a second instead.
"""

import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "desktop"))


def test_reading_order_paths_agree():
    """
    The folder we download the layout model to must be the one we load it from.

    occular holds that path twice, and redirecting only the download sends the
    model somewhere the loader never looks — which is how a Windows build got
    "Модель порядка чтения не найдена в ...\\_internal\\occular\\weights".
    """
    import app
    from occular import model_files, reading_order

    # If either name moves, the redirect below would quietly create a new
    # attribute nobody reads, so check them before touching anything.
    assert hasattr(model_files, "READING_ORDER_DIR"), "occular renamed the download path"
    assert hasattr(reading_order, "_RO_DIR"), "occular renamed the load path"

    # A frozen bundle: no weights in the package folder, and it is read-only.
    bundle = Path(tempfile.mkdtemp()) / "_internal" / "occular" / "weights" / "reading_order"
    model_files.READING_ORDER_DIR = reading_order._RO_DIR = bundle

    data = Path(tempfile.mkdtemp())
    app.redirect_weights(data)

    target = data / "models" / "reading_order"
    assert model_files.READING_ORDER_DIR == target, "download path not redirected"
    assert reading_order._RO_DIR == target, "load path not redirected"
    assert target.is_dir()

    # Weights shipped inside the build are used where they lie.
    shipped = Path(tempfile.mkdtemp())
    (shipped / "encoder.onnx").write_bytes(b"")
    model_files.READING_ORDER_DIR = reading_order._RO_DIR = shipped
    app.redirect_weights(Path(tempfile.mkdtemp()))
    assert model_files.READING_ORDER_DIR == shipped, "clobbered the bundled weights"
    assert reading_order._RO_DIR == shipped


def test_reading_order_failure_is_not_fatal():
    """
    A missing layout model costs column order, not recognition.

    It is optional — order_lines() sorts columns geometrically without it — but
    the pipeline builds it eagerly, so its failure used to take the whole app
    down and leave the reader with no OCR at all.
    """
    attempts = []

    class Settings:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    def OCRPipeline(settings):
        attempts.append(settings.reading_order)
        if settings.reading_order:
            raise FileNotFoundError("Модель порядка чтения не найдена")
        return types.SimpleNamespace(settings=settings)

    stub = types.ModuleType("occular")
    stub.OCRPipeline = OCRPipeline
    stub.Settings = Settings
    stub.download_reading_order = lambda: "ok"     # downloads fine, loads badly
    sys.modules["occular"] = stub                  # must come after the test above

    import server
    pipe = server.get_pipeline(None)

    assert attempts == [True, False], f"expected a retry without it, got {attempts}"
    assert pipe is not None
    assert server._model_state == "ready", server._model_state
    assert server._model_error is None
    assert server._reading_order is False

    # Anything that is not about reading order must still be reported.
    server._pipe = server._pipe_lang = None

    def broken(settings):
        raise RuntimeError("detector weights are corrupt")

    stub.OCRPipeline = broken
    try:
        server.get_pipeline(None)
    except RuntimeError:
        assert server._model_state == "error"
        assert "corrupt" in server._model_error
    else:
        raise AssertionError("a real failure was swallowed")


if __name__ == "__main__":
    # Ordered: the second test replaces occular with a stub for good.
    for test in (test_reading_order_paths_agree, test_reading_order_failure_is_not_fatal):
        test()
        print(f"ok  {test.__name__}")
    print("\nall good")
