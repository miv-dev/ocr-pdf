"""
Local OCR server for the scan reader.

    pip install occular-ocr flask
    python server.py
    open http://localhost:8000

The browser renders each PDF page to a canvas and posts that PNG here.
We recognize it and send back line boxes in the same pixel coordinates,
so the text layer lines up with the picture exactly.
"""

import io
import os
import threading

from flask import Flask, request, jsonify, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", 32)) * 1024 * 1024

_pipe = None
_pipe_lang = None
_reading_order = False            # set once we know whether the model is available
_lock = threading.Lock()          # the models are not thread-safe

# What the front end shows while the first run pulls weights from HuggingFace.
# "idle" -> "downloading" -> "loading" -> "ready", or "error".
_model_state = "idle"
_model_error = None
_download_baseline = None         # cache size when the download started
DESKTOP = os.environ.get("OCCULAR_DESKTOP") == "1"

# What a first run pulls down: detector, recognizer, charset and the language
# model. Only used to draw a progress bar, so an estimate is good enough.
EXPECTED_DOWNLOAD_BYTES = 400 * 1024 * 1024


def _say(message):
    """
    print() that cannot raise.

    occular narrates in Russian, and a Windows console defaults to a code page
    that cannot encode Cyrillic — so an ordinary progress line raises
    UnicodeEncodeError there. Inside an except block that turns a recovery into
    a crash, which is how a missing optional model cost a reader their OCR.
    """
    try:
        print(message)
    except UnicodeEncodeError:
        try:
            print(message.encode("ascii", "backslashreplace").decode("ascii"))
        except Exception:                          # noqa: BLE001
            pass                                   # nowhere to say it; carry on
    except Exception:                              # noqa: BLE001
        pass


def _cache_bytes():
    """How much is in the HuggingFace cache right now, part-downloaded files included."""
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
    except Exception:                              # noqa: BLE001
        return 0
    total = 0
    for root, _dirs, files in os.walk(HF_HUB_CACHE):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass                               # a blob being renamed under us
    return total


def _download_progress():
    """Bytes fetched since this download started, and what we expect in total."""
    if _download_baseline is None:
        return None
    done = max(0, _cache_bytes() - _download_baseline)
    return {"downloaded": done, "total": max(EXPECTED_DOWNLOAD_BYTES, done)}


def _weights_present():
    """Are the detector/recognizer weights already on disk, locally or in the HF cache?"""
    try:
        from occular import model_files
    except Exception:                              # noqa: BLE001
        return False
    if any(w.parent.name != "reading_order" for w in model_files.WEIGHTS_DIR.rglob("*.onnx")):
        return True                                # shipped with the build
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
    except Exception:                              # noqa: BLE001
        return False
    repo = "models--" + model_files.WEIGHTS_HF_REPO.replace("/", "--")
    return os.path.isdir(os.path.join(HF_HUB_CACHE, repo))


def _try_reading_order():
    """The multi-column model is optional and downloads once. Fall back quietly."""
    if os.environ.get("READING_ORDER") == "0":
        return False
    try:
        from occular import download_reading_order
        download_reading_order()
        return True
    except Exception as exc:                       # noqa: BLE001
        _say(f"reading-order model unavailable ({exc}); using geometric column sort")
        return False


def order_lines(lines, page_width):
    """
    Sort lines into human reading order for multi-column pages.

    Group them into columns by the horizontal position of their centres —
    a gap wider than a sixth of the page starts a new column — then read
    each column top to bottom, left to right. This is what stops a two-page
    spread from copying out as interleaved half-sentences.
    """
    if len(lines) < 2:
        return lines

    centres = sorted(((l["x0"] + l["x1"]) / 2, i) for i, l in enumerate(lines))
    gap = page_width / 6.0

    columns, group = [], [centres[0]]
    for centre, idx in centres[1:]:
        if centre - group[-1][0] > gap:
            columns.append(group)
            group = []
        group.append((centre, idx))
    columns.append(group)

    ordered = []
    for group in columns:
        idxs = [idx for _, idx in group]
        idxs.sort(key=lambda i: lines[i]["y0"])
        ordered.extend(lines[i] for i in idxs)
    return ordered


