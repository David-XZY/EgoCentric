from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CameraStream:
    socket: str
    name: str
    queue: Any


@dataclass(frozen=True)
class EncodedCameraStream:
    socket: str
    name: str
    queue: Any
    codec: str
    file_extension: str
    fps: int


@dataclass(frozen=True)
class ImuStream:
    queue: Any


H26X_ENCODER_LIMIT_MPIX_PER_SEC = 248.0
MJPEG_ENCODER_LIMIT_MPIX_PER_SEC = 450.0
ENCODER_BUDGET_TOLERANCE_RATIO = 0.01


def import_depthai() -> Any:
    try:
        import depthai as dai
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "未安装 depthai。请先执行: conda env create -f envs/oak-depthai.yml && conda activate oak-depthai"
        ) from exc
    return dai


def enum_value(enum_obj: Any, name: str) -> Any:
    if hasattr(enum_obj, name):
        return getattr(enum_obj, name)
    try:
        return enum_obj[name]
    except Exception as exc:
        raise ValueError(f"DepthAI 当前版本不支持枚举值: {name}") from exc


def safe_call(obj: Any, method: str, *args: Any) -> Any:
    fn = getattr(obj, method, None)
    if not callable(fn):
        return None
    try:
        return fn(*args)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def to_jsonable(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(key): to_jsonable(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item, depth + 1) for item in value]
    if hasattr(value, "name") and hasattr(value, "value"):
        return {"name": str(value.name), "value": to_jsonable(value.value, depth + 1)}
    if hasattr(value, "__dict__"):
        public = {
            key: item
            for key, item in vars(value).items()
            if not key.startswith("_") and not callable(item)
        }
        if public:
            return {key: to_jsonable(item, depth + 1) for key, item in public.items()}
    return str(value)


def timestamp_ns(value: Any) -> int | None:
    if value is None:
        return None
    for method in ("getTimestampDevice", "getTimestamp"):
        fn = getattr(value, method, None)
        if callable(fn):
            try:
                return timestamp_ns(fn())
            except Exception:
                pass
    if hasattr(value, "total_seconds"):
        return int(value.total_seconds() * 1_000_000_000)
    if isinstance(value, (int, float)):
        return int(value)
    return None


def device_timestamp_ns(value: Any) -> int | None:
    if value is None:
        return None
    fn = getattr(value, "getTimestampDevice", None)
    if not callable(fn):
        return None
    try:
        return timestamp_ns(fn())
    except Exception:
        return None


def host_timestamp_ns(message: Any) -> int | None:
    fn = getattr(message, "getTimestamp", None)
    if callable(fn):
        try:
            return timestamp_ns(fn())
        except Exception:
            return None
    return None


def sequence_num(message: Any) -> int | None:
    fn = getattr(message, "getSequenceNum", None)
    if callable(fn):
        try:
            return int(fn())
        except Exception:
            return None
    return None


def frame_to_cv2(message: Any) -> Any:
    fn = getattr(message, "getCvFrame", None)
    if callable(fn):
        return fn()
    fn = getattr(message, "getFrame", None)
    if callable(fn):
        return fn()
    raise RuntimeError("DepthAI 帧对象不支持 getCvFrame/getFrame")


def get_message_nowait(queue: Any) -> Any:
    for method in ("tryGet", "try_get"):
        fn = getattr(queue, method, None)
        if callable(fn):
            return fn()
    fn = getattr(queue, "get", None)
    if callable(fn):
        try:
            return fn(block=False)
        except TypeError:
            return None
    return None


def get_queue_output(
    output: Any,
    queue_size: int,
    *,
    blocking: bool = True,
) -> Any:
    try:
        return output.createOutputQueue(
            maxSize=queue_size,
            blocking=blocking,
        )
    except AttributeError as exc:
        raise RuntimeError(
            "DepthAI 输出对象不支持 createOutputQueue，当前脚本需要 DepthAI v3"
        ) from exc


def get_messages_any(
    dai: Any,
    queues: dict[str, Any],
    *,
    timeout_s: float = 1.0,
) -> dict[str, Any]:
    owner = getattr(dai, "MessageQueue", None)
    get_any = getattr(owner, "getAny", None)
    if callable(get_any):
        return dict(get_any(queues, timedelta(seconds=timeout_s)))
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        messages = {
            key: message
            for key, item_queue in queues.items()
            if (message := get_message_nowait(item_queue)) is not None
        }
        if messages:
            return messages
        time.sleep(0.001)
    return {}


