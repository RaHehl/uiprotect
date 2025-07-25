from __future__ import annotations

import enum
from collections.abc import Callable, Coroutine
from functools import cache, lru_cache
from typing import Annotated, Any, Literal, TypeVar, Union

from packaging.version import Version as BaseVersion
from pydantic import BaseModel, Field
from pydantic.types import StringConstraints
from pydantic.v1.config import BaseConfig as BaseConfigV1
from pydantic.v1.fields import SHAPE_DICT as SHAPE_DICT_V1  # noqa: F401
from pydantic.v1.fields import SHAPE_LIST as SHAPE_LIST_V1  # noqa: F401
from pydantic.v1.fields import SHAPE_SET as SHAPE_SET_V1  # noqa: F401
from pydantic.v1.fields import ModelField as ModelFieldV1
from pydantic_extra_types.color import Color  # noqa: F401

from .._compat import cached_property

KT = TypeVar("KT")
VT = TypeVar("VT")


class _BaseConfigV1(BaseConfigV1):
    arbitrary_types_allowed = True
    validate_assignment = True


@lru_cache(maxsize=512)
def extract_type_shape(annotation: type[Any] | None) -> tuple[Any, int]:
    """Extract the type from a type hint."""
    if annotation is None:
        raise ValueError("Type annotation cannot be None")
    v1_field = ModelFieldV1(
        name="", type_=annotation, class_validators=None, model_config=_BaseConfigV1
    )
    return v1_field.type_, v1_field.shape


DEFAULT = "DEFAULT_VALUE"
DEFAULT_TYPE = Literal["DEFAULT_VALUE"]
EventCategories = Literal[
    "critical",
    "update",
    "admin",
    "ring",
    "motion",
    "smart",
    "iot",
]

ProgressCallback = Callable[[int, int, int], Coroutine[Any, Any, None]]
IteratorCallback = Callable[[int, bytes | None], Coroutine[Any, Any, None]]


class FixSizeOrderedDict(dict[KT, VT]):
    """A fixed size ordered dict."""

    def __init__(self, *args: Any, max_size: int = 0, **kwargs: Any) -> None:
        """Create the FixSizeOrderedDict."""
        self._max_size = max_size
        super().__init__(*args, **kwargs)

    def __setitem__(self, key: KT, value: VT) -> None:
        """Set an update up to the max size."""
        dict.__setitem__(self, key, value)
        if self._max_size > 0 and len(self) > 0 and len(self) > self._max_size:
            del self[next(iter(self))]


class ValuesEnumMixin:
    _values: list[str] | None = None
    _values_normalized: dict[str, str] | None = None

    @classmethod
    @cache
    def from_string(cls, value: str) -> Any:
        return cls(value)  # type: ignore[call-arg]

    @classmethod
    @cache
    def values(cls) -> list[str]:
        if cls._values is None:
            cls._values = [e.value for e in cls]  # type: ignore[attr-defined]
        return cls._values

    @classmethod
    @cache
    def values_set(cls) -> set[str]:
        return set(cls.values())

    @classmethod
    def _missing_(cls, value: Any) -> Any | None:
        if cls._values_normalized is None:
            cls._values_normalized = {e.value.lower(): e for e in cls}  # type: ignore[attr-defined]

        value_normal = value
        if isinstance(value, str):
            value_normal = value.lower()
        return cls._values_normalized.get(value_normal)


class UnknownValuesEnumMixin(ValuesEnumMixin):
    @classmethod
    def _missing_(cls, value: Any) -> Any | None:
        # value always set in superclass _missing
        return super()._missing_(value) or cls._values_normalized.get("unknown")  # type: ignore[union-attr]


