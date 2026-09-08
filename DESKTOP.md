# The desktop app

The same reader, but recognition runs on the machine in front of you instead of
your server. No page images travel over the network, there is nothing to keep
running, and a slow VPS stops being the bottleneck.

It is the *same code*: `desktop/app.py` starts the Flask app from `server.py` on
a random loopback port and shows `index.html` in a native window.

| Platform | Window is drawn by | Needs |
| -------- | ------------------ | ----- |
| Windows 10/11 | WebView2 (Edge) | Ships with Windows 10 21H2 and later; older builds need the [WebView2 runtime](https://developer.microsoft.com/microsoft-edge/webview2/) |
| macOS 11+ | WKWebView | Nothing |
| Linux | WebKitGTK | `gir1.2-webkit2-4.0`, `python3-gi` |

## Running it from source

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-desktop.txt     # .venv\Scripts\pip on Windows
.venv/bin/python desktop/app.py
```

If pywebview is missing or its toolkit won't load, the launcher falls back to
opening the page in your default browser — the app still works.

## Building an installable app

```bash
pip install -r requirements-desktop.txt
pyinstaller --noconfirm desktop/occular.spec
```

The result is a one-folder build in `dist/` — `dist/Occular/Occular.exe` on
Windows, `dist/Occular.app` on macOS, `dist/Occular/Occular` on Linux. One
folder rather than one file on purpose: ONNX Runtime and OpenCV are hundreds of
megabytes, and a one-file build unpacks all of it to a temp directory on every
launch.

**PyInstaller does not cross-compile.** A Windows `.exe` has to be built on
Windows. If you don't have a Windows machine, push a `v*` tag and let
`.github/workflows/desktop.yml` build all three from a clean runner, or run it
by hand from the Actions tab.

### With or without the weights inside

By default the build is about **700 MB** and downloads roughly 400 MB of model
weights from HuggingFace the first time it runs. The window shows the progress
and it only ever happens once.

To ship the weights inside the build instead — about **1.4 GB**, and then the
app never needs the internet at all:

```bash
python desktop/fetch_models.py            # pull the weights into this machine's cache
OCCULAR_BUNDLE_WEIGHTS=1 pyinstaller --noconfirm desktop/occular.spec
```

The CI workflow exposes this as the `bundle_weights` input on manual runs.

Offline builds are worth it if the people using this are on locked-down
machines. Otherwise the smaller download is the friendlier default.

## Where it keeps things

Model weights and caches go in a per-user folder, never inside the app — a
frozen bundle is read-only, and on one-file builds its directory is a temp dir
that disappears:

| Platform | Folder |
| -------- | ------ |
| Windows | `%LOCALAPPDATA%\Occular\` |
| macOS | `~/Library/Application Support/Occular/` |
| Linux | `~/.local/share/occular/` |

Override with `OCCULAR_DATA_DIR`. Deleting that folder resets the app; the next
launch re-downloads the weights.

## Signing

Unsigned builds get a warning on both desktop platforms — SmartScreen's "Windows
protected your PC", and Gatekeeper's "cannot be opened because the developer
cannot be verified". Neither blocks a determined user (More info → Run anyway;
right-click → Open), but if this goes to people who aren't you, sign it:

- **Windows** — an Authenticode certificate, then `signtool sign /fd sha256`.
  SmartScreen reputation still takes a while to build for a new certificate.
- **macOS** — a Developer ID certificate, `codesign --deep --options runtime`,
  then `notarytool submit` and `stapler staple`.

## Environment variables

| Variable | Effect |
| -------- | ------ |
| `OCCULAR_DATA_DIR` | Where weights and caches go |
| `OCCULAR_PORT` | Fix the local port instead of picking a free one |
| `OMP_NUM_THREADS` | Recognition threads; defaults to cores − 1 |
| `READING_ORDER=0` | Skip the multi-column layout model, use the geometric sort |
| `MAX_UPLOAD_MB` | Largest page image accepted (default 32) |
