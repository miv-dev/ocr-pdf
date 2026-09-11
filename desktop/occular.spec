# PyInstaller spec — build with:  pyinstaller desktop/occular.spec
#
# Produces a one-folder app (dist/Occular/). One-folder rather than one-file:
# the ONNX runtime and OpenCV are hundreds of megabytes, and a one-file build
# unpacks all of it to a temp directory on every single launch.
#
# Set OCCULAR_BUNDLE_WEIGHTS=1 to ship the model weights inside the build
# (about 1.4 GB, but then the app needs no internet at all, ever).

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs

ROOT = Path(SPECPATH).resolve().parent
BUNDLE_WEIGHTS = os.environ.get("OCCULAR_BUNDLE_WEIGHTS") == "1"

# server.py is imported as a module, so PyInstaller freezes it into the archive
# on its own — shipping a second copy as data would only let the two drift apart.
datas = [
    (str(ROOT / "index.html"), "."),
    (str(ROOT / "vendor"), "vendor"),
]

# occular ships JSON configs and charsets next to its code.
datas += collect_data_files("occular", include_py_files=False)

if BUNDLE_WEIGHTS:
    import occular
    weights = Path(occular.__file__).parent / "weights"
    if weights.exists():
        datas.append((str(weights), "occular/weights"))
    from huggingface_hub.constants import HF_HUB_CACHE
    hub = Path(HF_HUB_CACHE)
    if hub.exists():
        datas.append((str(hub), "prefetched_hub"))

binaries = collect_dynamic_libs("onnxruntime") + collect_dynamic_libs("cv2")

hiddenimports = [
    "occular",
    "onnxruntime",
    "onnxruntime.capi._pybind_state",
    "pyclipper",
    "pyctcdecode",
    "pygtrie",
    "huggingface_hub",
    "cv2",
    "PIL.Image",
    "webview",
]
if sys.platform == "win32":
    # pywebview draws its window with WinForms through pythonnet, so the build
    # needs the .NET side of that bridge: Python.Runtime.dll (pythonnet), the
    # ClrLoader shims (clr_loader) and the WebView2 assemblies under
    # webview/lib. All of those are package *data* — listing the packages as
    # hidden imports pulls in the Python wrappers and none of the DLLs, which
    # is what "failed to resolve Python.Runtime.Loader.Initialize" means.
    for _pkg in ("pythonnet", "clr_loader"):
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h

    datas += collect_data_files("webview")          # webview/lib/*.dll
    hiddenimports += [
        "webview.platforms.winforms",               # the real backend module
        "webview.platforms.edgechromium",
        "clr",
        "_cffi_backend",                            # clr_loader.ffi is cffi-based
    ]
elif sys.platform == "darwin":
    hiddenimports += ["webview.platforms.cocoa"]
else:
    hiddenimports += ["webview.platforms.gtk", "webview.platforms.qt"]

excludes = [
    "tkinter", "matplotlib", "pytest", "hypothesis", "IPython",
    "notebook", "sympy", "setuptools", "pip",
]

a = Analysis(
    [str(ROOT / "desktop" / "app.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

icon = ROOT / "desktop" / ("icon.ico" if sys.platform == "win32" else "icon.icns")

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="Occular",
    console=False,                 # no terminal window behind the app
    icon=str(icon) if icon.exists() else None,
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,                     # UPX corrupts onnxruntime's DLLs
    name="Occular",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Occular.app",
        icon=str(icon) if icon.exists() else None,
        bundle_identifier="dev.occular.scanreader",
        info_plist={
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "CFBundleShortVersionString": "1.0.4",
        },
    )