@enum.unique
class ModelType(str, UnknownValuesEnumMixin, enum.Enum):
    CAMERA = "camera"
    CLOUD_IDENTITY = "cloudIdentity"
    EVENT = "event"
    GROUP = "group"
    LIGHT = "light"
    LIVEVIEW = "liveview"
    NVR = "nvr"
    USER = "user"
    USER_LOCATION = "userLocation"
    VIEWPORT = "viewer"
    BRIDGE = "bridge"
    SENSOR = "sensor"
    DOORLOCK = "doorlock"
    SCHEDULE = "schedule"
    CHIME = "chime"
    AIPORT = "aiport"
    DEVICE_GROUP = "deviceGroup"
    RECORDING_SCHEDULE = "recordingSchedule"
    ULP_USER = "ulpUser"
    RINGTONE = "ringtone"
    KEYRING = "keyring"
    UNKNOWN = "unknown"

    bootstrap_model_types: tuple[ModelType, ...]
    bootstrap_models: tuple[str, ...]
    bootstrap_models_set: set[str]
    bootstrap_models_types_set: set[ModelType]
    bootstrap_models_types_and_event_set: set[ModelType]

    @cached_property
    def devices_key(self) -> str:
        """Return the devices key."""
        return f"{self.value}s"

    @cached_property
    def name(self) -> str:
        """Return the name."""
        return self._name_

    @cached_property
    def value(self) -> str:
        """Return the value."""
        return self._value_

    @classmethod
    @cache
    def from_string(cls, value: str) -> ModelType:
        return cls(value)

    @classmethod
    def _bootstrap_model_types(cls) -> tuple[ModelType, ...]:
        """Return the bootstrap models as a tuple."""
        # TODO:
        # legacyUFV
        # display
        return (
            ModelType.CAMERA,
            ModelType.USER,
            ModelType.GROUP,
            ModelType.LIVEVIEW,
            ModelType.VIEWPORT,
            ModelType.LIGHT,
            ModelType.BRIDGE,
            ModelType.SENSOR,
            ModelType.DOORLOCK,
            ModelType.CHIME,
            ModelType.AIPORT,
        )

    @classmethod
    def _bootstrap_models(cls) -> tuple[str, ...]:
        """Return the bootstrap models strings as a tuple."""
        return tuple(
            model_type.value for model_type in ModelType._bootstrap_model_types()
        )

    @classmethod
    def _bootstrap_models_set(cls) -> set[str]:
        """Return the set of bootstrap models strings as a set."""
        return set(ModelType._bootstrap_models())

    @classmethod
    def _bootstrap_models_types_set(cls) -> set[ModelType]:
        """Return the set of bootstrap models as a set."""
        return set(ModelType._bootstrap_model_types())

    @classmethod
    def _bootstrap_models_types_and_event_set(cls) -> set[ModelType]:
        """Return the set of bootstrap models and the event model as a set."""
        return ModelType._bootstrap_models_types_set() | {ModelType.EVENT}

    def _immutable(self, name: str, value: Any) -> None:
        raise AttributeError("Cannot modify ModelType")


ModelType.bootstrap_model_types = ModelType._bootstrap_model_types()
ModelType.bootstrap_models = ModelType._bootstrap_models()
ModelType.bootstrap_models_set = ModelType._bootstrap_models_set()
ModelType.bootstrap_models_types_set = ModelType._bootstrap_models_types_set()
ModelType.bootstrap_models_types_and_event_set = (
    ModelType._bootstrap_models_types_and_event_set()
)
ModelType.__setattr__ = ModelType._immutable  # type: ignore[method-assign, assignment]


