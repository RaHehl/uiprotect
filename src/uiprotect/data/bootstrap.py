"""UniFi Protect Bootstrap."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from aiohttp.client_exceptions import ServerDisconnectedError
from convertertools import pop_dict_set, pop_dict_tuple
from pydantic import PrivateAttr, ValidationError

from ..exceptions import ClientError
from ..utils import normalize_mac, to_snake_case, utc_now
from .base import (
    RECENT_EVENT_MAX,
    ProtectBaseObject,
    ProtectDeviceModel,
    ProtectModel,
    ProtectModelWithId,
)
from .convert import MODEL_TO_CLASS, create_from_unifi_dict
from .devices import (
    AiPort,
    Bridge,
    Camera,
    Chime,
    Doorlock,
    Light,
    ProtectAdoptableDeviceModel,
    Ringtone,
    Sensor,
    Viewer,
)
from .nvr import NVR, Event, Liveview
from .types import EventType, FixSizeOrderedDict, ModelType
from .user import Group, Keyrings, UlpUserKeyringBase, UlpUsers, User
from .websocket import (
    WSAction,
    WSPacket,
    WSSubscriptionMessage,
)

if TYPE_CHECKING:
    from ..api import ProtectApiClient


_LOGGER = logging.getLogger(__name__)

MAX_SUPPORTED_CAMERAS = 256
MAX_EVENT_HISTORY_IN_STATE_MACHINE = MAX_SUPPORTED_CAMERAS * 2
STATS_KEYS = {
    "eventStats",
    "storageStats",
    "stats",
    "systemInfo",
    "phyRate",
    "wifiConnectionState",
    "upSince",
    "uptime",
    "lastSeen",
    "recordingSchedules",
}

IGNORE_DEVICE_KEYS = {"nvrMac", "guid"}
STATS_AND_IGNORE_DEVICE_KEYS = STATS_KEYS | IGNORE_DEVICE_KEYS

_IGNORE_KEYS_BY_MODEL_TYPE = {
    #
    # `lastMotion` from cameras update every 100 milliseconds when a motion event is active
    # this overrides the behavior to only update `lastMotion` when a new event starts
    #
    ModelType.CAMERA: {"lastMotion"},
    #
    # `cameraIds` is updated every 10s, but we don't need to process it since bootstrap
    # is resynced every so often anyways.
    #
    ModelType.CHIME: {"cameraIds"},
}


IGNORE_DEVICE_KEYS_BY_MODEL_TYPE = {
    model_type: IGNORE_DEVICE_KEYS | keys
    for model_type, keys in _IGNORE_KEYS_BY_MODEL_TYPE.items()
}
STATS_AND_IGNORE_DEVICE_KEYS_BY_MODEL_TYPE = {
    model_type: STATS_AND_IGNORE_DEVICE_KEYS | keys
    for model_type, keys in _IGNORE_KEYS_BY_MODEL_TYPE.items()
}


CAMERA_EVENT_ATTR_MAP: dict[EventType, tuple[str, str]] = {
    EventType.MOTION: ("last_motion", "last_motion_event_id"),
    EventType.SMART_DETECT: ("last_smart_detect", "last_smart_detect_event_id"),
    EventType.SMART_DETECT_LINE: ("last_smart_detect", "last_smart_detect_event_id"),
    EventType.SMART_AUDIO_DETECT: (
        "last_smart_audio_detect",
        "last_smart_audio_detect_event_id",
    ),
    EventType.RING: ("last_ring", "last_ring_event_id"),
    EventType.NFC_CARD_SCANNED: (
        "last_nfc_card_scanned",
        "last_nfc_card_scanned_event_id",
    ),
    EventType.FINGERPRINT_IDENTIFIED: (
        "last_fingerprint_identified",
        "last_fingerprint_identified_event_id",
    ),
}


def _process_light_event(event: Event, light: Light) -> None:
    light.last_motion_event_id = event.id


def _process_sensor_event(event: Event, sensor: Sensor) -> None:
    if event.type is EventType.MOTION_SENSOR:
        sensor.last_motion_event_id = event.id
    elif event.type in {EventType.SENSOR_CLOSED, EventType.SENSOR_OPENED}:
        sensor.last_contact_event_id = event.id
    elif event.type is EventType.SENSOR_EXTREME_VALUE:
        sensor.extreme_value_detected_at = event.end
        sensor.last_value_event_id = event.id
    elif event.type is EventType.SENSOR_ALARM:
        sensor.last_value_event_id = event.id


_CAMERA_SMART_AND_LINE_EVENTS = {
    EventType.SMART_DETECT,
    EventType.SMART_DETECT_LINE,
}
_CAMERA_SMART_AUDIO_EVENT = EventType.SMART_AUDIO_DETECT


def _process_camera_event(event: Event, camera: Camera) -> None:
    event_type = event.type
    dt_attr, event_attr = CAMERA_EVENT_ATTR_MAP[event_type]
    event_id = event.id
    event_start = event.start

    setattr(camera, event_attr, event_id)
    setattr(camera, dt_attr, event_start)
    if event_type in _CAMERA_SMART_AND_LINE_EVENTS:
        for smart_type in event.smart_detect_types:
            camera.last_smart_detect_event_ids[smart_type] = event_id
            camera.last_smart_detects[smart_type] = event_start
    elif event_type is _CAMERA_SMART_AUDIO_EVENT:
        for smart_type in event.smart_detect_types:
            if (audio_type := smart_type.audio_type) is None:
                continue
            camera.last_smart_audio_detect_event_ids[audio_type] = event_id
            camera.last_smart_audio_detects[audio_type] = event_start


@dataclass
class WSStat:
    model: str
    action: str
    keys: list[str]
    keys_set: list[str]
    size: int
    filtered: bool


class ProtectDeviceRef(ProtectBaseObject):
    model: ModelType
    id: str


class Bootstrap(ProtectBaseObject):
    auth_user_id: str
    access_key: str
    cameras: dict[str, Camera]
    users: dict[str, User]
    groups: dict[str, Group]
    liveviews: dict[str, Liveview]
    nvr: NVR
    viewers: dict[str, Viewer]
    lights: dict[str, Light]
    bridges: dict[str, Bridge]
    sensors: dict[str, Sensor]
    doorlocks: dict[str, Doorlock]
    chimes: dict[str, Chime]
    aiports: dict[str, AiPort]
    ringtones: list[Ringtone]
    last_update_id: str

    # TODO:
    # schedules
    # agreements

    # not directly from UniFi
    keyrings: Keyrings = Keyrings()
    ulp_users: UlpUsers = UlpUsers()
    events: dict[str, Event] = FixSizeOrderedDict()
    capture_ws_stats: bool = False
    mac_lookup: dict[str, ProtectDeviceRef] = {}
    id_lookup: dict[str, ProtectDeviceRef] = {}
    _ws_stats: list[WSStat] = PrivateAttr([])
    _has_doorbell: bool | None = PrivateAttr(None)
    _has_smart: bool | None = PrivateAttr(None)
    _has_media: bool | None = PrivateAttr(None)
    _recording_start: datetime | None = PrivateAttr(None)
    _refresh_tasks: set[asyncio.Task[None]] = PrivateAttr(set())

    @classmethod
    def unifi_dict_to_dict(cls, data: dict[str, Any]) -> dict[str, Any]:
        api: ProtectApiClient | None = data.get("api") or (
            cls._api if isinstance(cls, ProtectBaseObject) else None
        )
        mac_lookup: dict[str, dict[str, str | ModelType]] = {}
        id_lookup: dict[str, dict[str, str | ModelType]] = {}
        data["idLookup"] = id_lookup
        data["macLookup"] = mac_lookup

        for model_type in ModelType.bootstrap_models_types_set:
            key = model_type.devices_key  # type: ignore[attr-defined]
            items: dict[str, ProtectModel] = {}
            if key not in data:
                data[key] = {}
                _LOGGER.error(
                    f"Missing key in bootstrap: {key}. This may be fixed by updating Protect."
                )
                continue
            for item in data[key]:
                if (
                    api is not None
                    and api.ignore_unadopted
                    and not item.get("isAdopted", True)
                ):
                    continue

                id_: str = item["id"]
                ref = {"model": model_type, "id": id_}
                items[id_] = item
                id_lookup[id_] = ref
                if "mac" in item:
                    cleaned_mac = normalize_mac(item["mac"])
                    mac_lookup[cleaned_mac] = ref
            data[key] = items

        return super().unifi_dict_to_dict(data)

    def unifi_dict(
        self,
        data: dict[str, Any] | None = None,
        exclude: set[str] | None = None,
    ) -> dict[str, Any]:
        data = super().unifi_dict(data=data, exclude=exclude)

        pop_dict_tuple(data, ("events", "captureWsStats", "macLookup", "idLookup"))
        for model_type in ModelType.bootstrap_models_types_set:
            attr = model_type.devices_key  # type: ignore[attr-defined]
            if attr in data and isinstance(data[attr], dict):
                data[attr] = list(data[attr].values())

        return data

    @property
    def ws_stats(self) -> list[WSStat]:
        return self._ws_stats

    def clear_ws_stats(self) -> None:
        self._ws_stats = []

    @property
    def auth_user(self) -> User:
        return self._api.bootstrap.users[self.auth_user_id]

    @property
    def has_doorbell(self) -> bool:
        if self._has_doorbell is None:
            self._has_doorbell = any(
                c.feature_flags.is_doorbell for c in self.cameras.values()
            )

        return self._has_doorbell

    @property
    def recording_start(self) -> datetime | None:
        """Get earilest recording date."""
        if self._recording_start is None:
            try:
                self._recording_start = min(
                    c.stats.video.recording_start
                    for c in self.cameras.values()
                    if c.stats.video.recording_start is not None
                )
            except ValueError:
                return None
        return self._recording_start

    @property
    def has_smart_detections(self) -> bool:
        """Check if any camera has smart detections."""
        if self._has_smart is None:
            self._has_smart = any(
                c.feature_flags.has_smart_detect for c in self.cameras.values()
            )
        return self._has_smart

    @property
    def has_media(self) -> bool:
        """Checks if user can read media for any camera."""
        if self._has_media is None:
            if self.recording_start is None:
                return False
            self._has_media = any(
                c.can_read_media(self.auth_user) for c in self.cameras.values()
            )
        return self._has_media

    def get_device_from_mac(self, mac: str) -> ProtectAdoptableDeviceModel | None:
        """Retrieve a device from MAC address."""
        return self._get_device_from_ref(self.mac_lookup.get(normalize_mac(mac)))

    def get_device_from_id(self, device_id: str) -> ProtectAdoptableDeviceModel | None:
        """Retrieve a device from device ID (without knowing model type)."""
        return self._get_device_from_ref(self.id_lookup.get(device_id))

    def _get_device_from_ref(
        self, ref: ProtectDeviceRef | None
    ) -> ProtectAdoptableDeviceModel | None:
        if ref is None:
            return None
        devices_key = ref.model.devices_key
        devices: dict[str, ProtectAdoptableDeviceModel] = getattr(self, devices_key)
        return devices[ref.id]

    def process_event(self, event: Event) -> None:
        event_type = event.type
        if event_type in CAMERA_EVENT_ATTR_MAP and (camera := event.camera):
            _process_camera_event(event, camera)
        elif event_type is EventType.MOTION_LIGHT and (light := event.light):
            _process_light_event(event, light)
        elif event_type is EventType.MOTION_SENSOR and (sensor := event.sensor):
            _process_sensor_event(event, sensor)

        self.events[event.id] = event

    def _process_add_packet(
        self,
        model_type: ModelType,
        data: dict[str, Any],
    ) -> WSSubscriptionMessage | None:
        obj = create_from_unifi_dict(data, api=self._api, model_type=model_type)
        if model_type is ModelType.EVENT:
            if TYPE_CHECKING:
                assert isinstance(obj, Event)
            self.process_event(obj)
        elif model_type is ModelType.NVR:
            if TYPE_CHECKING:
                assert isinstance(obj, NVR)
            self.nvr = obj
        elif model_type in ModelType.bootstrap_models_types_set:
            if TYPE_CHECKING:
                assert isinstance(obj, ProtectAdoptableDeviceModel)
            if not self._api.ignore_unadopted or (
                obj.is_adopted and not obj.is_adopted_by_other
            ):
                id_ = obj.id
                getattr(self, model_type.devices_key)[id_] = obj
                ref = ProtectDeviceRef(model=model_type, id=id_)
                self.id_lookup[id_] = ref
                self.mac_lookup[normalize_mac(obj.mac)] = ref
        else:
            _LOGGER.debug("Unexpected bootstrap model type for add: %s", model_type)
            return None

        return WSSubscriptionMessage(
            action=WSAction.ADD,
            new_update_id=self.last_update_id,
            changed_data=obj.model_dump(),
            new_obj=obj,
        )

    def _process_remove_packet(
        self, model_type: ModelType, action: dict[str, Any]
    ) -> WSSubscriptionMessage | None:
        devices_key = model_type.devices_key
        devices: dict[str, ProtectDeviceModel] | None = getattr(self, devices_key, None)
        if devices is None:
            return None

        device_id: str = action["id"]
        self.id_lookup.pop(device_id, None)
        if (device := devices.pop(device_id, None)) is None:
            return None
        self.mac_lookup.pop(normalize_mac(device.mac), None)
        return WSSubscriptionMessage(
            action=WSAction.REMOVE,
            new_update_id=self.last_update_id,
            changed_data={},
            old_obj=device,
        )

    def _process_ws_keyring_or_ulp_user_message(
        self,
        action: dict[str, Any],
        data: dict[str, Any],
        model_type: ModelType,
    ) -> WSSubscriptionMessage | None:
        action_id = action["id"]
        obj_from_bootstrap: UlpUserKeyringBase[ProtectModelWithId] = getattr(
            self, to_snake_case(model_type.devices_key)
        )
        action_type = action["action"]
        if action_type == "add":
            add_obj = create_from_unifi_dict(data, api=self._api, model_type=model_type)
            if TYPE_CHECKING:
                model_class = MODEL_TO_CLASS.get(model_type)
                assert model_class is not None and isinstance(add_obj, model_class)
            add_obj = cast(ProtectModelWithId, add_obj)
            obj_from_bootstrap.add(add_obj)
            return WSSubscriptionMessage(
                action=WSAction.ADD,
                new_update_id=self.last_update_id,
                changed_data=add_obj.model_dump(),
                new_obj=add_obj,
            )
        elif action_type == "remove":
            to_remove = obj_from_bootstrap.by_id(action_id)
            if to_remove is None:
                return None
            obj_from_bootstrap.remove(to_remove)
            return WSSubscriptionMessage(
                action=WSAction.REMOVE,
                new_update_id=self.last_update_id,
                changed_data={},
                old_obj=to_remove,
            )
        elif action_type == "update":
            updated_obj = obj_from_bootstrap.by_id(action_id)
            if updated_obj is None:
                return None

            old_obj = updated_obj.model_copy()
            updated_data = {to_snake_case(k): v for k, v in data.items()}
            updated_obj.update_from_dict(updated_data)

            return WSSubscriptionMessage(
                action=WSAction.UPDATE,
                new_update_id=self.last_update_id,
                changed_data=updated_data,
                new_obj=updated_obj,
                old_obj=old_obj,
            )
        _LOGGER.debug("Unexpected ws action for %s: %s", model_type, action_type)
        return None

    def _process_nvr_update(
        self,
        action: dict[str, Any],
        data: dict[str, Any],
        ignore_stats: bool,
    ) -> WSSubscriptionMessage | None:
        if ignore_stats:
            pop_dict_set(data, STATS_KEYS)
        # nothing left to process
        if not data:
            return None

        # for another NVR in stack
        nvr_id: str | None = action.get("id")
        if nvr_id and nvr_id != self.nvr.id:
            return None

        # nothing left to process
        if not (data := self.nvr.unifi_dict_to_dict(data)):
            return None

        old_nvr = self.nvr.model_copy()
        self.nvr = self.nvr.update_from_dict(data)

        return WSSubscriptionMessage(
            action=WSAction.UPDATE,
            new_update_id=self.last_update_id,
            changed_data=data,
            new_obj=self.nvr,
            old_obj=old_nvr,
        )

    def _process_device_update(
        self,
        model_type: ModelType,
        action: dict[str, Any],
        data: dict[str, Any],
        ignore_stats: bool,
        is_ping_back: bool,
    ) -> WSSubscriptionMessage | None:
        """
        Process a device update packet.

        If is_ping_back is True, the packet is an empty packet
        that was generated internally as a result of an event
        that will expire and result in a state change.
        """
        if ignore_stats:
            remove_keys = STATS_AND_IGNORE_DEVICE_KEYS_BY_MODEL_TYPE.get(
                model_type, STATS_AND_IGNORE_DEVICE_KEYS
            )
        else:
            remove_keys = IGNORE_DEVICE_KEYS_BY_MODEL_TYPE.get(
                model_type, IGNORE_DEVICE_KEYS
            )

        pop_dict_set(data, remove_keys)

        # nothing left to process
        if not data and not is_ping_back:
            return None

        devices: dict[str, ProtectModelWithId] = getattr(self, model_type.devices_key)
        action_id: str = action["id"]
        if action_id not in devices:
            # ignore updates to events that phase out
            if model_type is not ModelType.EVENT:
                _LOGGER.debug("Unexpected %s: %s", model_type, action_id)
            return None

        obj = devices[action_id]
        data = obj.unifi_dict_to_dict(data)

        if not data and not is_ping_back:
            # nothing left to process
            return None

        old_obj = obj.model_copy()
        obj = obj.update_from_dict(data)

        if model_type is ModelType.EVENT:
            if TYPE_CHECKING:
                assert isinstance(obj, Event)
            self.process_event(obj)
        elif model_type is ModelType.SENSOR:
            if TYPE_CHECKING:
                assert isinstance(obj, Sensor)
            if "alarm_triggered_at" in data and (trigged_at := obj.alarm_triggered_at):
                if is_recent := trigged_at + RECENT_EVENT_MAX >= utc_now():
                    obj.set_alarm_timeout()
                _LOGGER.debug("alarm_triggered_at for %s (%s)", obj.id, is_recent)

        devices[action_id] = obj
        return WSSubscriptionMessage(
            action=WSAction.UPDATE,
            new_update_id=self.last_update_id,
            changed_data=data,
            new_obj=obj,
            old_obj=old_obj,
        )

    def process_ws_packet(
        self,
        packet: WSPacket,
        models: set[ModelType] | None = None,
        ignore_stats: bool = False,
        is_ping_back: bool = False,
    ) -> WSSubscriptionMessage | None:
        """Process a WS packet."""
        capture_ws_stats = self.capture_ws_stats
        action = packet.action_frame.data
        data = packet.data_frame.data
        keys = list(data) if capture_ws_stats else None

        new_update_id: str | None = action["newUpdateId"]
        if new_update_id is not None:
            self.last_update_id = new_update_id

        message = self._make_ws_packet_message(
            action, data, models, ignore_stats, is_ping_back
        )

        if capture_ws_stats:
            if TYPE_CHECKING:
                assert keys is not None
            self._ws_stats.append(
                WSStat(
                    model=action["modelKey"],
                    action=action["action"],
                    keys=keys,
                    keys_set=[] if message is None else list(message.changed_data),
                    size=len(packet.raw),
                    filtered=message is None,
                ),
            )

        return message

    def _make_ws_packet_message(
        self,
        action: dict[str, Any],
        data: dict[str, Any],
        models: set[ModelType] | None,
        ignore_stats: bool,
        is_ping_back: bool,
    ) -> WSSubscriptionMessage | None:
        """Process a WS packet."""
        model_key: str = action["modelKey"]
        if (model_type := ModelType.from_string(model_key)) is ModelType.UNKNOWN:
            _LOGGER.debug("Unknown model type: %s", model_key)
            return None

        if models and model_type not in models:
            return None

        action_action: str = action["action"]

        try:
            if model_type in {ModelType.KEYRING, ModelType.ULP_USER}:
                return self._process_ws_keyring_or_ulp_user_message(
                    action, data, model_type
                )
            if action_action == "remove":
                return self._process_remove_packet(model_type, action)
            if not data and not is_ping_back:
                return None
            if action_action == "add":
                return self._process_add_packet(model_type, data)
            if action_action == "update":
                if model_type is ModelType.NVR:
                    return self._process_nvr_update(action, data, ignore_stats)
                if model_type in ModelType.bootstrap_models_types_and_event_set:
                    return self._process_device_update(
                        model_type, action, data, ignore_stats, is_ping_back
                    )
        except (ValidationError, ValueError) as err:
            self._handle_ws_error(action_action, model_type, action, err)

        _LOGGER.debug(
            "Unexpected bootstrap model type deviceadoptedfor update: %s", model_key
        )
        return None

    def _handle_ws_error(
        self,
        action_action: str,
        model_type: ModelType,
        action: dict[str, Any],
        err: Exception,
    ) -> None:
        msg = ""
        device_id: str = action["id"]
        if model_type is ModelType.EVENT:
            msg = f"Validation error processing event: {device_id}. Ignoring event."
        else:
            task = asyncio.create_task(self.refresh_device(model_type, device_id))
            self._refresh_tasks.add(task)
            task.add_done_callback(self._refresh_tasks.discard)
            msg = (
                f"{action_action} packet caused invalid state. "
                f"Refreshing device: {model_type} {device_id}"
            )
        _LOGGER.debug("%s Error: %s", msg, err)

    async def refresh_device(self, model_type: ModelType, device_id: str) -> None:
        """Refresh a device in the bootstrap."""
        try:
            if model_type is ModelType.NVR:
                device: ProtectModelWithId = await self._api.get_nvr()
            else:
                device = await self._api.get_device(model_type, device_id)
        except (
            ValidationError,
            TimeoutError,
            asyncio.TimeoutError,
            ClientError,
            ServerDisconnectedError,
        ):
            _LOGGER.warning("Failed to refresh model: %s %s", model_type, device_id)
            return

        if isinstance(device, NVR):
            self.nvr = device
        else:
            devices_key = model_type.devices_key
            devices: dict[str, ProtectModelWithId] = getattr(self, devices_key)
            devices[device.id] = device
        _LOGGER.debug("Successfully refresh model: %s %s", model_type, device_id)

    async def get_is_prerelease(self) -> bool:
        """[DEPRECATED] Always returns False. Will be removed after HA 2025.8.0."""
        return False