def close_message_queue(message_queue: Any) -> None:
    close = getattr(message_queue, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def normalize_encoder_codec(codec: str) -> str:
    normalized = (
        codec.strip().upper().replace("-", "").replace(".", "").replace(" ", "_")
    )
    aliases = {
        "JPG": "MJPEG",
        "JPEG": "MJPEG",
        "MJP": "MJPEG",
        "MJPEG": "MJPEG",
        "H264": "H264_MAIN",
        "H264_MAIN": "H264_MAIN",
        "AVC": "H264_MAIN",
        "H265": "H265_MAIN",
        "H265_MAIN": "H265_MAIN",
        "HEVC": "H265_MAIN",
    }
    if normalized not in aliases:
        known = ", ".join(sorted({"MJPEG", "H264", "H265", "H264_MAIN", "H265_MAIN"}))
        raise ValueError(f"未知编码格式 {codec!r}，可用值: {known}")
    return aliases[normalized]


def encoded_stream_extension(codec: str) -> str:
    profile = normalize_encoder_codec(codec)
    if profile == "MJPEG":
        return "mjpeg"
    if profile == "H264_MAIN":
        return "h264"
    if profile == "H265_MAIN":
        return "h265"
    raise ValueError(f"未知编码 profile: {profile}")


def encoder_pixels_per_second(
    width: int, height: int, fps: int, stream_count: int = 1
) -> float:
    return float(width) * float(height) * float(fps) * float(stream_count) / 1_000_000


def encoder_codec_limit_mpix(codec: str) -> float:
    profile = normalize_encoder_codec(codec)
    if profile == "MJPEG":
        return MJPEG_ENCODER_LIMIT_MPIX_PER_SEC
    return H26X_ENCODER_LIMIT_MPIX_PER_SEC


def resolve_encoder_codec(
    codec: str, width: int, height: int, fps: int, stream_count: int
) -> str:
    if codec.strip().upper() != "AUTO":
        return normalize_encoder_codec(codec)
    mpix = encoder_pixels_per_second(width, height, fps, stream_count)
    if mpix <= H26X_ENCODER_LIMIT_MPIX_PER_SEC * (1 + ENCODER_BUDGET_TOLERANCE_RATIO):
        return "H265_MAIN"
    return "MJPEG"


def validate_encoder_mpix_budget(
    codec: str, mpix: float, description: str = "编码配置"
) -> str | None:
    profile = normalize_encoder_codec(codec)
    limit = encoder_codec_limit_mpix(profile)
    if mpix <= limit:
        return None
    tolerated_limit = limit * (1 + ENCODER_BUDGET_TOLERANCE_RATIO)
    if mpix <= tolerated_limit:
        return (
            f"警告: {description}需要 {mpix:.3f} MPix/s，略高于 {profile} 约 {limit:.0f} MPix/s "
            f"的标称预算，但处于 {ENCODER_BUDGET_TOLERANCE_RATIO:.0%} 边界容差内"
        )
    raise ValueError(
        f"{description}需要 {mpix:.2f} MPix/s，"
        f"超过该编码器约 {limit:.0f} MPix/s 的预算；请减少路数/分辨率/FPS，或改用更合适的编码格式"
    )


def validate_encoder_budget(
    codec: str,
    width: int,
    height: int,
    fps: int,
    stream_count: int,
) -> str | None:
    mpix = encoder_pixels_per_second(width, height, fps, stream_count)
    return validate_encoder_mpix_budget(
        codec,
        mpix,
        f"{stream_count} 路 {width}x{height}@{fps} {normalize_encoder_codec(codec)} ",
    )


def request_camera_output(
    camera: Any, dai: Any, width: int, height: int, fps: int
) -> Any:
    return request_camera_output_typed(camera, dai, width, height, fps, None)


def request_camera_output_typed(
    camera: Any,
    dai: Any,
    width: int,
    height: int,
    fps: int,
    output_type: str | None = None,
) -> Any:
    if not hasattr(camera, "requestOutput"):
        raise RuntimeError(
            "DepthAI Camera 节点不支持 requestOutput，当前脚本需要 DepthAI v3"
        )

    if output_type:
        img_type = enum_value(dai.ImgFrame.Type, output_type)
        return camera.requestOutput(size=(width, height), type=img_type, fps=fps)
    return camera.requestOutput(size=(width, height), fps=fps)


def _link_camera_output_to_encoder(camera_output: Any, encoder: Any) -> None:
    link = getattr(camera_output, "link", None)
    encoder_input = getattr(encoder, "input", None)
    if callable(link) and encoder_input is not None:
        link(encoder_input)
        return
    raise RuntimeError(
        "无法把 Camera 输出连接到 VideoEncoder 输入，当前 DepthAI 版本不支持该连接方式"
    )


def build_video_encoder_node(
    node: Any,
    dai: Any,
    camera_output: Any,
    fps: int,
    codec: str,
    bitrate_kbps: int | None = None,
    quality: int | None = None,
    lossless: bool = False,
    num_frames_pool: int | None = None,
    keyframe_frequency: int | None = None,
    num_b_frames: int | None = None,
) -> Any:
    profile_name = normalize_encoder_codec(codec)
    profile = enum_value(dai.VideoEncoderProperties.Profile, profile_name)
    built = None
    build = getattr(node, "build", None)
    if callable(build):
        try:
            built = build(camera_output)
        except TypeError:
            built = None
    encoder = built if built is not None else node
    if built is None:
        _link_camera_output_to_encoder(camera_output, encoder)

    set_profile = getattr(encoder, "setProfile", None)
    if callable(set_profile):
        set_profile(profile)
    else:
        set_default_profile = getattr(encoder, "setDefaultProfilePreset", None)
        if not callable(set_default_profile):
            raise RuntimeError(
                "DepthAI VideoEncoder 不支持 setProfile/setDefaultProfilePreset"
            )
        set_default_profile(float(fps), profile)

    if bitrate_kbps and profile_name != "MJPEG":
        set_bitrate = getattr(encoder, "setBitrateKbps", None)
        if callable(set_bitrate):
            set_bitrate(int(bitrate_kbps))

    if quality is not None and profile_name == "MJPEG":
        set_quality = getattr(encoder, "setQuality", None)
        if callable(set_quality):
            set_quality(max(0, min(100, int(quality))))

    if profile_name == "MJPEG":
        set_lossless = getattr(encoder, "setLossless", None)
        if callable(set_lossless):
            set_lossless(bool(lossless))

    if num_frames_pool is not None:
        set_pool = getattr(encoder, "setNumFramesPool", None)
        if callable(set_pool):
            set_pool(max(1, int(num_frames_pool)))

    if profile_name != "MJPEG":
        if keyframe_frequency is not None:
            set_keyframe = getattr(encoder, "setKeyframeFrequency", None)
            if callable(set_keyframe):
                set_keyframe(max(1, int(keyframe_frequency)))
        if num_b_frames is not None:
            set_b_frames = getattr(encoder, "setNumBFrames", None)
            if callable(set_b_frames):
                set_b_frames(max(0, int(num_b_frames)))

    return encoder


def get_video_encoder_output(encoder: Any, queue_size: int) -> Any:
    output = getattr(encoder, "out", None)
    if output is None:
        output = getattr(encoder, "bitstream", None)
    if output is None:
        raise RuntimeError("DepthAI VideoEncoder 没有可用 out/bitstream 输出")
    return get_queue_output(output, queue_size)


def set_camera_frame_sync_input(camera: Any, dai: Any, socket_name: str) -> None:
    control = getattr(camera, "initialControl", None)
    set_mode = getattr(control, "setFrameSyncMode", None)
    if not callable(set_mode):
        raise RuntimeError(
            f"{socket_name} 不支持 initialControl.setFrameSyncMode，"
            "无法启用硬同步；可用 --no-hardware-sync 降级运行"
        )
    try:
        set_mode(enum_value(dai.CameraControl.FrameSyncMode, "INPUT"))
    except Exception as exc:
        raise RuntimeError(
            f"{socket_name} 设置 FrameSyncMode.INPUT 失败: {type(exc).__name__}: {exc}。"
            "可用 --no-hardware-sync 降级运行"
        ) from exc


def configure_camera_initial_control(
    camera: Any,
    dai: Any | None = None,
    *,
    mode: str = "auto",
    image_orientation: str | None = None,
    exposure_time_us: int | None = None,
    sensitivity_iso: int | None = None,
    white_balance_k: int | None = None,
) -> None:
    if image_orientation is not None:
        set_orientation = getattr(camera, "setImageOrientation", None)
        if not callable(set_orientation):
            raise RuntimeError("Camera 节点不支持 setImageOrientation")
        orientation_name = str(image_orientation).strip().upper()
        orientation = (
            enum_value(dai.CameraImageOrientation, orientation_name)
            if dai is not None
            else orientation_name
        )
        set_orientation(orientation)
    control = getattr(camera, "initialControl", None)
    if control is None:
        raise RuntimeError("Camera 节点没有 initialControl，无法配置 3A 参数")
    normalized_mode = str(mode).strip().lower()
    if normalized_mode not in {"auto", "manual"}:
        raise ValueError(f"未知 camera_control.mode={mode!r}，可用值: auto/manual")
    if normalized_mode == "auto":
        set_auto_exposure = getattr(control, "setAutoExposureEnable", None)
        if not callable(set_auto_exposure):
            raise RuntimeError("CameraControl 不支持 setAutoExposureEnable")
        set_auto_exposure()
        set_auto_white_balance = getattr(control, "setAutoWhiteBalanceMode", None)
        if not callable(set_auto_white_balance):
            raise RuntimeError("CameraControl 不支持 setAutoWhiteBalanceMode")
        if dai is None:
            auto_mode = "AUTO"
        else:
            auto_mode = enum_value(dai.CameraControl.AutoWhiteBalanceMode, "AUTO")
        set_auto_white_balance(auto_mode)
    if exposure_time_us is not None or sensitivity_iso is not None:
        if exposure_time_us is None or sensitivity_iso is None:
            raise ValueError("手动曝光必须同时指定 exposure_time_us 和 sensitivity_iso")
        set_manual_exposure = getattr(control, "setManualExposure", None)
        if not callable(set_manual_exposure):
            raise RuntimeError("CameraControl 不支持 setManualExposure")
        set_manual_exposure(int(exposure_time_us), int(sensitivity_iso))
    if white_balance_k is not None:
        set_manual_white_balance = getattr(control, "setManualWhiteBalance", None)
        if not callable(set_manual_white_balance):
            raise RuntimeError("CameraControl 不支持 setManualWhiteBalance")
        set_manual_white_balance(int(white_balance_k))


def _hard_sync_script_source(
    fps: int, pulse_width_sec: float, overhead_sec: float
) -> str:
    return f"""# coding=utf-8
import time
import GPIO

fps = {float(fps)!r}
pulse_width_sec = {float(pulse_width_sec)!r}
overhead_sec = {float(overhead_sec)!r}

calib = Device.readCalibration2().getEepromData()
board_rev = calib.boardRev
revision = -1
if len(board_rev) >= 2 and board_rev[0] == "R":
    try:
        revision = int(board_rev[1])
    except Exception:
        revision = -1

node.warn(f"硬同步 FSIN: boardRev={{board_rev}}, parsedRevision={{revision}}, fps={{fps}}")

# OAK-FFC-4P R5 及更早版本与 R6+ 的 FSIN 选择脚不同。
gpio_fsin_2lane = 41
gpio_fsin_4lane = 40
gpio_fsin_mode_select = 6
if revision >= 6:
    gpio_fsin_2lane = 41
    gpio_fsin_4lane = 42
    gpio_fsin_mode_select = 38

GPIO.setup(gpio_fsin_2lane, GPIO.OUT)
GPIO.write(gpio_fsin_2lane, 0)
GPIO.setup(gpio_fsin_4lane, GPIO.IN)
GPIO.setup(gpio_fsin_mode_select, GPIO.OUT)
GPIO.write(gpio_fsin_mode_select, 1)

period = 1.0 / fps
sleep_low = max(0.0, period - pulse_width_sec - overhead_sec)
node.warn(f"硬同步 FSIN: period={{period}}, pulse={{pulse_width_sec}}, sleepLow={{sleep_low}}")

while True:
    GPIO.write(gpio_fsin_2lane, 1)
    time.sleep(pulse_width_sec)
    GPIO.write(gpio_fsin_2lane, 0)
    time.sleep(sleep_low)
"""


def add_hardware_sync_script(
    pipeline: Any, dai: Any, fps: int, hardware_sync_config: dict[str, Any]
) -> None:
    script_cls = getattr(getattr(dai, "node", None), "Script", None)
    if script_cls is None:
        raise RuntimeError(
            "DepthAI 当前版本不支持 Script 节点，无法启用硬同步；可用 --no-hardware-sync 降级运行"
        )
    try:
        script = pipeline.create(script_cls)
        script.setProcessor(enum_value(dai.ProcessorType, "LEON_CSS"))
        script.setScript(
            _hard_sync_script_source(
                fps=fps,
                pulse_width_sec=float(
                    hardware_sync_config.get("pulse_width_sec", 0.001)
                ),
                overhead_sec=float(
                    hardware_sync_config.get("script_overhead_sec", 0.003)
                ),
            )
        )
    except Exception as exc:
        raise RuntimeError(
            f"创建 FSIN/GPIO 硬同步脚本失败: {type(exc).__name__}: {exc}。"
            "可用 --no-hardware-sync 降级运行"
        ) from exc


def build_camera_streams(
    pipeline: Any,
    dai: Any,
    cameras: list[dict[str, str]],
    width: int,
    height: int,
    fps: int,
    queue_size: int,
    output_type: str | None = None,
    hardware_sync_config: dict[str, Any] | None = None,
    camera_control_config: dict[str, Any] | None = None,
) -> list[CameraStream]:
    streams: list[CameraStream] = []
    hardware_sync_enabled = bool((hardware_sync_config or {}).get("enabled", False))
    for camera_cfg in cameras:
        socket_name = camera_cfg["socket"]
        socket = enum_value(dai.CameraBoardSocket, socket_name)
        node = pipeline.create(dai.node.Camera)
        camera = build_camera_node(node, socket, width, height, fps)
        configure_camera_initial_control(
            camera, dai, **(camera_control_config or {"mode": "auto"})
        )
        if hardware_sync_enabled:
            set_camera_frame_sync_input(camera, dai, socket_name)
        output = request_camera_output_typed(
            camera, dai, width, height, fps, output_type
        )
        queue = get_queue_output(output, queue_size)
        streams.append(
            CameraStream(socket=socket_name, name=camera_cfg["name"], queue=queue)
        )
    if hardware_sync_enabled:
        add_hardware_sync_script(pipeline, dai, fps, hardware_sync_config or {})
    return streams


def build_encoded_camera_streams(
    pipeline: Any,
    dai: Any,
    cameras: list[dict[str, str]],
    width: int,
    height: int,
    fps: int,
    queue_size: int,
    codec: str,
    bitrate_kbps: int | None = None,
    quality: int | None = None,
    lossless: bool = False,
    num_frames_pool: int | None = None,
    keyframe_frequency: int | None = None,
    num_b_frames: int | None = None,
    hardware_sync_config: dict[str, Any] | None = None,
    camera_control_config: dict[str, Any] | None = None,
    sensor_fps: int | None = None,
    output_fps_by_socket: dict[str, int] | None = None,
    validate_budget: bool = True,
) -> list[EncodedCameraStream]:
    profile = normalize_encoder_codec(codec)
    if validate_budget and not output_fps_by_socket:
        validate_encoder_budget(profile, width, height, fps, len(cameras))
    effective_sensor_fps = int(sensor_fps or fps)
    streams: list[EncodedCameraStream] = []
    hardware_sync_enabled = bool((hardware_sync_config or {}).get("enabled", False))
    video_encoder_cls = getattr(getattr(dai, "node", None), "VideoEncoder", None)
    if video_encoder_cls is None:
        raise RuntimeError("DepthAI 当前版本不支持 VideoEncoder 节点")
    for camera_cfg in cameras:
        socket_name = camera_cfg["socket"]
        output_fps = int((output_fps_by_socket or {}).get(socket_name, fps))
        socket = enum_value(dai.CameraBoardSocket, socket_name)
        node = pipeline.create(dai.node.Camera)
        camera = build_camera_node(node, socket, width, height, effective_sensor_fps)
        configure_camera_initial_control(
            camera, dai, **(camera_control_config or {"mode": "auto"})
        )
        if hardware_sync_enabled:
            set_camera_frame_sync_input(camera, dai, socket_name)
        camera_output = request_camera_output_typed(
            camera, dai, width, height, output_fps, "NV12"
        )
        encoder_node = pipeline.create(video_encoder_cls)
        encoder = build_video_encoder_node(
            encoder_node,
            dai,
            camera_output,
            fps=output_fps,
            codec=profile,
            bitrate_kbps=bitrate_kbps,
            quality=quality,
            lossless=lossless,
            num_frames_pool=num_frames_pool,
            keyframe_frequency=keyframe_frequency,
            num_b_frames=num_b_frames,
        )
        queue = get_video_encoder_output(encoder, queue_size)
        streams.append(
            EncodedCameraStream(
                socket=socket_name,
                name=camera_cfg["name"],
                queue=queue,
                codec=profile,
                file_extension=encoded_stream_extension(profile),
                fps=output_fps,
            )
        )
    if hardware_sync_enabled:
        add_hardware_sync_script(
            pipeline, dai, effective_sensor_fps, hardware_sync_config or {}
        )
    return streams


def write_encoded_message(file_obj: Any, message: Any) -> int:
    get_data = getattr(message, "getData", None)
    if not callable(get_data):
        raise RuntimeError("编码帧对象不支持 getData")
    data = get_data()
    if hasattr(data, "tofile"):
        data.tofile(file_obj)
        nbytes = getattr(data, "nbytes", None)
        if nbytes is not None:
            return int(nbytes)
        try:
            return len(data)
        except TypeError:
            return 0
    payload = bytes(data)
    file_obj.write(payload)
    return len(payload)


def build_camera_node(node: Any, socket: Any, width: int, height: int, fps: int) -> Any:
    try:
        built = node.build(
            boardSocket=socket, sensorResolution=(width, height), sensorFps=float(fps)
        )
    except RuntimeError as exc:
        if "Invalid sensor resolution" not in str(exc):
            raise
        built = node.build(boardSocket=socket, sensorFps=float(fps))
    return built if built is not None else node


def build_imu_stream(
    pipeline: Any, dai: Any, imu_config: dict[str, Any], queue_size: int
) -> ImuStream | None:
    if not imu_config.get("enabled", True):
        return None
    imu_node_cls = getattr(getattr(dai, "node", None), "IMU", None)
    if imu_node_cls is None:
        return None
    imu = pipeline.create(imu_node_cls)
    frequency = int(imu_config.get("frequency_hz", 200))
    sensors = imu_config.get("sensors") or [
        "ACCELEROMETER_CALIBRATED",
        "GYROSCOPE_CALIBRATED",
    ]
    for sensor_name in sensors:
        try:
            imu.enableIMUSensor(enum_value(dai.IMUSensor, str(sensor_name)), frequency)
        except Exception:
            continue
    for method, value in (("setBatchReportThreshold", 1), ("setMaxBatchReports", 10)):
        fn = getattr(imu, method, None)
        if callable(fn):
            try:
                fn(value)
            except Exception:
                pass
    output = getattr(imu, "out", None)
    if output is None:
        return None
    return ImuStream(queue=get_queue_output(output, queue_size))


def start_pipeline_context(
    dai: Any,
    device_info: Any = None,
    *,
    device: Any = None,
) -> Any:
    device = device if device is not None else open_device(dai, device_info)
    pipeline = dai.Pipeline(device)
    set_chunk_size = getattr(pipeline, "setXLinkChunkSize", None)
    if callable(set_chunk_size):
        set_chunk_size(0)
    try:
        setattr(pipeline, "_oak_device_handle", device)
    except Exception:
        pass
    return pipeline


def start_pipeline(pipeline: Any, device_info: Any = None) -> None:
    if not hasattr(pipeline, "start"):
        raise RuntimeError("DepthAI Pipeline 不支持 start()，当前脚本需要 DepthAI v3")
    pipeline.start()


def stop_pipeline(pipeline: Any) -> None:
    for method in ("stop", "close"):
        fn = getattr(pipeline, method, None)
        if callable(fn):
            try:
                fn()
                break
            except Exception:
                pass
    device = getattr(pipeline, "_oak_device_handle", None)
    close_fn = getattr(device, "close", None)
    if callable(close_fn):
        try:
            close_fn()
        except Exception:
            pass


def enumerate_device_infos(dai: Any) -> list[Any]:
    for owner_name in ("Device", "DeviceBootloader"):
        owner = getattr(dai, owner_name, None)
        fn = getattr(owner, "getAllAvailableDevices", None)
        if callable(fn):
            try:
                devices = fn()
                return list(devices or [])
            except Exception:
                pass
    return []


def open_device(dai: Any, device_info: Any = None) -> Any:
    if device_info is None:
        return dai.Device()
    try:
        return dai.Device(device_info)
    except TypeError:
        return dai.Device()


def device_info_summary(info: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {"raw": str(info)}
    for key, method in (
        ("mxid", "getMxId"),
        ("mxid", "getDeviceId"),
        ("name", "getName"),
        ("state", "getState"),
        ("protocol", "getProtocol"),
    ):
        value = safe_call(info, method)
        if value is not None:
            summary[key] = to_jsonable(value)
    for attr in ("mxid", "deviceId", "name", "state", "protocol"):
        if hasattr(info, attr):
            key = "mxid" if attr == "deviceId" else attr
            summary[key] = to_jsonable(getattr(info, attr))
    return summary


def collect_device_snapshot(
    dai: Any,
    device_info: Any = None,
    *,
    device: Any = None,
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "depthai_version": getattr(dai, "__version__", None),
        "discovered_at_unix_ns": time.time_ns(),
        "device_info": device_info_summary(device_info)
        if device_info is not None
        else None,
    }
    opened_here = device is None
    try:
        device = device if device is not None else open_device(dai, device_info)
        if opened_here:
            enter = getattr(device, "__enter__", None)
            if callable(enter):
                device = enter()
        for key, method in (
            ("mxid", "getMxId"),
            ("name", "getDeviceName"),
            ("usb_speed", "getUsbSpeed"),
            ("connected_imu", "getConnectedIMU"),
            ("device_state", "getDeviceState"),
        ):
            value = safe_call(device, method)
            if value is not None:
                snapshot[key] = to_jsonable(value)
        snapshot["camera_features"] = to_jsonable(
            safe_call(device, "getConnectedCameraFeatures")
        )
        snapshot["camera_sensor_names"] = to_jsonable(
            safe_call(device, "getCameraSensorNames")
        )
        calibration = safe_call(device, "readCalibration")
        snapshot["calibration"] = calibration_to_json(calibration)
    finally:
        if opened_here and device is not None:
            exit_fn = getattr(device, "__exit__", None)
            close_fn = getattr(device, "close", None)
            try:
                if callable(exit_fn):
                    exit_fn(None, None, None)
                elif callable(close_fn):
                    close_fn()
            except Exception:
                pass
    return snapshot


def calibration_to_json(calibration: Any) -> Any:
    if calibration is None:
        return None
    if isinstance(calibration, dict) and "error" in calibration:
        return calibration
    for method in ("eepromToJson", "serializeToJson"):
        fn = getattr(calibration, method, None)
        if callable(fn):
            try:
                value = fn()
                if isinstance(value, str):
                    try:
                        return json.loads(value)
                    except json.JSONDecodeError:
                        return value
                return to_jsonable(value)
            except Exception as exc:
                return {"error": f"{type(exc).__name__}: {exc}"}
    data = safe_call(calibration, "getEepromData")
    return to_jsonable(data if data is not None else calibration)


def select_device_info(dai: Any, mxid: str | None = None) -> Any:
    devices = enumerate_device_infos(dai)
    if not devices:
        return None
    if not mxid:
        return devices[0]
    for info in devices:
        summary = device_info_summary(info)
        if str(summary.get("mxid")) == mxid:
            return info
    raise RuntimeError(f"未找到 MXID={mxid} 的 OAK 设备")


def extract_imu_packets(message: Any) -> list[dict[str, Any]]:
    packets = getattr(message, "packets", None)
    if packets is None:
        packets = [message]
    rows: list[dict[str, Any]] = []
    for packet in packets:
        for sensor_attr, sensor_name in (
            ("acceleroMeter", "accelerometer"),
            ("gyroscope", "gyroscope"),
            ("magneticField", "magnetometer"),
            ("rotationVector", "rotation_vector"),
        ):
            sensor = getattr(packet, sensor_attr, None)
            if sensor is None:
                continue
            sensor_device_ns = device_timestamp_ns(sensor)
            if sensor_device_ns is None:
                sensor_device_ns = device_timestamp_ns(packet)
            sensor_host_ns = host_timestamp_ns(sensor)
            if sensor_host_ns is None:
                sensor_host_ns = host_timestamp_ns(packet)
            rows.append(
                {
                    "sensor": sensor_name,
                    "device_timestamp_ns": sensor_device_ns,
                    "host_timestamp_ns": sensor_host_ns,
                    "x": getattr(sensor, "i", "")
                    if sensor_name == "rotation_vector"
                    else getattr(sensor, "x", ""),
                    "y": getattr(sensor, "j", "")
                    if sensor_name == "rotation_vector"
                    else getattr(sensor, "y", ""),
                    "z": getattr(sensor, "k", "")
                    if sensor_name == "rotation_vector"
                    else getattr(sensor, "z", ""),
                    "w": getattr(sensor, "real", "")
                    if sensor_name == "rotation_vector"
                    else getattr(sensor, "w", ""),
                    "accuracy": (
                        getattr(
                            sensor,
                            "rotationVectorAccuracy",
                            getattr(sensor, "accuracy", ""),
                        )
                        if sensor_name == "rotation_vector"
                        else getattr(sensor, "accuracy", "")
                    ),
                }
            )
    return rows


def extract_imu_wide_samples(message: Any) -> list[dict[str, Any]]:
    packets = getattr(message, "packets", None)
    if packets is None:
        packets = [message]
    rows: list[dict[str, Any]] = []
    for packet in packets:
        row: dict[str, Any] = {}
        timestamps: list[int] = []
        host_timestamps: list[int] = []
        for sensor_attr, prefix in (
            ("acceleroMeter", "accel"),
            ("gyroscope", "gyro"),
            ("magneticField", "mag"),
            ("rotationVector", "rotation"),
        ):
            sensor = getattr(packet, sensor_attr, None)
            if sensor is None:
                continue
            sensor_device_ns = device_timestamp_ns(sensor)
            if sensor_device_ns is None:
                sensor_device_ns = device_timestamp_ns(packet)
            sensor_host_ns = host_timestamp_ns(sensor)
            if sensor_host_ns is None:
                sensor_host_ns = host_timestamp_ns(packet)
            row[f"{prefix}_device_timestamp_ns"] = sensor_device_ns
            row[f"{prefix}_host_timestamp_ns"] = sensor_host_ns
            if sensor_device_ns is not None:
                timestamps.append(sensor_device_ns)
            if sensor_host_ns is not None:
                host_timestamps.append(sensor_host_ns)
            if prefix == "rotation":
                row.update(
                    {
                        "quat_x": getattr(sensor, "i", ""),
                        "quat_y": getattr(sensor, "j", ""),
                        "quat_z": getattr(sensor, "k", ""),
                        "quat_w": getattr(sensor, "real", ""),
                        "orientation_accuracy": getattr(
                            sensor,
                            "rotationVectorAccuracy",
                            getattr(sensor, "accuracy", ""),
                        ),
                    }
                )
            else:
                suffixes = {
                    "accel": ("accel_x_m_s2", "accel_y_m_s2", "accel_z_m_s2"),
                    "gyro": ("gyro_x_rad_s", "gyro_y_rad_s", "gyro_z_rad_s"),
                    "mag": ("mag_x_uT", "mag_y_uT", "mag_z_uT"),
                }[prefix]
                row.update(
                    {
                        suffixes[0]: getattr(sensor, "x", ""),
                        suffixes[1]: getattr(sensor, "y", ""),
                        suffixes[2]: getattr(sensor, "z", ""),
                    }
                )
        if not row:
            continue
        row["device_timestamp_ns"] = (
            max(timestamps) if timestamps else device_timestamp_ns(packet)
        )
        row["host_timestamp_ns"] = (
            max(host_timestamps) if host_timestamps else host_timestamp_ns(packet)
        )
        rows.append(row)
    return rows