@enum.unique
class EventType(str, ValuesEnumMixin, enum.Enum):
    DISCONNECT = "disconnect"
    FACTORY_RESET = "factoryReset"
    PROVISION = "provision"
    UPDATE = "update"
    CAMERA_POWER_CYCLE = "cameraPowerCycling"
    RING = "ring"
    DOOR_ACCESS = "doorAccess"
    RESOLUTION_LOWERED = "resolutionLowered"
    POOR_CONNECTION = "poorConnection"
    STREAM_RECOVERY = "streamRecovery"
    MOTION = "motion"
    NFC_CARD_SCANNED = "nfcCardScanned"
    FINGERPRINT_IDENTIFIED = "fingerprintIdentified"
    RECORDING_DELETED = "recordingDeleted"
    SMART_AUDIO_DETECT = "smartAudioDetect"
    SMART_DETECT = "smartDetectZone"
    SMART_DETECT_LINE = "smartDetectLine"
    NO_SCHEDULE = "nonScheduledRecording"
    RECORDING_MODE_CHANGED = "recordingModeChanged"
    HOTPLUG = "hotplug"
    FACE_GROUP_DETECTED = "faceGroupDetected"
    CONSOLIDATED_RESOLUTION_LOWERED = "consolidatedResolutionLowered"
    CONSOLIDATED_POOR_CONNECTION = "consolidatedPoorConnection"
    CAMERA_CONNECTED = "cameraConnected"
    CAMERA_REBOOTED = "cameraRebooted"
    CAMERA_DISCONNECTED = "cameraDisconnected"
    # ---
    INSTALLED_DISK = "installed"
    CORRUPTED_DB_RECOVERED = "corruptedDbRecovered"
    OFFLINE = "offline"
    OFF = "off"
    REBOOT = "reboot"
    FIRMWARE_UPDATE = "fwUpdate"
    APP_UPDATE = "applicationUpdate"
    APPLICATION_UPDATABLE = "applicationUpdatable"
    ACCESS = "access"
    DRIVE_FAILED = "driveFailed"
    CAMERA_UTILIZATION_LIMIT_REACHED = "cameraUtilizationLimitReached"
    CAMERA_UTILIZATION_LIMIT_EXCEEDED = "cameraUtilizationLimitExceeded"
    DRIVE_SLOW = "driveSlow"
    GLOBAL_RECORDING_MODE_CHANGED = "globalRecordingModeChanged"
    NVR_SETTINGS_CHANGED = "nvrSettingsChanged"
    # ---
    UNADOPTED_DEVICE_DISCOVERED = "unadoptedDeviceDiscovered"
    MULTIPLE_UNADOPTED_DEVICE_DISCOVERED = "multipleUnadoptedDeviceDiscovered"
    DEVICE_ADOPTED = "deviceAdopted"
    DEVICE_UNADOPTED = "deviceUnadopted"
    UVF_DISCOVERED = "ufvDiscovered"
    DEVICE_PASSWORD_UPDATE = "devicesPasswordUpdated"  # noqa: S105
    DEVICE_UPDATABLE = "deviceUpdatable"
    MULTIPLE_DEVICE_UPDATABLE = "multipleDeviceUpdatable"
    DEVICE_CONNECTED = "deviceConnected"
    DEVICE_REBOOTED = "deviceRebooted"
    DEVICE_DISCONNECTED = "deviceDisconnected"
    NETWORK_DEVICE_OFFLINE = "networkDeviceOffline"
    # ---
    USER_LEFT = "userLeft"
    USER_ARRIVED = "userArrived"
    VIDEO_EXPORTED = "videoExported"
    MIC_DISABLED = "microphoneDisabled"
    VIDEO_DELETED = "videoDeleted"
    SCHEDULE_CHANGED = "recordingScheduleChanged"
    # ---
    MOTION_SENSOR = "sensorMotion"
    SENSOR_OPENED = "sensorOpened"
    SENSOR_CLOSED = "sensorClosed"
    SENSOR_ALARM = "sensorAlarm"
    SENSOR_EXTREME_VALUE = "sensorExtremeValues"
    SENSOR_WATER_LEAK = "sensorWaterLeak"
    SENSOR_BATTERY_LOW = "sensorBatteryLow"
    # ---
    MOTION_LIGHT = "lightMotion"
    # ---
    DOORLOCK_OPEN = "doorlockOpened"
    DOORLOCK_CLOSE = "doorlockClosed"
    DOORLOCK_BATTERY_LOW = "doorlockBatteryLow"
    # ---
    DISRUPTED_CONDITIONS = "ringDisruptedConditions"
    # ---
    RECORDING_OFF = "recordingOff"

    @staticmethod
    @cache
    def device_events() -> list[str]:
        return [
            EventType.MOTION.value,
            EventType.NFC_CARD_SCANNED.value,
            EventType.FINGERPRINT_IDENTIFIED.value,
            EventType.RING.value,
            EventType.SMART_DETECT.value,
            EventType.SMART_AUDIO_DETECT.value,
            EventType.SMART_DETECT_LINE.value,
        ]

    @staticmethod
    @cache
    def device_events_set() -> set[str]:
        return set(EventType.device_events())

    @staticmethod
    @cache
    def motion_events() -> list[str]:
        return [EventType.MOTION.value, EventType.SMART_DETECT.value]


