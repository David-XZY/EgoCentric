from __future__ import annotations

import os
from pathlib import Path

import PySide6

HARDWARE_DECODER_CANDIDATES = (
    "vah264dec",
    "vaapih264dec",
    "v4l2slh264dec",
    "v4l2h264dec",
    "nvh264dec",
)
SOFTWARE_DECODER = "avdec_h264"


def select_decoder(Gst: object) -> str | None:
    for index, name in enumerate(
        (*HARDWARE_DECODER_CANDIDATES, SOFTWARE_DECODER)
    ):
        if Gst.ElementFactory.find(name) is None:
            continue
        probe = Gst.ElementFactory.make(name, f"decoder-probe-{index}")
        if probe is None:
            continue
        result = probe.set_state(Gst.State.READY)
        probe.set_state(Gst.State.NULL)
        if result != Gst.StateChangeReturn.FAILURE:
            return name
    return None


def main() -> int:
    if PySide6.__version__ != "6.10.2":
        raise RuntimeError(
            f"PySide6 必须为 6.10.2，当前为 {PySide6.__version__}"
        )
    candidates = [
        Path("/usr/lib/x86_64-linux-gnu/girepository-1.0"),
        Path("/usr/lib/girepository-1.0"),
    ]
    existing = [str(path) for path in candidates if path.is_dir()]
    configured = os.environ.get("GI_TYPELIB_PATH", "")
    os.environ["GI_TYPELIB_PATH"] = ":".join(
        [*existing, *([configured] if configured else [])]
    )
    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError) as exc:
        raise RuntimeError("缺少 PyGObject/GStreamer GI 绑定") from exc
    Gst.init(None)
    missing = [
        name
        for name in (
            "appsrc",
            "h264parse",
            "glupload",
            "glcolorconvert",
            "qml6glsink",
        )
        if Gst.ElementFactory.find(name) is None
    ]
    if missing:
        raise RuntimeError(
            "缺少 GStreamer 预览元素: " + ", ".join(missing)
        )
    decoder = select_decoder(Gst)
    if decoder is None:
        raise RuntimeError("缺少可用的 GStreamer H.264 解码器")
    print(
        f"预览运行时检查通过: Qt {PySide6.__version__}, decoder={decoder}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
