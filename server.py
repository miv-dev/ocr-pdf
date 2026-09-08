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

_pipe = None
_pipe_lang = None
_reading_order = False            # set once we know whether the model is available
_lock = threading.Lock()          # the models are not thread-safe


def _try_reading_order():
    """The multi-column model is optional and downloads once. Fall back quietly."""
    if os.environ.get("READING_ORDER") == "0":
        return False
    try:
        from occular import download_reading_order
        download_reading_order()
        return True
    except Exception as exc:                       # noqa: BLE001
        print(f"reading-order model unavailable ({exc}); using geometric column sort")
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
    global _pipe, _pipe_lang, _reading_order
    from occular import OCRPipeline, Settings

    key = tuple(languages) if languages else None
    if _pipe is None or _pipe_lang != key:
        if _pipe is None:
            _reading_order = _try_reading_order()
        print(f"loading models (languages={languages or 'ru+en'}) …")
        _pipe = OCRPipeline(Settings(
            deskew=True,                    # straighten crooked scans
            lm=True,                        # beam search + Russian language model
            reading_order=_reading_order,   # order lines across columns
            languages=languages,
        ))
        _pipe_lang = key
        print("ready")
    return _pipe


@app.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.route("/health")
def health():
    return jsonify({"ok": True})


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
    print("http://localhost:8000  —  models load on the first page")
    app.run(host="127.0.0.1", port=8000, threaded=True)