def get_pipeline(languages):
    """Build the pipeline once and keep it warm; rebuild only if language changes."""
    global _pipe, _pipe_lang, _reading_order, _model_state, _model_error
    global _download_baseline

    key = tuple(languages) if languages else None
    if _pipe is None or _pipe_lang != key:
        # Say something before the first import: pulling occular and onnxruntime
        # into a frozen build takes the better part of a minute on a cold disk.
        _model_state = "loading"
        _model_error = None
        try:
            from occular import OCRPipeline, Settings

            if _weights_present():
                _model_state = "loading"
            else:
                _download_baseline = _cache_bytes()
                _model_state = "downloading"
            if _pipe is None:
                _reading_order = _try_reading_order()
            _say(f"loading models (languages={languages or 'ru+en'}) …")

            def build(with_reading_order):
                return OCRPipeline(Settings(
                    deskew=True,                 # straighten crooked scans
                    lm=True,                     # beam search + Russian language model
                    reading_order=with_reading_order,
                    languages=languages,
                ))

            try:
                _pipe = build(_reading_order)
            except Exception as exc:               # noqa: BLE001
                if not _reading_order:
                    raise
                # Downloading the layout model and loading it are separate steps
                # that fail separately — it can land on disk somewhere the loader
                # does not look. It is optional either way: order_lines() sorts
                # columns geometrically. Losing column order beats losing OCR.
                _say(f"pipeline with reading order failed ({exc}); retrying without it")
                _reading_order = False
                _pipe = build(False)
        except Exception as exc:                   # noqa: BLE001
            _model_state, _model_error = "error", str(exc)
            raise
        _pipe_lang = key
        print("ready")
    _model_state = "ready"
    return _pipe


def warm_up():
    """Load the default pipeline in the background so the first page isn't a long wait."""
    def run():
        try:
            with _lock:
                get_pipeline(None)
        except Exception:                          # noqa: BLE001
            app.logger.exception("model warm-up failed")

    threading.Thread(target=run, name="warm-up", daemon=True).start()


@app.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.route("/vendor/<path:name>")
def vendor(name):
    """pdf.js and friends, shipped with the app so it works with no internet."""
    return send_from_directory(os.path.join(HERE, "vendor"), name)


@app.route("/health")
def health():
    return jsonify({"ok": True, "desktop": DESKTOP})


@app.route("/models")
def models():
    """Where the weights are: idle | downloading | loading | ready | error."""
    body = {"state": _model_state, "error": _model_error, "desktop": DESKTOP}
    if _model_state == "downloading":
        body["progress"] = _download_progress()
    return jsonify(body)


@app.route("/ocr", methods=["POST"])
def ocr():
    """Body: raw PNG bytes. Query: ?lang=ru,en  ->  {"lines": [...]}"""
    data = request.get_data()
    if not data:
        return jsonify({"error": "empty request body"}), 400

    lang = request.args.get("lang", "").strip()
    if lang in ("", "ru+en", "default"):
        languages = None
    elif lang == "auto":
        languages = "auto"
    else:
        languages = [c.strip() for c in lang.split(",") if c.strip()]

    import numpy as np
    import cv2

    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jsonify({"error": "could not decode the image"}), 400

    try:
        with _lock:
            pipe = get_pipeline(languages)
            try:
                result = pipe.process_image(img)
            except (TypeError, AttributeError):
                # older builds want a path rather than an array
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp.write(data)
                    path = tmp.name
                try:
                    result = pipe.process_image(path)
                finally:
                    os.unlink(path)
    except Exception as exc:                       # noqa: BLE001
        app.logger.exception("recognition failed")
        return jsonify({"error": str(exc)}), 500

    lines = []
    for item in result:
        quad = item.get("quad") or []
        xs = [float(p[0]) for p in quad]
        ys = [float(p[1]) for p in quad]
        if not xs or not ys:
            continue
        lines.append({
            "text": item.get("text", ""),
            "confidence": float(item.get("confidence", 0)),
            "x0": min(xs), "y0": min(ys),
            "x1": max(xs), "y1": max(ys),
        })

    if not _reading_order:
        lines = order_lines(lines, img.shape[1])

    return jsonify({"lines": lines})


if __name__ == "__main__":
    _say("http://localhost:8000  —  models load in the background")
    warm_up()
    app.run(host="127.0.0.1", port=8000, threaded=True)
