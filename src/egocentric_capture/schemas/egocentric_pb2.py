"""Generated protocol buffer code."""
from google.protobuf import descriptor as _descriptor
from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import runtime_version as _runtime_version
from google.protobuf import symbol_database as _symbol_database
from google.protobuf.internal import builder as _builder
_runtime_version.ValidateProtobufRuntimeVersion(
    _runtime_version.Domain.PUBLIC,
    7,
    35,
    1,
    '',
    'egocentric.proto'
)

_sym_db = _symbol_database.Default()




DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(b'\n\x10\x65gocentric.proto\x12\x15\x65gocentric.capture.v1\"\x90\x02\n\x0e\x43lockReference\x12\x1a\n\x12normalized_unix_ns\x18\x01 \x01(\x04\x12\x19\n\x11host_monotonic_ns\x18\x02 \x01(\x04\x12\x1d\n\x10source_device_ns\x18\x03 \x01(\x04H\x00\x88\x01\x01\x12\x1b\n\x0esource_host_ns\x18\x04 \x01(\x04H\x01\x88\x01\x01\x12!\n\x14\x61rrival_monotonic_ns\x18\x05 \x01(\x04H\x02\x88\x01\x01\x12\x16\n\x0euncertainty_ns\x18\x06 \x01(\x04\x12\x0f\n\x07quality\x18\x07 \x01(\tB\x13\n\x11_source_device_nsB\x11\n\x0f_source_host_nsB\x17\n\x15_arrival_monotonic_ns\"\xe1\x01\n\x0c\x43\x61meraTiming\x12\x34\n\x05stamp\x18\x01 \x01(\x0b\x32%.egocentric.capture.v1.ClockReference\x12\x0e\n\x06\x63\x61mera\x18\x02 \x01(\t\x12\x0e\n\x06socket\x18\x03 \x01(\t\x12\x10\n\x08sequence\x18\x04 \x01(\x04\x12\x12\n\nframe_type\x18\x05 \x01(\t\x12\r\n\x05width\x18\x06 \x01(\r\x12\x0e\n\x06height\x18\x07 \x01(\r\x12\r\n\x05\x63odec\x18\x08 \x01(\t\x12\x15\n\rpayload_bytes\x18\t \x01(\x04\x12\x10\n\x08keyframe\x18\n \x01(\x08\"\xa2\x04\n\x0cOakImuSample\x12\x34\n\x05stamp\x18\x01 \x01(\x0b\x32%.egocentric.capture.v1.ClockReference\x12\x12\n\naccel_m_s2\x18\x02 \x03(\x01\x12\x12\n\ngyro_rad_s\x18\x03 \x03(\x01\x12\x13\n\x0bmagnetic_ut\x18\x04 \x03(\x01\x12\x17\n\x0fquaternion_xyzw\x18\x05 \x03(\x01\x12\x66\n\x1bsensor_device_timestamps_ns\x18\x06 \x03(\x0b\x32\x41.egocentric.capture.v1.OakImuSample.SensorDeviceTimestampsNsEntry\x12\x62\n\x19sensor_host_timestamps_ns\x18\x07 \x03(\x0b\x32?.egocentric.capture.v1.OakImuSample.SensorHostTimestampsNsEntry\x12!\n\x14orientation_accuracy\x18\x08 \x01(\x01H\x00\x88\x01\x01\x1a?\n\x1dSensorDeviceTimestampsNsEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12\r\n\x05value\x18\x02 \x01(\x04:\x02\x38\x01\x1a=\n\x1bSensorHostTimestampsNsEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12\r\n\x05value\x18\x02 \x01(\x04:\x02\x38\x01\x42\x17\n\x15_orientation_accuracy\"|\n\x10WearableRawChunk\x12\x0c\n\x04\x64\x61ta\x18\x01 \x01(\x0c\x12\x1f\n\x17read_start_monotonic_ns\x18\x02 \x01(\x04\x12\x1d\n\x15read_end_monotonic_ns\x18\x03 \x01(\x04\x12\x1a\n\x12normalized_unix_ns\x18\x04 \x01(\x04\"p\n\x11WearableEmgSample\x12\x34\n\x05stamp\x18\x01 \x01(\x0b\x32%.egocentric.capture.v1.ClockReference\x12\x10\n\x08sequence\x18\x02 \x01(\r\x12\x13\n\x0b\x63hannels_uv\x18\x03 \x03(\x11\"\xa8\x01\n\x11WearableImuSample\x12\x34\n\x05stamp\x18\x01 \x01(\x0b\x32%.egocentric.capture.v1.ClockReference\x12\x10\n\x08sequence\x18\x02 \x01(\r\x12\x10\n\x08gyro_raw\x18\x03 \x03(\x11\x12\x11\n\taccel_raw\x18\x04 \x03(\x11\x12\x12\n\ngyro_rad_s\x18\x05 \x03(\x01\x12\x12\n\naccel_m_s2\x18\x06 \x03(\x01\"i\n\tClockSync\x12\x19\n\x11host_monotonic_ns\x18\x01 \x01(\x04\x12\x0f\n\x07unix_ns\x18\x02 \x01(\x04\x12\x16\n\x0euncertainty_ns\x18\x03 \x01(\x04\x12\x18\n\x10offset_jitter_ns\x18\x04 \x01(\x01\"\x92\x04\n\x0cHealthSample\x12\x34\n\x05stamp\x18\x01 \x01(\x0b\x32%.egocentric.capture.v1.ClockReference\x12\x42\n\x08rates_hz\x18\x02 \x03(\x0b\x32\x30.egocentric.capture.v1.HealthSample.RatesHzEntry\x12N\n\x0flast_seen_age_s\x18\x03 \x03(\x0b\x32\x35.egocentric.capture.v1.HealthSample.LastSeenAgeSEntry\x12L\n\rsequence_gaps\x18\x04 \x03(\x0b\x32\x35.egocentric.capture.v1.HealthSample.SequenceGapsEntry\x12\x13\n\x0bqueue_depth\x18\x05 \x01(\x04\x12\x13\n\x0bqueue_drops\x18\x06 \x01(\x04\x12\x17\n\x0f\x64isk_free_bytes\x18\x07 \x01(\x04\x12\r\n\x05ready\x18\x08 \x01(\x08\x1a.\n\x0cRatesHzEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12\r\n\x05value\x18\x02 \x01(\x01:\x02\x38\x01\x1a\x33\n\x11LastSeenAgeSEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12\r\n\x05value\x18\x02 \x01(\x01:\x02\x38\x01\x1a\x33\n\x11SequenceGapsEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12\r\n\x05value\x18\x02 \x01(\x04:\x02\x38\x01\"\xe3\x01\n\x0bSystemEvent\x12\x34\n\x05stamp\x18\x01 \x01(\x0b\x32%.egocentric.capture.v1.ClockReference\x12\r\n\x05level\x18\x02 \x01(\t\x12\x0c\n\x04\x63ode\x18\x03 \x01(\t\x12\x0f\n\x07message\x18\x04 \x01(\t\x12@\n\x07\x64\x65tails\x18\x05 \x03(\x0b\x32/.egocentric.capture.v1.SystemEvent.DetailsEntry\x1a.\n\x0c\x44\x65tailsEntry\x12\x0b\n\x03key\x18\x01 \x01(\t\x12\r\n\x05value\x18\x02 \x01(\t:\x02\x38\x01\x62\x06proto3')

