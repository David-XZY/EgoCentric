#!/usr/bin/env bash
set -euo pipefail

# 从项目根目录执行，生成自带 Python 3.12 运行时的 amd64 安装包。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${VERSION:-0.2.0}"
ARCH="${ARCH:-$(dpkg --print-architecture)}"
VENV_PYTHON="${VENV_PYTHON:-$ROOT/.venv/bin/python}"
BUILD_ROOT="$ROOT/build/deb"
PACKAGE_ROOT="$BUILD_ROOT/egocentric-capture_${VERSION}_${ARCH}"
DIST_ROOT="$ROOT/dist/egocentric-capture"

if [[ "$ARCH" != "amd64" ]]; then
    echo "当前构建仅支持 amd64，实际架构为 $ARCH" >&2
    exit 2
fi

# 让 PyInstaller 的 GI hooks 能解析系统 GStreamer typelib。
SYSTEM_GI_TYPELIB_PATH="/usr/lib/x86_64-linux-gnu/girepository-1.0"
if [[ -d "$SYSTEM_GI_TYPELIB_PATH" ]]; then
    export GI_TYPELIB_PATH="$SYSTEM_GI_TYPELIB_PATH${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
fi

"$VENV_PYTHON" "$ROOT/packaging/check_preview_runtime.py"

if [[ "${SKIP_PYINSTALLER:-0}" != "1" ]]; then
    QT_QPA_PLATFORM=offscreen "$VENV_PYTHON" -m PyInstaller \
        --noconfirm \
        --clean \
        --distpath "$ROOT/dist" \
        --workpath "$ROOT/build/pyinstaller" \
        "$ROOT/packaging/egocentric-capture.spec"
fi

# conda 的 NumPy 让 BLAS 与 LAPACK 指向同一 OpenBLAS 实现，补齐运行时别名。
if [[ ! -e "$DIST_ROOT/_internal/liblapack.so.3" ]]; then
    ln -s libblas.so.3 "$DIST_ROOT/_internal/liblapack.so.3"
fi

rm -rf "$PACKAGE_ROOT"
mkdir -p \
    "$PACKAGE_ROOT/DEBIAN" \
    "$PACKAGE_ROOT/opt/egocentric-capture" \
    "$PACKAGE_ROOT/usr/bin" \
    "$PACKAGE_ROOT/usr/share/applications" \
    "$PACKAGE_ROOT/lib/udev/rules.d"

cp -a "$DIST_ROOT/." "$PACKAGE_ROOT/opt/egocentric-capture/"
install -m 0755 "$ROOT/packaging/launcher.sh" \
    "$PACKAGE_ROOT/usr/bin/egocentric-capture"
install -m 0644 "$ROOT/packaging/egocentric-capture.desktop" \
    "$PACKAGE_ROOT/usr/share/applications/egocentric-capture.desktop"
install -m 0644 "$ROOT/packaging/99-egocentric-capture.rules" \
    "$PACKAGE_ROOT/lib/udev/rules.d/99-egocentric-capture.rules"
install -m 0755 "$ROOT/packaging/postinst" "$PACKAGE_ROOT/DEBIAN/postinst"
install -m 0755 "$ROOT/packaging/postrm" "$PACKAGE_ROOT/DEBIAN/postrm"
chmod 0755 "$PACKAGE_ROOT/DEBIAN"
chmod -R a+rX "$PACKAGE_ROOT"

INSTALLED_SIZE="$(du -sk "$PACKAGE_ROOT/opt" | cut -f1)"
cat >"$PACKAGE_ROOT/DEBIAN/control" <<EOF
Package: egocentric-capture
Version: $VERSION
Section: science
Priority: optional
Architecture: $ARCH
Maintainer: EgoCentric Capture Team
Installed-Size: $INSTALLED_SIZE
Depends: libc6, libstdc++6, libgl1, libglib2.0-0, libusb-1.0-0, libxcb-cursor0, libcairo2, libgirepository-2.0-0, gstreamer1.0-qt6, gstreamer1.0-plugins-base, gstreamer1.0-plugins-good, gstreamer1.0-plugins-bad, gstreamer1.0-libav, gstreamer1.0-gl, gir1.2-gstreamer-1.0, gir1.2-gst-plugins-base-1.0, mesa-va-drivers, libva2
Description: 统一多模态原始数据采集系统
 同步采集四路 OAK H.264 视频、OAK IMU、8 通道 EMG 和手环 IMU，
 使用 QML 数据驾驶舱、MediaPipe 手势识别、分段 MCAP 与 Protobuf。
EOF

mkdir -p "$ROOT/dist"
dpkg-deb --root-owner-group -Zzstd -z3 --build "$PACKAGE_ROOT" \
    "$ROOT/dist/egocentric-capture_${VERSION}_${ARCH}.deb"
echo "$ROOT/dist/egocentric-capture_${VERSION}_${ARCH}.deb"
