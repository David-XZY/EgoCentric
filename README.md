# EgoCentric Capture

面向采集员的统一多模态原始数据采集应用。生产界面使用 Qt Quick/QML
数据驾驶舱，应用同时记录：

- OAK-4P-New 四路硬同步 H.264 视频
- OAK 板载 IMU
- 肌电手环 8 通道 EMG
- 肌电手环 IMU

驾驶舱会从主相机预览支路运行 MediaPipe 双手关键点和手势识别。该分析支路仅保留
最新帧，识别异常或积压不会阻塞原始采集、写盘或最终质检。权威数据仍保存为分段
MCAP，消息使用 Protobuf 描述；MP4、Parquet 和 NumPy 文件由独立后处理命令导出。

## 数据驾驶舱

- 相机 A 全屏主视角，相机 B/C/D 位于底部辅助视图
- 左右两组 4 通道实时 EMG 波形
- MediaPipe 双手骨架、标准手势、`PINCH` 和 `PRE-GRASP`
- 设备频率、同步偏差、真实写盘吞吐、存储和队列状态
- 左侧抽屉中的受试者、任务、采集手、操作人和高级设置
- 录制、质检、安全收尾和录制中退出确认

`--simulate` 会使用确定性的手部关键点动画。设置
`EGOCENTRIC_STATIC_DEMO=1` 时，离屏截图使用内置实验室图片作为背景；真机运行不会
使用该图片。

## 开发运行

开发机需要预装以下系统组件：

- `gstreamer1.0-qt6`
- `gstreamer1.0-plugins-base`
- `gstreamer1.0-plugins-good`
- `gstreamer1.0-plugins-bad`
- `gstreamer1.0-libav`
- `gstreamer1.0-gl`
- `gir1.2-gstreamer-1.0`
- `gir1.2-gst-plugins-base-1.0`
- `mesa-va-drivers`

从源码构建 PyGObject 还需要 `libcairo2-dev`、
`libgirepository-2.0-dev`、`python3-dev` 和 `pkg-config`。项目固定使用
`PySide6==6.10.2`，以匹配 Ubuntu `gstreamer1.0-qt6` 使用的 Qt ABI。

```bash
source .venv/bin/activate
pip install -e . --no-deps
egocentric-capture gui --simulate
```

无真机时使用 `--simulate` 验证完整采集、落盘、质检和界面流程。

## 构建与安装

```bash
./packaging/build_deb.sh
```

生成的 `.deb` 可通过 Ubuntu 软件中心或统一部署流程安装。安装后可从桌面应用列表
启动，也可运行：

```bash
egocentric-capture gui
```

安装包会部署到 `/opt/egocentric-capture`，同时安装
`/usr/bin/egocentric-capture`、桌面入口以及 OAK 和 CP210x 接收器的
`udev` 规则。

`build_deb.sh` 会在运行 PyInstaller 前检查 PySide6 版本、GI 绑定、
`qml6glsink`、GL 转换链以及 H.264 解码器，缺少任一组件都会立即失败并给出原因。

## 命令

```bash
egocentric-capture gui
egocentric-capture gui --simulate
egocentric-capture inspect <session>
egocentric-capture validate <session>
egocentric-capture recover <session>
egocentric-capture export <session> --format numpy
```

## Session 目录

```text
YYYYMMDD_HHMMSS_<participant>_<task>_rNN/
├── session.json
├── segments/
│   ├── 0000.mcap
│   └── 0001.mcap
├── checksums.sha256
└── logs/
    └── app.log
```

只有完成文件关闭、MCAP 校验、质量检查和校验和生成后，`session.json` 才会被标记
为 `completed`。设备掉线、序号断裂或写盘异常都会保留失败目录并标记为
`failed`；预览异常会明确告警和写入系统事件，但权威原始录制继续。

## 当前验收状态

模拟设备的软件链路已覆盖四路 H.264、双 IMU、EMG、有界且满载即失败的 Writer、
单遍 MCAP CRC/计数/SHA-256、截断恢复、校验和、MP4/Parquet/NumPy 导出和离屏
GUI 启停。真机 20 次启停、60 分钟长录制、设备拔插、磁盘不足、进程强杀以及
干净 Ubuntu 安装验收需要在独占 OAK 和手环的机器上执行。