_globals = globals()
_builder.BuildMessageAndEnumDescriptors(DESCRIPTOR, _globals)
_builder.BuildTopDescriptorsAndMessages(DESCRIPTOR, 'egocentric_pb2', _globals)
if not _descriptor._USE_C_DESCRIPTORS:
  DESCRIPTOR._loaded_options = None
  _globals['_OAKIMUSAMPLE_SENSORDEVICETIMESTAMPSNSENTRY']._loaded_options = None
  _globals['_OAKIMUSAMPLE_SENSORDEVICETIMESTAMPSNSENTRY']._serialized_options = b'8\001'
  _globals['_OAKIMUSAMPLE_SENSORHOSTTIMESTAMPSNSENTRY']._loaded_options = None
  _globals['_OAKIMUSAMPLE_SENSORHOSTTIMESTAMPSNSENTRY']._serialized_options = b'8\001'
  _globals['_HEALTHSAMPLE_RATESHZENTRY']._loaded_options = None
  _globals['_HEALTHSAMPLE_RATESHZENTRY']._serialized_options = b'8\001'
  _globals['_HEALTHSAMPLE_LASTSEENAGESENTRY']._loaded_options = None
  _globals['_HEALTHSAMPLE_LASTSEENAGESENTRY']._serialized_options = b'8\001'
  _globals['_HEALTHSAMPLE_SEQUENCEGAPSENTRY']._loaded_options = None
  _globals['_HEALTHSAMPLE_SEQUENCEGAPSENTRY']._serialized_options = b'8\001'
  _globals['_SYSTEMEVENT_DETAILSENTRY']._loaded_options = None
  _globals['_SYSTEMEVENT_DETAILSENTRY']._serialized_options = b'8\001'
  _globals['_CLOCKREFERENCE']._serialized_start=44
  _globals['_CLOCKREFERENCE']._serialized_end=316
  _globals['_CAMERATIMING']._serialized_start=319
  _globals['_CAMERATIMING']._serialized_end=544
  _globals['_OAKIMUSAMPLE']._serialized_start=547
  _globals['_OAKIMUSAMPLE']._serialized_end=1093
  _globals['_OAKIMUSAMPLE_SENSORDEVICETIMESTAMPSNSENTRY']._serialized_start=942
  _globals['_OAKIMUSAMPLE_SENSORDEVICETIMESTAMPSNSENTRY']._serialized_end=1005
  _globals['_OAKIMUSAMPLE_SENSORHOSTTIMESTAMPSNSENTRY']._serialized_start=1007
  _globals['_OAKIMUSAMPLE_SENSORHOSTTIMESTAMPSNSENTRY']._serialized_end=1068
  _globals['_WEARABLERAWCHUNK']._serialized_start=1095
  _globals['_WEARABLERAWCHUNK']._serialized_end=1219
  _globals['_WEARABLEEMGSAMPLE']._serialized_start=1221
  _globals['_WEARABLEEMGSAMPLE']._serialized_end=1333
  _globals['_WEARABLEIMUSAMPLE']._serialized_start=1336
  _globals['_WEARABLEIMUSAMPLE']._serialized_end=1504
  _globals['_CLOCKSYNC']._serialized_start=1506
  _globals['_CLOCKSYNC']._serialized_end=1611
  _globals['_HEALTHSAMPLE']._serialized_start=1614
  _globals['_HEALTHSAMPLE']._serialized_end=2144
  _globals['_HEALTHSAMPLE_RATESHZENTRY']._serialized_start=1992
  _globals['_HEALTHSAMPLE_RATESHZENTRY']._serialized_end=2038
  _globals['_HEALTHSAMPLE_LASTSEENAGESENTRY']._serialized_start=2040
  _globals['_HEALTHSAMPLE_LASTSEENAGESENTRY']._serialized_end=2091
  _globals['_HEALTHSAMPLE_SEQUENCEGAPSENTRY']._serialized_start=2093
  _globals['_HEALTHSAMPLE_SEQUENCEGAPSENTRY']._serialized_end=2144
  _globals['_SYSTEMEVENT']._serialized_start=2147
  _globals['_SYSTEMEVENT']._serialized_end=2374
  _globals['_SYSTEMEVENT_DETAILSENTRY']._serialized_start=2328
  _globals['_SYSTEMEVENT_DETAILSENTRY']._serialized_end=2374
