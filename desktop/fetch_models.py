"""
Download the model weights into this machine's cache.

Run it before a build with OCCULAR_BUNDLE_WEIGHTS=1 so the weights end up
inside the package, or run it after installing to get the wait over with:

    python desktop/fetch_models.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    import numpy as np

    from occular import OCRPipeline, Settings, download_reading_order

    print("Fetching the reading-order model …")
    try:
        download_reading_order()
    except Exception as exc:                          # noqa: BLE001
        print(f"  skipped: {exc}")

    print("Fetching the detector, recognizer and language model …")
    pipe = OCRPipeline(Settings(deskew=True, lm=True, reading_order=False))

    # A real pass over a blank page, so every lazily loaded piece is pulled now.
    pipe.process_image(np.full((320, 320, 3), 255, dtype=np.uint8))

    print("Done. The weights are in the HuggingFace cache for this user.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