@enum.unique
class StateType(str, ValuesEnumMixin, enum.Enum):
    CONNECTED = "CONNECTED"
    CONNECTING = "CONNECTING"
    DISCONNECTED = "DISCONNECTED"


@enum.unique
class ProtectWSPayloadFormat(int, enum.Enum):
    """Websocket Payload formats."""

    JSON = 1
    UTF8String = 2
    NodeBuffer = 3


@enum.unique
class SmartDetectObjectType(str, ValuesEnumMixin, enum.Enum):
    PERSON = "person"
    ANIMAL = "animal"
    VEHICLE = "vehicle"
    LICENSE_PLATE = "licensePlate"
    PACKAGE = "package"
    SMOKE = "alrmSmoke"
    CMONX = "alrmCmonx"
    SIREN = "alrmSiren"
    BABY_CRY = "alrmBabyCry"
    SPEAK = "alrmSpeak"
    BARK = "alrmBark"
    BURGLAR = "alrmBurglar"
    CAR_HORN = "alrmCarHorn"
    GLASS_BREAK = "alrmGlassBreak"
    FACE = "face"
    # old?
    CAR = "car"
    PET = "pet"

    @cached_property
    def audio_type(self) -> SmartDetectAudioType | None:
        return OBJECT_TO_AUDIO_MAP.get(self)


@enum.unique
class SmartDetectAudioType(str, ValuesEnumMixin, enum.Enum):
    SMOKE = "alrmSmoke"
    CMONX = "alrmCmonx"
    SMOKE_CMONX = "smoke_cmonx"
    SIREN = "alrmSiren"
    BABY_CRY = "alrmBabyCry"
    SPEAK = "alrmSpeak"
    BARK = "alrmBark"
    BURGLAR = "alrmBurglar"
    CAR_HORN = "alrmCarHorn"
    GLASS_BREAK = "alrmGlassBreak"


@enum.unique
class DetectionColor(str, ValuesEnumMixin, enum.Enum):
    BLACK = "black"
    BLUE = "blue"
    BROWN = "brown"
    GRAY = "gray"
    GREEN = "green"
    ORANGE = "orange"
    PINK = "pink"
    PURPLE = "purple"
    RED = "red"
    WHITE = "white"
    YELLOW = "yellow"


OBJECT_TO_AUDIO_MAP = {
    SmartDetectObjectType.SMOKE: SmartDetectAudioType.SMOKE,
    SmartDetectObjectType.CMONX: SmartDetectAudioType.CMONX,
    SmartDetectObjectType.SIREN: SmartDetectAudioType.SIREN,
    SmartDetectObjectType.BABY_CRY: SmartDetectAudioType.BABY_CRY,
    SmartDetectObjectType.SPEAK: SmartDetectAudioType.SPEAK,
    SmartDetectObjectType.BARK: SmartDetectAudioType.BARK,
    SmartDetectObjectType.BURGLAR: SmartDetectAudioType.BURGLAR,
    SmartDetectObjectType.CAR_HORN: SmartDetectAudioType.CAR_HORN,
    SmartDetectObjectType.GLASS_BREAK: SmartDetectAudioType.GLASS_BREAK,
}


@enum.unique
class DoorbellMessageType(str, ValuesEnumMixin, enum.Enum):
    LEAVE_PACKAGE_AT_DOOR = "LEAVE_PACKAGE_AT_DOOR"
    DO_NOT_DISTURB = "DO_NOT_DISTURB"
    CUSTOM_MESSAGE = "CUSTOM_MESSAGE"
    IMAGE = "IMAGE"


@enum.unique
class LightModeEnableType(str, ValuesEnumMixin, enum.Enum):
    DARK = "dark"
    ALWAYS = "fulltime"
    NIGHT = "night"


@enum.unique
class LightModeType(str, ValuesEnumMixin, enum.Enum):
    MOTION = "motion"
    WHEN_DARK = "always"
    MANUAL = "off"
    SCHEDULE = "schedule"


