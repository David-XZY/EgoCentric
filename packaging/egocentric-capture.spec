from pathlib import Path

import PyInstaller.utils.hooks as pyinstaller_hooks
from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from PyInstaller.utils.hooks import conda as conda_support

pyinstaller_hooks.conda_support = conda_support

root = Path(SPECPATH).parent
source = root / "src"

datas = [
    (
        str(source / "egocentric_capture" / "config" / "default.yaml"),
        "egocentric_capture/config",
    ),
    (
        str(source / "egocentric_capture" / "schemas" / "egocentric.proto"),
        "egocentric_capture/schemas",
    ),
    (
        str(source / "egocentric_capture" / "assets" / "preview_grid.qml"),
        "egocentric_capture/assets",
    ),
]
datas += collect_data_files("foxglove_schemas_protobuf")

hiddenimports = []
for package in (
    "google.protobuf",
    "mcap",
    "mcap_protobuf",
    "gi",
):
    hiddenimports += collect_submodules(package)
hiddenimports += [
    "av",
    "cairo",
    "cv2",
    "depthai",
    "egocentric_capture.gui",
    "foxglove_schemas_protobuf.CompressedVideo_pb2",
    "gi.repository.Gst",
    "gi.repository.GstApp",
    "gi.repository.GstGL",
    "pyarrow",
    "pyarrow._parquet",
    "pyarrow.parquet",
    "pyqtgraph",
    "serial.tools.list_ports",
]

analysis = Analysis(
    [str(root / "packaging" / "entrypoint.py")],
    pathex=[str(source)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "libemg",
        "mediapipe",
        "matplotlib",
        "onnxruntime",
        "pandas",
        "PIL",
        "pytest",
        "scipy",
        "sklearn",
        "tensorflow",
        "torch",
    ],
    noarchive=False,
)
python_archive = PYZ(analysis.pure)
executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="egocentric-capture",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="egocentric-capture",
)