@enum.unique
class VideoMode(str, ValuesEnumMixin, enum.Enum):
    DEFAULT = "default"
    HIGH_FPS = "highFps"
    HOMEKIT = "homekit"
    SPORT = "sport"
    SLOW_SHUTTER = "slowShutter"
    LPR_NONE_REFLEX = "lprNoneReflex"
    LPR_REFLEX = "lprReflex"
    # should only be for unadopted devices
    UNKNOWN = "unknown"


@enum.unique
class AudioStyle(str, UnknownValuesEnumMixin, enum.Enum):
    NATURE = "nature"
    NOISE_REDUCED = "noiseReduced"


@enum.unique
class RecordingMode(str, ValuesEnumMixin, enum.Enum):
    ADAPTIVE = "adaptive"
    ALWAYS = "always"
    NEVER = "never"
    SCHEDULE = "schedule"
    DETECTIONS = "detections"


@enum.unique
class AnalyticsOption(str, ValuesEnumMixin, enum.Enum):
    NONE = "none"
    ANONYMOUS = "anonymous"
    FULL = "full"


@enum.unique
class RecordingType(str, ValuesEnumMixin, enum.Enum):
    TIMELAPSE = "timelapse"
    CONTINUOUS = "rotating"
    DETECTIONS = "detections"


@enum.unique
class ResolutionStorageType(str, ValuesEnumMixin, enum.Enum):
    UHD = "4K"
    HD = "HD"
    FREE = "free"


@enum.unique
class IRLEDMode(str, UnknownValuesEnumMixin, enum.Enum):
    AUTO = "auto"
    ON = "on"
    AUTO_NO_LED = "autoFilterOnly"
    OFF = "off"
    MANUAL = "manual"
    CUSTOM = "custom"
    UNKNOWN = "unknown"


@enum.unique
class MountType(str, ValuesEnumMixin, enum.Enum):
    NONE = "none"
    LEAK = "leak"
    DOOR = "door"
    WINDOW = "window"
    GARAGE = "garage"


@enum.unique
class SensorType(str, ValuesEnumMixin, enum.Enum):
    TEMPERATURE = "temperature"
    LIGHT = "light"
    HUMIDITY = "humidity"


@enum.unique
class SensorStatusType(str, UnknownValuesEnumMixin, enum.Enum):
    OFFLINE = "offline"
    UNKNOWN = "unknown"
    SAFE = "safe"
    NEUTRAL = "neutral"
    LOW = "low"
    HIGH = "high"


@enum.unique
class SleepStateType(str, ValuesEnumMixin, enum.Enum):
    DISCONNECTED = "disconnected"
    AWAKE = "awake"
    START_SLEEP = "goingToSleep"
    ASLEEP = "asleep"
    WAKING = "waking"


@enum.unique
class AutoExposureMode(str, ValuesEnumMixin, enum.Enum):
    MANUAL = "manual"
    AUTO = "auto"
    NONE = "none"
    SHUTTER = "shutter"
    FLICK50 = "flick50"
    FLICK60 = "flick60"


@enum.unique
class FocusMode(str, ValuesEnumMixin, enum.Enum):
    MANUAL = "manual"
    AUTO = "auto"
    NONE = "none"
    ZTRIG = "ztrig"
    TOUCH = "touch"


@enum.unique
class MountPosition(str, UnknownValuesEnumMixin, enum.Enum):
    CEILING = "ceiling"
    WALL = "wall"
    DESK = "desk"
    NONE = "none"
    UNKNOWN = "unknown"


@enum.unique
class GeofencingSetting(str, ValuesEnumMixin, enum.Enum):
    OFF = "off"
    ALL_AWAY = "allAway"


@enum.unique
class MotionAlgorithm(str, ValuesEnumMixin, enum.Enum):
    STABLE = "stable"
    ENHANCED = "enhanced"


@enum.unique
class AudioCodecs(str, ValuesEnumMixin, enum.Enum):
    AAC = "aac"
    VORBIS = "vorbis"
    OPUS = "opus"


@enum.unique
class LowMedHigh(str, ValuesEnumMixin, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@enum.unique
class StorageType(str, UnknownValuesEnumMixin, enum.Enum):
    DISK = "hdd"
    RAID = "raid"
    SD_CARD = "sdcard"
    INTERNAL_SSD = "internalSSD"
    UNKNOWN = "UNKNOWN"


@enum.unique
class FirmwareReleaseChannel(str, ValuesEnumMixin, enum.Enum):
    INTERNAL = "internal"
    ALPHA = "alpha"
    BETA = "beta"
    RELEASE_CANDIDATE = "release-candidate"
    RELEASE = "release"


@enum.unique
class ChimeType(int, enum.Enum):
    NONE = 0
    MECHANICAL = 300
    DIGITAL = 1000


@enum.unique
class LockStatusType(str, ValuesEnumMixin, enum.Enum):
    OPEN = "OPEN"
    OPENING = "OPENING"
    CLOSED = "CLOSED"
    CLOSING = "CLOSING"
    JAMMED_WHILE_CLOSING = "JAMMED_WHILE_CLOSING"
    JAMMED_WHILE_OPENING = "JAMMED_WHILE_OPENING"
    FAILED_WHILE_CLOSING = "FAILED_WHILE_CLOSING"
    FAILED_WHILE_OPENING = "FAILED_WHILE_OPENING"
    NOT_CALIBRATED = "NOT_CALIBRATED"
    AUTO_CALIBRATION_IN_PROGRESS = "AUTO_CALIBRATION_IN_PROGRESS"
    CALIBRATION_WAITING_OPEN = "CALIBRATION_WAITING_OPEN"
    CALIBRATION_WAITING_CLOSE = "CALIBRATION_WAITING_CLOSE"


@enum.unique
class PermissionNode(str, UnknownValuesEnumMixin, enum.Enum):
    CREATE = "create"
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    READ_MEDIA = "readmedia"
    DELETE_MEDIA = "deletemedia"
    READ_LIVE = "readlive"
    UNKNOWN = "unknown"

    @cached_property
    def name(self) -> str:
        """Return the name."""
        return self._name_

    @cached_property
    def value(self) -> str:
        """Return the value."""
        return self._value_


@enum.unique
class HDRMode(str, UnknownValuesEnumMixin, enum.Enum):
    NONE = "none"
    NORMAL = "normal"
    ALWAYS_ON = "superHdr"


@enum.unique
class LensType(str, enum.Enum):
    NONE = "none"
    FULL_360 = "360"
    WIDE = "wide"
    TELESCOPIC = "tele"
    DLSR_17 = "m43"


DoorbellText = Annotated[str, StringConstraints(max_length=30)]

ICRCustomValue = Annotated[int, Field(ge=0, le=11)]

ICRLuxValue = Annotated[int, Field(ge=1, le=30)]

LEDLevel = Annotated[int, Field(ge=0, le=6)]

PercentInt = Annotated[int, Field(ge=0, le=101)]

TwoByteInt = Annotated[int, Field(ge=1, le=256)]

PercentFloat = Annotated[float, Field(ge=0, le=100)]

WDRLevel = Annotated[int, Field(ge=0, le=4)]

ICRSensitivity = Annotated[int, Field(ge=0, le=4)]

Percent = Annotated[float, Field(ge=0, le=1)]

RepeatTimes = Annotated[int, Field(ge=1, le=6)]


class PTZPositionDegree(BaseModel):
    pan: float
    tilt: float
    zoom: float


class PTZPositionSteps(BaseModel):
    focus: int
    pan: int
    tilt: int
    zoom: int


class PTZPosition(BaseModel):
    degree: PTZPositionDegree
    steps: PTZPositionSteps


class PTZPresetPosition(BaseModel):
    pan: int
    tilt: int
    zoom: int


class PTZPreset(BaseModel):
    id: str
    name: str
    slot: int
    ptz: PTZPresetPosition


CoordType = Union[Percent, int, float]


class Version(BaseVersion):
    def __str__(self) -> str:
        super_str = super().__str__()
        if self.pre is not None and self.pre[0] == "b":
            super_str = super_str.replace("b", "-beta.")
        return super_str
