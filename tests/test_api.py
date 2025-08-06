"""Tests for uiprotect.unifi_protect_server."""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import datetime, timedelta
from io import BytesIO
from ipaddress import IPv4Address
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from PIL import Image

from tests.conftest import (
    TEST_BRIDGE_EXISTS,
    TEST_CAMERA_EXISTS,
    TEST_HEATMAP_EXISTS,
    TEST_LIGHT_EXISTS,
    TEST_LIVEVIEW_EXISTS,
    TEST_PUBLIC_API_SNAPSHOT_EXISTS,
    TEST_SENSOR_EXISTS,
    TEST_SMART_TRACK_EXISTS,
    TEST_SNAPSHOT_EXISTS,
    TEST_THUMBNAIL_EXISTS,
    TEST_VIDEO_EXISTS,
    TEST_VIEWPORT_EXISTS,
    MockDatetime,
    compare_objs,
    get_time,
    validate_video_file,
)
from tests.sample_data.constants import CONSTANTS
from uiprotect.api import ProtectApiClient
from uiprotect.data import (
    Camera,
    Event,
    EventType,
    ModelType,
    create_from_unifi_dict,
)
from uiprotect.data.types import Version, VideoMode
from uiprotect.exceptions import BadRequest, NotAuthorized, NvrError
from uiprotect.utils import to_js_time

from .common import assert_equal_dump

OLD_VERSION = Version("1.2.3")
NFC_FINGERPRINT_SUPPORT_VERSION = Version("5.1.57")

if TYPE_CHECKING:
    from uiprotect.data.base import ProtectAdoptableDeviceModel
    from uiprotect.data.bootstrap import Bootstrap


async def check_motion_event(event: Event):
    data = await event.get_thumbnail()
    assert data is not None
    img = Image.open(BytesIO(data))
    assert img.format in {"PNG", "JPEG"}

    data = await event.get_heatmap()
    assert data is not None
    img = Image.open(BytesIO(data))
    assert img.format in {"PNG", "JPEG"}


async def check_camera(camera: Camera):
    if camera.last_motion_event is not None:
        await check_motion_event(camera.last_motion_event)

    if camera.last_smart_detect_event is not None:
        await check_motion_event(camera.last_smart_detect_event)

    assert camera.last_nfc_card_scanned is None
    assert camera.last_fingerprint_identified is None

    for channel in camera.channels:
        assert channel._api is not None

    data = await camera.get_snapshot()
    assert data is not None
    img = Image.open(BytesIO(data))
    assert img.format in {"PNG", "JPEG"}

    pub_data = await camera.get_public_api_snapshot()
    assert pub_data is not None
    pub_img = Image.open(BytesIO(pub_data))
    assert pub_img.format in {"PNG", "JPEG"}

    camera.last_ring_event  # noqa: B018

    assert camera.timelapse_url == f"https://127.0.0.1:0/protect/timelapse/{camera.id}"
    check_device(camera)

    for channel in camera.channels:
        if channel.is_rtsp_enabled:
            for _ in range(2):
                assert (
                    channel.rtsp_url
                    == f"rtsp://{camera.api.connection_host}:7447/{channel.rtsp_alias}"
                )
                assert (
                    channel.rtsps_url
                    == f"rtsps://{camera.api.connection_host}:7441/{channel.rtsp_alias}?enableSrtp"
                )
                assert (
                    channel.rtsps_no_srtp_url
                    == f"rtsps://{camera.api.connection_host}:7441/{channel.rtsp_alias}"
                )

    if VideoMode.HIGH_FPS in camera.feature_flags.video_modes:
        assert camera.feature_flags.has_highfps
    else:
        assert not camera.feature_flags.has_highfps

    if camera.feature_flags.has_hdr:
        assert not camera.feature_flags.has_wdr
    else:
        assert camera.feature_flags.has_wdr


def check_device(device: ProtectAdoptableDeviceModel):
    assert device.protect_url == f"https://127.0.0.1:0/protect/devices/{device.id}"


async def check_bootstrap(bootstrap: Bootstrap):
    bootstrap.api._api_key = "test_api_key"
    assert bootstrap.auth_user
    assert (
        bootstrap.nvr.protect_url
        == f"https://127.0.0.1:0/protect/devices/{bootstrap.nvr.id}"
    )

    for light in bootstrap.lights.values():
        if light.camera is not None:
            await check_camera(light.camera)
        light.last_motion_event  # noqa: B018
        check_device(light)

    for camera in bootstrap.cameras.values():
        await check_camera(camera)

    for viewer in bootstrap.viewers.values():
        assert viewer.liveview
        check_device(viewer)

    for sensor in bootstrap.sensors.values():
        check_device(sensor)

    for liveview in bootstrap.liveviews.values():
        liveview.owner  # noqa: B018
        assert (
            liveview.protect_url
            == f"https://127.0.0.1:0/protect/liveview/{liveview.id}"
        )

        for slot in liveview.slots:
            expected_ids = set(slot.camera_ids).intersection(
                set(bootstrap.cameras.keys()),
            )
            assert len(expected_ids) == len(slot.cameras)

    for user in bootstrap.users.values():
        user.groups  # noqa: B018

        if user.cloud_account is not None:
            assert user.cloud_account.user == user

    for event in bootstrap.events.values():
        event.smart_detect_events  # noqa: B018

        if event.type.value in EventType.motion_events() and event.camera is not None:
            await check_motion_event(event)


def test_base_url(protect_client: ProtectApiClient):
    arg = f"{protect_client.private_ws_path}?lastUpdateId={protect_client.bootstrap.last_update_id}"

    assert protect_client.base_url == "https://127.0.0.1:0"
    assert protect_client.ws_url == f"wss://127.0.0.1:0{arg}"

    protect_client._port = 443
    protect_client._update_url()

    assert protect_client.base_url == "https://127.0.0.1"
    assert protect_client.ws_url == f"wss://127.0.0.1{arg}"


def test_api_client_creation():
    """Test we can create the object."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "username",
        "password",
        debug=True,
        store_sessions=False,
    )
    assert client


def test_early_bootstrap():
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "username",
        "password",
        debug=True,
        store_sessions=False,
    )

    with pytest.raises(BadRequest):
        client.bootstrap  # noqa: B018


@pytest.mark.asyncio()
@pytest.mark.skipif(not TEST_CAMERA_EXISTS, reason="Missing testdata")
async def test_get_is_prerelease_returns_false(protect_client: ProtectApiClient):
    protect_client._bootstrap = None

    await protect_client.update()

    assert await protect_client.bootstrap.get_is_prerelease() is False
    assert await protect_client.bootstrap.nvr.get_is_prerelease() is False


@pytest.mark.asyncio()
@pytest.mark.skipif(not TEST_CAMERA_EXISTS, reason="Missing testdata")
async def test_bootstrap_get_device_from_mac(bootstrap):
    orig_bootstrap = deepcopy(bootstrap)
    mac = bootstrap["cameras"][0]["mac"]

    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "username",
        "password",
        debug=True,
        store_sessions=False,
    )
    client.api_request_obj = AsyncMock(side_effect=[bootstrap, orig_bootstrap])
    client.update_device = AsyncMock()

    bootstrap_obj = await client.get_bootstrap()
    camera = bootstrap_obj.get_device_from_mac(mac)

    assert camera is not None
    assert camera.mac == mac


@pytest.mark.asyncio()
@pytest.mark.skipif(not TEST_CAMERA_EXISTS, reason="Missing testdata")
async def test_bootstrap_get_device_from_mac_bad_mac(bootstrap):
    orig_bootstrap = deepcopy(bootstrap)

    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "username",
        "password",
        debug=True,
        store_sessions=False,
    )
    client.api_request_obj = AsyncMock(side_effect=[bootstrap, orig_bootstrap])
    client.update_device = AsyncMock()

    bootstrap_obj = await client.get_bootstrap()
    camera = bootstrap_obj.get_device_from_mac("not_a_mac")

    assert camera is None


def test_connection_host(protect_client: ProtectApiClient):
    protect_client.bootstrap.nvr.hosts = [
        IPv4Address("192.168.1.1"),
        IPv4Address("192.168.2.1"),
        IPv4Address("192.168.3.1"),
        "se-gw.local",
    ]

    # mismatch between client IP and IP that NVR returns
    protect_client._connection_host = None
    protect_client._host = "192.168.10.1"
    assert protect_client.connection_host == IPv4Address("192.168.1.1")

    # same IP from client and NVR (first match)
    protect_client._connection_host = None
    protect_client._host = "192.168.1.1"
    assert protect_client.connection_host == IPv4Address("192.168.1.1")

    # same IP from client and NVR (not first match)
    protect_client._connection_host = None
    protect_client._host = "192.168.3.1"
    assert protect_client.connection_host == IPv4Address("192.168.3.1")

    # same IP from client and NVR (not first match, DNS host)
    protect_client._connection_host = None
    protect_client._host = "se-gw.local"
    assert protect_client.connection_host == "se-gw.local"


def test_connection_host_override():
    protect = ProtectApiClient(
        "127.0.0.1",
        443,
        "test",
        "test",
        override_connection_host=True,
        store_sessions=False,
    )

    expected = IPv4Address("127.0.0.1")
    assert protect._connection_host == expected


@pytest.mark.asyncio()
async def test_force_update_with_old_Version(protect_client: ProtectApiClient):
    protect_client._bootstrap = None

    await protect_client.update()

    assert protect_client.bootstrap
    original_bootstrap = protect_client.bootstrap
    protect_client._bootstrap = None
    with patch(
        "uiprotect.api.ProtectApiClient.get_bootstrap",
        AsyncMock(return_value=AsyncMock(nvr=AsyncMock(version=OLD_VERSION))),
    ) as mock:
        await protect_client.update()
        assert mock.called

    assert protect_client.bootstrap
    assert original_bootstrap != protect_client.bootstrap


@pytest.mark.asyncio()
async def test_force_update_with_nfc_fingerprint_version(
    protect_client: ProtectApiClient,
):
    protect_client._bootstrap = None

    await protect_client.update()

    assert protect_client.bootstrap
    original_bootstrap = protect_client.bootstrap
    protect_client._bootstrap = None
    with (
        patch(
            "uiprotect.api.ProtectApiClient.get_bootstrap",
            return_value=AsyncMock(
                nvr=AsyncMock(version=NFC_FINGERPRINT_SUPPORT_VERSION)
            ),
        ) as get_bootstrap_mock,
        patch(
            "uiprotect.api.ProtectApiClient.api_request_list",
            side_effect=lambda endpoint: {
                "keyrings": [
                    {
                        "deviceType": "camera",
                        "deviceId": "new_device_id_1",
                        "registryType": "fingerprint",
                        "registryId": "new_registry_id_1",
                        "lastActivity": 1733432893331,
                        "metadata": {},
                        "ulpUser": "new_ulp_user_id_1",
                        "id": "new_keyring_id_1",
                        "modelKey": "keyring",
                    }
                ],
                "ulp-users": [
                    {
                        "ulpId": "new_ulp_id_1",
                        "firstName": "localadmin",
                        "lastName": "",
                        "fullName": "localadmin",
                        "avatar": "",
                        "status": "ACTIVE",
                        "id": "new_ulp_user_id_1",
                        "modelKey": "ulpUser",
                    }
                ],
            }.get(endpoint, []),
        ) as api_request_list_mock,
    ):
        await protect_client.update()
        assert get_bootstrap_mock.called
        assert api_request_list_mock.called
        api_request_list_mock.assert_any_call("keyrings")
        api_request_list_mock.assert_any_call("ulp-users")
        assert api_request_list_mock.call_count == 2

    assert protect_client.bootstrap
    assert original_bootstrap != protect_client.bootstrap
    assert len(protect_client.bootstrap.keyrings)
    assert len(protect_client.bootstrap.ulp_users)


@pytest.mark.asyncio()
async def test_force_update_no_user_keyring_access(protect_client: ProtectApiClient):
    protect_client._bootstrap = None

    await protect_client.update()

    assert protect_client.bootstrap
    original_bootstrap = protect_client.bootstrap
    protect_client._bootstrap = None
    with (
        patch(
            "uiprotect.api.ProtectApiClient.get_bootstrap",
            return_value=AsyncMock(
                nvr=AsyncMock(version=NFC_FINGERPRINT_SUPPORT_VERSION)
            ),
        ) as get_bootstrap_mock,
        patch(
            "uiprotect.api.ProtectApiClient.api_request_list",
            side_effect=NotAuthorized,
        ) as api_request_list_mock,
    ):
        await protect_client.update()
        assert get_bootstrap_mock.called
        assert api_request_list_mock.called
        api_request_list_mock.assert_any_call("keyrings")
        api_request_list_mock.assert_any_call("ulp-users")
        assert api_request_list_mock.call_count == 2

    assert protect_client.bootstrap
    assert original_bootstrap != protect_client.bootstrap
    assert not len(protect_client.bootstrap.keyrings)
    assert not len(protect_client.bootstrap.ulp_users)


@pytest.mark.asyncio()
async def test_force_update_user_keyring_internal_error(
    protect_client: ProtectApiClient,
):
    protect_client._bootstrap = None

    await protect_client.update()

    assert protect_client.bootstrap
    protect_client._bootstrap = None
    with (
        pytest.raises(BadRequest),
        patch(
            "uiprotect.api.ProtectApiClient.get_bootstrap",
            return_value=AsyncMock(
                nvr=AsyncMock(version=NFC_FINGERPRINT_SUPPORT_VERSION)
            ),
        ),
        patch(
            "uiprotect.api.ProtectApiClient.api_request_list",
            side_effect=BadRequest,
        ),
    ):
        await protect_client.update()


@pytest.mark.asyncio()
async def test_get_nvr(protect_client: ProtectApiClient, nvr):
    """Verifies the `get_nvr` method"""
    nvr_obj = await protect_client.get_nvr()
    nvr_dict = nvr_obj.unifi_dict()

    compare_objs(ModelType.NVR.value, nvr, nvr_dict)


@pytest.mark.asyncio()
async def test_bootstrap(protect_client: ProtectApiClient):
    """Verifies lookup of all object via ID"""
    await check_bootstrap(protect_client.bootstrap)


@pytest.mark.asyncio()
async def test_bootstrap_cached_property(protect_client: ProtectApiClient):
    """Test cached property works with bootstrap."""
    bootstrap = protect_client.bootstrap

    assert bootstrap.has_doorbell is True
    bootstrap.cameras = {}
    assert bootstrap.has_doorbell is True
    bootstrap._has_doorbell = None
    assert bootstrap.has_doorbell is False


@pytest.mark.asyncio()
async def test_bootstrap_construct(protect_client_no_debug: ProtectApiClient):
    """Verifies lookup of all object via ID"""
    await check_bootstrap(protect_client_no_debug.bootstrap)


@pytest.mark.asyncio()
@patch("uiprotect.utils.datetime", MockDatetime)
async def test_get_events_raw_default(protect_client: ProtectApiClient, now: datetime):
    events = await protect_client.get_events_raw(_allow_manual_paginate=False)

    end = now + timedelta(seconds=10)

    protect_client.api_request.assert_called_with(  # type: ignore[attr-defined]
        url="events",
        method="get",
        require_auth=True,
        raise_exception=True,
        public_api=False,
        params={
            "orderDirection": "ASC",
            "withoutDescriptions": "false",
            "start": to_js_time(end - timedelta(hours=1)),
            "end": to_js_time(end),
        },
    )
    assert len(events) == CONSTANTS["event_count"]
    for event in events:
        assert event["type"] in EventType.values()
        assert event["modelKey"] in ModelType.values()


@pytest.mark.asyncio()
async def test_get_events_raw_limit(protect_client: ProtectApiClient):
    await protect_client.get_events_raw(limit=10)

    protect_client.api_request.assert_called_with(  # type: ignore[attr-defined]
        url="events",
        method="get",
        require_auth=True,
        raise_exception=True,
        public_api=False,
        params={"orderDirection": "ASC", "withoutDescriptions": "false", "limit": 10},
    )


@pytest.mark.asyncio()
async def test_get_events_raw_types(protect_client: ProtectApiClient):
    await protect_client.get_events_raw(
        limit=10,
        types=[EventType.MOTION, EventType.SMART_DETECT],
    )

    protect_client.api_request.assert_called_with(  # type: ignore[attr-defined]
        url="events",
        method="get",
        require_auth=True,
        raise_exception=True,
        public_api=False,
        params={
            "orderDirection": "ASC",
            "withoutDescriptions": "false",
            "limit": 10,
            "types": ["motion", "smartDetectZone"],
        },
    )


# test has a scaling "expected time to complete" based on the number of
# events in the last 24 hours
@pytest.mark.timeout(CONSTANTS["event_count"] * 0.1)  # type: ignore[misc]
@pytest.mark.asyncio()
async def test_get_events(protect_client: ProtectApiClient, raw_events):
    expected_events = [
        event
        for event in raw_events
        if event["score"] >= 50 and event["type"] in EventType.device_events()
    ]

    protect_client._minimum_score = 50

    events = await protect_client.get_events(_allow_manual_paginate=False)

    assert len(events) == len(expected_events)
    for index, event in enumerate(events):
        compare_objs(event.model.value, expected_events[index], event.unifi_dict())

        if event.type.value in EventType.motion_events():
            await check_motion_event(event)


@pytest.mark.asyncio()
async def test_get_events_not_event(protect_client: ProtectApiClient, camera):
    protect_client.get_events_raw = AsyncMock(return_value=[camera])  # type: ignore[method-assign]

    assert await protect_client.get_events() == []


@pytest.mark.asyncio()
async def test_get_events_not_event_with_type(protect_client: ProtectApiClient, camera):
    camera["type"] = EventType.MOTION.value

    protect_client.get_events_raw = AsyncMock(return_value=[camera])  # type: ignore[method-assign]

    assert await protect_client.get_events() == []


@pytest.mark.skipif(not TEST_CAMERA_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_device_mismatch(protect_client: ProtectApiClient, camera):
    protect_client.api_request_obj = AsyncMock(return_value=camera)  # type: ignore[method-assign]

    with pytest.raises(NvrError):
        await protect_client.get_bridge("test_id")


@pytest.mark.skipif(not TEST_CAMERA_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_device_not_adopted(protect_client: ProtectApiClient, camera):
    camera["isAdopted"] = False
    protect_client.api_request_obj = AsyncMock(return_value=camera)  # type: ignore[method-assign]

    with pytest.raises(NvrError):
        await protect_client.get_camera("test_id")


@pytest.mark.skipif(not TEST_CAMERA_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_device_not_adopted_enabled(protect_client: ProtectApiClient, camera):
    camera["isAdopted"] = False
    protect_client.ignore_unadopted = False
    protect_client.api_request_obj = AsyncMock(return_value=camera)  # type: ignore[method-assign]

    obj = create_from_unifi_dict(camera)
    assert_equal_dump(obj, await protect_client.get_camera("test_id"))


@pytest.mark.skipif(not TEST_CAMERA_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_camera(protect_client: ProtectApiClient, camera):
    obj = create_from_unifi_dict(camera)

    assert_equal_dump(obj, await protect_client.get_camera("test_id"))


@pytest.mark.skipif(not TEST_LIGHT_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_light(protect_client: ProtectApiClient, light):
    obj = create_from_unifi_dict(light)

    assert_equal_dump(obj, await protect_client.get_light("test_id"))


@pytest.mark.skipif(not TEST_SENSOR_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_sensor(protect_client: ProtectApiClient, sensor):
    obj = create_from_unifi_dict(sensor)

    assert_equal_dump(obj, await protect_client.get_sensor("test_id"))


@pytest.mark.skipif(not TEST_VIEWPORT_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_viewer(protect_client: ProtectApiClient, viewport):
    obj = create_from_unifi_dict(viewport)

    assert_equal_dump(obj, await protect_client.get_viewer("test_id"))


@pytest.mark.skipif(not TEST_BRIDGE_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_bridge(protect_client: ProtectApiClient, bridge):
    obj = create_from_unifi_dict(bridge)

    assert_equal_dump(obj, await protect_client.get_bridge("test_id"))


@pytest.mark.skipif(not TEST_LIVEVIEW_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_liveview(protect_client: ProtectApiClient, liveview):
    obj = create_from_unifi_dict(liveview)

    assert_equal_dump(obj, (await protect_client.get_liveview("test_id")))


@pytest.mark.skipif(not TEST_CAMERA_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_devices_mismatch(protect_client: ProtectApiClient, cameras):
    protect_client.api_request_list = AsyncMock(return_value=cameras)  # type: ignore[method-assign]

    with pytest.raises(NvrError):
        await protect_client.get_bridges()


@pytest.mark.skipif(not TEST_CAMERA_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_devices_not_adopted(protect_client: ProtectApiClient, cameras):
    cameras[0]["isAdopted"] = False
    protect_client.api_request_list = AsyncMock(return_value=cameras)  # type: ignore[method-assign]

    assert len(await protect_client.get_cameras()) == len(cameras) - 1


@pytest.mark.skipif(not TEST_CAMERA_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_devices_not_adopted_enabled(
    protect_client: ProtectApiClient,
    cameras,
):
    cameras[0]["isAdopted"] = False
    protect_client.ignore_unadopted = False
    protect_client.api_request_list = AsyncMock(return_value=cameras)  # type: ignore[method-assign]

    assert len(await protect_client.get_cameras()) == len(cameras)


@pytest.mark.skipif(not TEST_CAMERA_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_cameras(protect_client: ProtectApiClient, cameras):
    objs = [create_from_unifi_dict(d) for d in cameras]

    assert_equal_dump(objs, await protect_client.get_cameras())


@pytest.mark.skipif(not TEST_LIGHT_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_lights(protect_client: ProtectApiClient, lights):
    objs = [create_from_unifi_dict(d) for d in lights]

    assert_equal_dump(objs, await protect_client.get_lights())


@pytest.mark.skipif(not TEST_SENSOR_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_sensors(protect_client: ProtectApiClient, sensors):
    objs = [create_from_unifi_dict(d) for d in sensors]

    assert_equal_dump(objs, await protect_client.get_sensors())


@pytest.mark.skipif(not TEST_VIEWPORT_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_viewers(protect_client: ProtectApiClient, viewports):
    objs = [create_from_unifi_dict(d) for d in viewports]

    assert_equal_dump(objs, await protect_client.get_viewers())


@pytest.mark.skipif(not TEST_BRIDGE_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_bridges(protect_client: ProtectApiClient, bridges):
    objs = [create_from_unifi_dict(d) for d in bridges]

    assert_equal_dump(objs, await protect_client.get_bridges())


@pytest.mark.skipif(not TEST_LIVEVIEW_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_liveviews(protect_client: ProtectApiClient, liveviews):
    objs = [create_from_unifi_dict(d) for d in liveviews]

    assert_equal_dump(objs, await protect_client.get_liveviews())


@pytest.mark.skipif(not TEST_SNAPSHOT_EXISTS, reason="Missing testdata")
@patch("uiprotect.utils.datetime", MockDatetime)
@patch("uiprotect.api.time.time", get_time)
@pytest.mark.asyncio()
async def test_get_camera_snapshot(protect_client: ProtectApiClient, now):
    data = await protect_client.get_camera_snapshot("test_id")
    assert data is not None

    protect_client.api_request_raw.assert_called_with(  # type: ignore[attr-defined]
        "cameras/test_id/snapshot",
        params={
            "ts": to_js_time(now),
            "force": "true",
        },
        raise_exception=False,
    )

    img = Image.open(BytesIO(data))
    assert img.format in {"PNG", "JPEG"}


@pytest.mark.skipif(not TEST_PUBLIC_API_SNAPSHOT_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_public_api_camera_snapshot(protect_client: ProtectApiClient, now):
    data = await protect_client.get_public_api_camera_snapshot("test_id")
    assert data is not None

    protect_client.api_request_raw.assert_called_with(  # type: ignore[attr-defined]
        public_api=True,
        raise_exception=False,
        url="/v1/cameras/test_id/snapshot",
        params={
            "highQuality": "false",
        },
    )

    img = Image.open(BytesIO(data))
    assert img.format in {"PNG", "JPEG"}


@pytest.mark.skipif(not TEST_PUBLIC_API_SNAPSHOT_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_public_api_camera_snapshot_hq_true(
    protect_client: ProtectApiClient, now
):
    data = await protect_client.get_public_api_camera_snapshot(
        "test_id", high_quality=True
    )
    assert data is not None

    protect_client.api_request_raw.assert_called_with(  # type: ignore[attr-defined]
        public_api=True,
        raise_exception=False,
        url="/v1/cameras/test_id/snapshot",
        params={
            "highQuality": "true",
        },
    )

    img = Image.open(BytesIO(data))
    assert img.format in {"PNG", "JPEG"}


@pytest.mark.skipif(not TEST_SNAPSHOT_EXISTS, reason="Missing testdata")
@patch("uiprotect.utils.datetime", MockDatetime)
@patch("uiprotect.api.time.time", get_time)
@pytest.mark.asyncio()
async def test_get_pacakge_camera_snapshot(protect_client: ProtectApiClient, now):
    data = await protect_client.get_package_camera_snapshot("test_id")
    assert data is not None

    protect_client.api_request_raw.assert_called_with(  # type: ignore[attr-defined]
        "cameras/test_id/package-snapshot",
        params={
            "ts": to_js_time(now),
            "force": "true",
        },
        raise_exception=False,
    )

    img = Image.open(BytesIO(data))
    assert img.format in {"PNG", "JPEG"}


@pytest.mark.skipif(not TEST_SNAPSHOT_EXISTS, reason="Missing testdata")
@patch("uiprotect.utils.datetime", MockDatetime)
@patch("uiprotect.api.time.time", get_time)
@pytest.mark.asyncio()
async def test_get_camera_snapshot_args(protect_client: ProtectApiClient, now):
    data = await protect_client.get_camera_snapshot("test_id", 1920, 1080)
    assert data is not None

    protect_client.api_request_raw.assert_called_with(  # type: ignore[attr-defined]
        "cameras/test_id/snapshot",
        params={
            "ts": to_js_time(now),
            "force": "true",
            "w": 1920,
            "h": 1080,
        },
        raise_exception=False,
    )

    img = Image.open(BytesIO(data))
    assert img.format in {"PNG", "JPEG"}


@pytest.mark.skipif(not TEST_SNAPSHOT_EXISTS, reason="Missing testdata")
@patch("uiprotect.utils.datetime", MockDatetime)
@patch("uiprotect.api.time.time", get_time)
@pytest.mark.asyncio()
async def test_get_package_camera_snapshot_args(protect_client: ProtectApiClient, now):
    data = await protect_client.get_package_camera_snapshot("test_id", 1920, 1080)
    assert data is not None

    protect_client.api_request_raw.assert_called_with(  # type: ignore[attr-defined]
        "cameras/test_id/package-snapshot",
        params={
            "ts": to_js_time(now),
            "force": "true",
            "w": 1920,
            "h": 1080,
        },
        raise_exception=False,
    )

    img = Image.open(BytesIO(data))
    assert img.format in {"PNG", "JPEG"}


@pytest.mark.skipif(not TEST_VIDEO_EXISTS, reason="Missing testdata")
@patch("uiprotect.api.datetime", MockDatetime)
@patch("uiprotect.api.time.time", get_time)
@pytest.mark.asyncio()
async def test_get_camera_video(protect_client: ProtectApiClient, now, tmp_binary_file):
    camera = next(iter(protect_client.bootstrap.cameras.values()))
    start = now - timedelta(seconds=CONSTANTS["camera_video_length"])

    data = await protect_client.get_camera_video(camera.id, start, now)
    assert data is not None

    protect_client.api_request_raw.assert_called_with(  # type: ignore[attr-defined]
        "video/export",
        params={
            "camera": camera.id,
            "channel": 0,
            "start": to_js_time(start),
            "end": to_js_time(now),
        },
        raise_exception=False,
    )

    tmp_binary_file.write(data)
    tmp_binary_file.close()

    validate_video_file(tmp_binary_file.name, CONSTANTS["camera_video_length"])


@pytest.mark.asyncio()
async def test_get_camera_video_http_error(
    protect_client: ProtectApiClient, now: datetime
) -> None:
    """Test get_camera_video with HTTP error response."""
    camera = next(iter(protect_client.bootstrap.cameras.values()))
    start = now - timedelta(seconds=CONSTANTS["camera_video_length"])

    # Test the simple path (no output_file) which uses api_request_raw
    protect_client.api_request_raw = AsyncMock(
        side_effect=NotAuthorized("Access denied")
    )

    with pytest.raises(NotAuthorized):
        await protect_client.get_camera_video(camera.id, start, now)


@pytest.mark.asyncio()
async def test_get_camera_video_logging(
    protect_client: ProtectApiClient, now: datetime, caplog: pytest.LogCaptureFixture
) -> None:
    """Test get_camera_video debug logging."""
    caplog.set_level(logging.DEBUG, logger="uiprotect.api")

    camera = next(iter(protect_client.bootstrap.cameras.values()))
    start = now - timedelta(seconds=CONSTANTS["camera_video_length"])

    # Create a simple async context manager class for the file mock
    class MockAsyncFile:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def write(self, data):
            pass

    # Mock the response
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.close = AsyncMock()

    # Create a mock content with iter_chunked method
    class MockContent:
        def iter_chunked(self, chunk_size):
            return MockAsyncIterator()

    class MockAsyncIterator:
        def __init__(self):
            self.items = [b"test_chunk"]
            self.index = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.index >= len(self.items):
                raise StopAsyncIteration
            item = self.items[self.index]
            self.index += 1
            return item

    mock_response.content = MockContent()

    # Mock aiofiles.open to return our mock file
    with patch("aiofiles.open", return_value=MockAsyncFile()):
        protect_client.request = AsyncMock(return_value=mock_response)

        await protect_client.get_camera_video(
            camera.id,
            start,
            now,
            output_file="/tmp/test.mp4",  # noqa: S108
        )

    # Check that debug log was written
    assert any(
        "Requesting camera video:" in record.message for record in caplog.records
    )


@pytest.mark.skipif(not TEST_THUMBNAIL_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_event_thumbnail(protect_client: ProtectApiClient):
    data = await protect_client.get_event_thumbnail("e-test_id")
    assert data is not None

    protect_client.api_request_raw.assert_called_with(  # type: ignore[attr-defined]
        "events/test_id/thumbnail",
        params={},
        raise_exception=False,
    )

    img = Image.open(BytesIO(data))
    assert img.format in {"PNG", "JPEG"}


@pytest.mark.skipif(not TEST_THUMBNAIL_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_event_thumbnail_args(protect_client: ProtectApiClient):
    data = await protect_client.get_event_thumbnail("test_id", 1920, 1080)
    assert data is not None

    protect_client.api_request_raw.assert_called_with(  # type: ignore[attr-defined]
        "events/test_id/thumbnail",
        params={
            "w": 1920,
            "h": 1080,
        },
        raise_exception=False,
    )

    img = Image.open(BytesIO(data))
    assert img.format in {"PNG", "JPEG"}


@pytest.mark.skipif(not TEST_HEATMAP_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_event_heatmap(protect_client: ProtectApiClient):
    data = await protect_client.get_event_heatmap("e-test_id")
    assert data is not None

    protect_client.api_request_raw.assert_called_with(  # type: ignore[attr-defined]
        "events/test_id/heatmap",
        raise_exception=False,
    )

    img = Image.open(BytesIO(data))
    assert img.format in {"PNG", "JPEG"}


@pytest.mark.skipif(not TEST_SMART_TRACK_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_event_smart_detect_track(protect_client: ProtectApiClient):
    data = await protect_client.get_event_smart_detect_track("test_id")
    assert data.camera

    protect_client.api_request.assert_called_with(  # type: ignore[attr-defined]
        url="events/test_id/smartDetectTrack",
        method="get",
        require_auth=True,
        raise_exception=True,
        public_api=False,
    )


@pytest.mark.skipif(not TEST_CAMERA_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_aiport(protect_client: ProtectApiClient, aiport):
    obj = create_from_unifi_dict(aiport)

    assert_equal_dump(obj, await protect_client.get_aiport("test_id"))


@pytest.mark.skipif(not TEST_CAMERA_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_aiport_not_adopted(protect_client: ProtectApiClient, aiport):
    aiport["isAdopted"] = False
    protect_client.api_request_obj = AsyncMock(return_value=aiport)

    with pytest.raises(NvrError):
        await protect_client.get_aiport("test_id")


@pytest.mark.skipif(not TEST_CAMERA_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_aiport_not_adopted_enabled(protect_client: ProtectApiClient, aiport):
    aiport["isAdopted"] = False
    protect_client.ignore_unadopted = False
    protect_client.api_request_obj = AsyncMock(return_value=aiport)

    obj = create_from_unifi_dict(aiport)
    assert_equal_dump(obj, await protect_client.get_aiport("test_id"))


@pytest.mark.skipif(not TEST_CAMERA_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_chime(protect_client: ProtectApiClient, chime):
    obj = create_from_unifi_dict(chime)

    assert_equal_dump(obj, await protect_client.get_chime("test_id"))


@pytest.mark.skipif(not TEST_CAMERA_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_chime_not_adopted(protect_client: ProtectApiClient, chime):
    chime["isAdopted"] = False
    protect_client.api_request_obj = AsyncMock(return_value=chime)

    with pytest.raises(NvrError):
        await protect_client.get_chime("test_id")


@pytest.mark.skipif(not TEST_CAMERA_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_chime_not_adopted_enabled(protect_client: ProtectApiClient, chime):
    chime["isAdopted"] = False
    protect_client.ignore_unadopted = False
    protect_client.api_request_obj = AsyncMock(return_value=chime)

    obj = create_from_unifi_dict(chime)
    assert_equal_dump(obj, await protect_client.get_chime("test_id"))


@pytest.mark.skipif(not TEST_CAMERA_EXISTS, reason="Missing testdata")
@pytest.mark.asyncio()
async def test_get_aiports(protect_client: ProtectApiClient, aiports):
    objs = [create_from_unifi_dict(d) for d in aiports]

    assert_equal_dump(objs, await protect_client.get_aiports())


@pytest.mark.asyncio()
async def test_play_speaker(protect_client: ProtectApiClient):
    """Test play_speaker with default parameters."""
    device_id = "cf1a330397c08f919d02bd7c"
    protect_client.api_request = AsyncMock()

    await protect_client.play_speaker(device_id)

    protect_client.api_request.assert_called_with(
        f"chimes/{device_id}/play-speaker",
        method="post",
        json=None,
    )


@pytest.mark.asyncio()
async def test_play_speaker_with_volume(protect_client: ProtectApiClient):
    """Test play_speaker with volume parameter."""
    device_id = "cf1a330397c08f919d02bd7c"
    volume = 5
    chime = protect_client.bootstrap.chimes[device_id]
    protect_client.api_request = AsyncMock()

    await protect_client.play_speaker(device_id, volume=volume)

    protect_client.api_request.assert_called_with(
        f"chimes/{device_id}/play-speaker",
        method="post",
        json={
            "volume": volume,
            "repeatTimes": chime.repeat_times,
            "trackNo": chime.track_no,
        },
    )


@pytest.mark.asyncio()
async def test_play_speaker_with_ringtone_id(protect_client: ProtectApiClient):
    """Test play_speaker with ringtone_id parameter."""
    device_id = "cf1a330397c08f919d02bd7c"
    ringtone_id = "ringtone_1"
    chime = protect_client.bootstrap.chimes[device_id]
    protect_client.api_request = AsyncMock()

    await protect_client.play_speaker(device_id, ringtone_id=ringtone_id)

    protect_client.api_request.assert_called_with(
        f"chimes/{device_id}/play-speaker",
        method="post",
        json={
            "volume": chime.volume,
            "repeatTimes": chime.repeat_times,
            "ringtoneId": ringtone_id,
        },
    )


@pytest.mark.asyncio()
async def test_play_speaker_invalid_chime_id(protect_client: ProtectApiClient):
    """Test play_speaker with invalid chime ID."""
    device_id = "invalid_id"
    protect_client.api_request = AsyncMock()

    with pytest.raises(BadRequest):
        await protect_client.play_speaker(device_id, volume=5)


@pytest.mark.asyncio()
async def test_play_speaker_with_all_parameters(protect_client: ProtectApiClient):
    """Test play_speaker with all parameters."""
    device_id = "cf1a330397c08f919d02bd7c"
    volume = 5
    repeat_times = 3
    ringtone_id = "ringtone_1"
    track_no = 2
    protect_client.api_request = AsyncMock()

    await protect_client.play_speaker(
        device_id,
        volume=volume,
        repeat_times=repeat_times,
        ringtone_id=ringtone_id,
        track_no=track_no,
    )

    protect_client.api_request.assert_called_with(
        f"chimes/{device_id}/play-speaker",
        method="post",
        json={
            "volume": volume,
            "repeatTimes": repeat_times,
            "ringtoneId": ringtone_id,
        },
    )


@pytest.mark.asyncio()
async def test_set_light_is_led_force_on(protect_client: ProtectApiClient):
    """Test set_light_is_led_force_on with valid parameters."""
    device_id = "test_light_id"
    is_led_force_on = True
    protect_client.api_request = AsyncMock()

    await protect_client.set_light_is_led_force_on(device_id, is_led_force_on)

    protect_client.api_request.assert_called_with(
        f"lights/{device_id}",
        method="patch",
        json={"lightOnSettings": {"isLedForceOn": is_led_force_on}},
    )


@pytest.mark.asyncio()
async def test_set_light_is_led_force_on_false(protect_client: ProtectApiClient):
    """Test set_light_is_led_force_on with is_led_force_on set to False."""
    device_id = "test_light_id"
    is_led_force_on = False
    protect_client.api_request = AsyncMock()

    await protect_client.set_light_is_led_force_on(device_id, is_led_force_on)

    protect_client.api_request.assert_called_with(
        f"lights/{device_id}",
        method="patch",
        json={"lightOnSettings": {"isLedForceOn": is_led_force_on}},
    )


@pytest.mark.asyncio()
async def test_set_light_is_led_force_on_invalid_device_id(
    protect_client: ProtectApiClient,
):
    """Test set_light_is_led_force_on with invalid device ID."""
    device_id = "invalid_id"
    is_led_force_on = True
    protect_client.api_request = AsyncMock(side_effect=BadRequest)

    with pytest.raises(BadRequest):
        await protect_client.set_light_is_led_force_on(device_id, is_led_force_on)


@pytest.mark.asyncio()
async def test_create_api_key_success(protect_client: ProtectApiClient):
    protect_client.api_request = AsyncMock(
        return_value={"data": {"full_api_key": "test_api_key"}}
    )
    result = await protect_client.create_api_key("test")
    assert result == "test_api_key"
    protect_client.api_request.assert_called_with(
        api_path="/proxy/users/api/v2",
        url="/user/self/keys",
        method="post",
        json={"name": "test"},
    )


@pytest.mark.asyncio()
async def test_create_api_key_empty_name(protect_client: ProtectApiClient):
    protect_client._last_token_cookie_decode = {"userId": "test_user_id"}
    with pytest.raises(BadRequest, match="API key name cannot be empty"):
        await protect_client.create_api_key("")


@pytest.mark.asyncio()
async def test_create_api_key_failure(protect_client: ProtectApiClient):
    protect_client.api_request = AsyncMock(return_value={})
    protect_client._last_token_cookie_decode = {"userId": "test_user_id"}
    with pytest.raises(BadRequest, match="Failed to create API key"):
        await protect_client.create_api_key("test")


@pytest.mark.asyncio()
async def test_api_request_raw_with_custom_api_path() -> None:
    """Test api_request_raw uses custom api_path when provided"""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "test",
        "test",
        verify_ssl=False,
    )

    # Mock the request method to verify the path
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.read = AsyncMock(return_value=b"test_response")
    mock_response.close = AsyncMock()

    client.request = AsyncMock(return_value=mock_response)

    result = await client.api_request_raw("/test/endpoint", api_path="/custom/api/path")

    assert result == b"test_response"
    client.request.assert_called_with(
        "get",
        "/custom/api/path/test/endpoint",
        require_auth=True,
        auto_close=False,
        public_api=False,
    )


@pytest.mark.asyncio()
async def test_api_request_raw_with_default_api_path() -> None:
    """Test api_request_raw uses default api_path when not provided"""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "test",
        "test",
        verify_ssl=False,
    )

    # Mock the request method to verify the path
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.read = AsyncMock(return_value=b"test_response")
    mock_response.close = AsyncMock()

    client.request = AsyncMock(return_value=mock_response)

    result = await client.api_request_raw("/test/endpoint")

    assert result == b"test_response"
    client.request.assert_called_with(
        "get",
        f"{client.private_api_path}/test/endpoint",
        require_auth=True,
        auto_close=False,
        public_api=False,
    )


@pytest.mark.asyncio
async def test_read_auth_config_file_not_found():
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "test",
        "test",
        verify_ssl=False,
    )
    with patch("aiofiles.open", side_effect=FileNotFoundError):
        result = await client._read_auth_config()
        assert result is None


class AsyncMockOpen:
    def __init__(self, read_data: bytes):
        self._read_data = read_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def read(self):
        return self._read_data


@pytest.mark.asyncio
async def test_read_auth_config_empty_file():
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "test",
        "test",
        verify_ssl=False,
    )
    with patch("aiofiles.open", return_value=AsyncMockOpen(b"")):
        result = await client._read_auth_config()
        assert result is None


@pytest.mark.asyncio
async def test_read_auth_config_invalid_json():
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "test",
        "test",
        verify_ssl=False,
    )
    with patch("aiofiles.open", return_value=AsyncMockOpen(b"{invalid json")):
        result = await client._read_auth_config()
        assert result is None


@pytest.mark.asyncio
async def test_read_auth_config_no_session():
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "test",
        "test",
        verify_ssl=False,
    )
    with patch("aiofiles.open", return_value=AsyncMockOpen(b"{}")):
        result = await client._read_auth_config()
        assert result is None


def test_api_key_init():
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="my_key",
        verify_ssl=False,
    )
    assert hasattr(client, "_api_key")
    assert client._api_key == "my_key"


@pytest.mark.asyncio()
async def test_get_meta_info_calls_public_api():
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="my_key",
        verify_ssl=False,
    )
    client.api_request = AsyncMock(return_value={"applicationVersion": "1.0.0"})
    result = await client.get_meta_info()
    assert result.applicationVersion == "1.0.0"
    client.api_request.assert_called_with(url="/v1/meta/info", public_api=True)


@pytest.mark.asyncio()
async def test_api_request_raw_public_api_sets_path_and_header():
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="my_key",
        verify_ssl=False,
    )
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.read = AsyncMock(return_value=b"public_response")
    mock_response.close = AsyncMock()
    client.request = AsyncMock(return_value=mock_response)

    result = await client.api_request_raw(
        "/v1/meta/info",
        public_api=True,
    )
    assert result == b"public_response"
    client.request.assert_called_with(
        "get",
        f"{client.public_api_path}/v1/meta/info",
        require_auth=True,
        auto_close=False,
        public_api=True,
    )


@pytest.mark.asyncio()
async def test_api_request_raw_public_api_requires_api_key():
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key=None,
        verify_ssl=False,
    )
    # Patch get_session to avoid aiohttp session creation
    client.get_session = AsyncMock()
    with pytest.raises(
        NotAuthorized, match="API key is required for public API requests"
    ):
        await client.api_request_raw(
            "/v1/meta/info",
            public_api=True,
        )


@pytest.mark.asyncio()
async def test_get_meta_info_invalid_response_type():
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="my_key",
        verify_ssl=False,
    )
    # Mock api_request to return a non-dict value
    client.api_request = AsyncMock(return_value=None)
    with pytest.raises(NvrError, match="Failed to retrieve meta info from public API"):
        await client.get_meta_info()


@pytest.mark.asyncio()
async def test_public_api_sets_x_api_key_header() -> None:
    """Test that X-API-KEY header is set when using public API."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="test_api_key_123",
        verify_ssl=False,
        store_sessions=False,  # Disable session storage to avoid auth cookie updates
    )

    # Create a mock session and response
    mock_session = AsyncMock()
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.content_type = "application/json"
    mock_response.read = AsyncMock(return_value=b'{"test": "data"}')
    mock_response.release = AsyncMock()
    mock_response.close = AsyncMock()
    mock_response.url = "https://127.0.0.1:0/proxy/protect/integration/v1/test"
    mock_response.cookies = {}  # Empty cookies to avoid cookie processing

    # Track headers passed to session.request
    actual_headers: dict[str, str] | None = None

    # Create async context manager for the request
    class MockRequestContext:
        async def __aenter__(self) -> AsyncMock:
            return mock_response

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: Any,
        ) -> None:
            return None

    def mock_session_request(
        method: str, url: str, headers: dict[str, str] | None = None, **kwargs: Any
    ) -> MockRequestContext:
        nonlocal actual_headers
        actual_headers = headers
        return MockRequestContext()

    mock_session.request = mock_session_request
    client.get_public_api_session = AsyncMock(return_value=mock_session)

    # Make a public API request
    await client.api_request_raw("/v1/test", public_api=True)

    # Verify the X-API-KEY header was set
    assert actual_headers == {"X-API-KEY": "test_api_key_123"}


@pytest.mark.asyncio
async def test_get_public_api_session_creates_and_reuses_session():
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        verify_ssl=False,
    )
    session1 = await client.get_public_api_session()
    assert isinstance(session1, aiohttp.ClientSession)
    session2 = await client.get_public_api_session()
    assert session1 is session2
    await session1.close()
    session3 = await client.get_public_api_session()
    assert session3 is not session1
    assert isinstance(session3, aiohttp.ClientSession)
    await session3.close()


@pytest.mark.asyncio
async def test_public_api_session_constructor_assignment():
    import aiohttp

    async with aiohttp.ClientSession() as session:
        client = ProtectApiClient(
            "127.0.0.1",
            0,
            "user",
            "pass",
            public_api_session=session,
            verify_ssl=False,
        )
        assert client._public_api_session is session


@pytest.mark.asyncio()
async def test_request_uses_get_session_for_private_api():
    """Test that request uses get_session (not get_public_api_session) for private API calls."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        verify_ssl=False,
    )
    # Patch get_session to return a mock session
    mock_session = AsyncMock()
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.content_type = "application/json"
    mock_response.read = AsyncMock(return_value=b"{}")
    mock_response.release = AsyncMock()
    mock_response.close = AsyncMock()
    mock_response.headers = {}
    mock_response.cookies = {}

    # __aenter__ returns the response
    class MockRequestContext:
        async def __aenter__(self):
            return mock_response

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_session.request = lambda *args, **kwargs: MockRequestContext()
    client.get_session = AsyncMock(return_value=mock_session)
    client.ensure_authenticated = AsyncMock()
    # Should use get_session, not get_public_api_session
    client.get_public_api_session = AsyncMock()
    # Call request with public_api=False
    result = await client.request("get", "/test/endpoint", public_api=False)
    assert result is mock_response
    client.get_session.assert_awaited()
    client.get_public_api_session.assert_not_called()


@pytest.mark.asyncio
async def test_close_public_api_session():
    async with aiohttp.ClientSession() as session:
        client = ProtectApiClient(
            "127.0.0.1",
            0,
            "user",
            "pass",
            public_api_session=session,
            verify_ssl=False,
        )
        # Should be the same session
        assert client._public_api_session is session
        # Close the session
        await client.close_public_api_session()
        # Should be None after closing
        assert client._public_api_session is None
        # Should be idempotent (no error if called again)
        await client.close_public_api_session()


def test_set_api_key_sets_value():
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key=None,
        verify_ssl=False,
    )
    client.set_api_key("test_key")
    assert client._api_key == "test_key"


def test_set_api_key_empty_raises():
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key=None,
        verify_ssl=False,
    )
    import pytest

    with pytest.raises(BadRequest, match="API key cannot be empty"):
        client.set_api_key("")


def test_is_api_key_set_true():
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="my_key",
        verify_ssl=False,
    )
    assert client.is_api_key_set() is True


def test_is_api_key_set_false():
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key=None,
        verify_ssl=False,
    )
    assert client.is_api_key_set() is False


@pytest.mark.asyncio
async def test_create_camera_rtsps_streams_with_list_qualities():
    """Test creating RTSPS streams with a list of qualities."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="test_key",
        verify_ssl=False,
    )

    mock_response = (
        b'{"high": "rtsps://example.com/high", "medium": "rtsps://example.com/medium"}'
    )

    with patch.object(
        client, "api_request_raw", new=AsyncMock(return_value=mock_response)
    ) as mock_request:
        result = await client.create_camera_rtsps_streams(
            "camera123", ["high", "medium"]
        )

        mock_request.assert_called_once_with(
            public_api=True,
            url="/v1/cameras/camera123/rtsps-stream",
            method="POST",
            json={"qualities": ["high", "medium"]},
        )

        assert result is not None
        assert result.get_stream_url("high") == "rtsps://example.com/high"
        assert result.get_stream_url("medium") == "rtsps://example.com/medium"


@pytest.mark.asyncio
async def test_create_camera_rtsps_streams_with_string_quality():
    """Test creating RTSPS streams with a single quality string."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="test_key",
        verify_ssl=False,
    )

    mock_response = b'{"high": "rtsps://example.com/high"}'

    with patch.object(
        client, "api_request_raw", new=AsyncMock(return_value=mock_response)
    ) as mock_request:
        result = await client.create_camera_rtsps_streams("camera123", "high")

        mock_request.assert_called_once_with(
            public_api=True,
            url="/v1/cameras/camera123/rtsps-stream",
            method="POST",
            json={"qualities": ["high"]},
        )

        assert result is not None
        assert result.get_stream_url("high") == "rtsps://example.com/high"


@pytest.mark.asyncio
async def test_create_camera_rtsps_streams_none_response():
    """Test creating RTSPS streams with None response."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="test_key",
        verify_ssl=False,
    )

    with patch.object(
        client, "api_request_raw", new=AsyncMock(return_value=None)
    ) as mock_request:
        result = await client.create_camera_rtsps_streams("camera123", ["high"])

        mock_request.assert_called_once_with(
            public_api=True,
            url="/v1/cameras/camera123/rtsps-stream",
            method="POST",
            json={"qualities": ["high"]},
        )

        assert result is None


@pytest.mark.asyncio
async def test_create_camera_rtsps_streams_invalid_json():
    """Test creating RTSPS streams with invalid JSON response."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="test_key",
        verify_ssl=False,
    )

    with patch.object(
        client, "api_request_raw", new=AsyncMock(return_value=b"invalid json")
    ) as mock_request:
        result = await client.create_camera_rtsps_streams("camera123", ["high"])

        mock_request.assert_called_once()
        assert result is None


@pytest.mark.asyncio
async def test_get_camera_rtsps_streams():
    """Test getting existing RTSPS streams."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="test_key",
        verify_ssl=False,
    )

    mock_response = (
        b'{"high": "rtsps://example.com/high", "medium": "rtsps://example.com/medium"}'
    )

    with patch.object(
        client, "api_request_raw", new=AsyncMock(return_value=mock_response)
    ) as mock_request:
        result = await client.get_camera_rtsps_streams("camera123")

        mock_request.assert_called_once_with(
            public_api=True,
            url="/v1/cameras/camera123/rtsps-stream",
            method="GET",
        )

        assert result is not None
        assert result.get_stream_url("high") == "rtsps://example.com/high"
        assert result.get_stream_url("medium") == "rtsps://example.com/medium"
        assert set(result.get_available_stream_qualities()) == {"high", "medium"}


@pytest.mark.asyncio
async def test_get_camera_rtsps_streams_none_response():
    """Test getting RTSPS streams with None response."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="test_key",
        verify_ssl=False,
    )

    with patch.object(
        client, "api_request_raw", new=AsyncMock(return_value=None)
    ) as mock_request:
        result = await client.get_camera_rtsps_streams("camera123")

        mock_request.assert_called_once_with(
            public_api=True,
            url="/v1/cameras/camera123/rtsps-stream",
            method="GET",
        )

        assert result is None


@pytest.mark.asyncio
async def test_get_camera_rtsps_streams_invalid_json():
    """Test getting RTSPS streams with invalid JSON response."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="test_key",
        verify_ssl=False,
    )

    with patch.object(
        client, "api_request_raw", new=AsyncMock(return_value=b"invalid json")
    ) as mock_request:
        result = await client.get_camera_rtsps_streams("camera123")

        mock_request.assert_called_once_with(
            public_api=True,
            url="/v1/cameras/camera123/rtsps-stream",
            method="GET",
        )

        assert result is None


@pytest.mark.asyncio
async def test_delete_camera_rtsps_streams_success():
    """Test deleting RTSPS streams successfully."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="test_key",
        verify_ssl=False,
    )

    with patch.object(
        client, "api_request_raw", new=AsyncMock(return_value=b"")
    ) as mock_request:
        result = await client.delete_camera_rtsps_streams(
            "camera123", ["high", "medium"]
        )

        mock_request.assert_called_once_with(
            public_api=True,
            url="/v1/cameras/camera123/rtsps-stream",
            method="DELETE",
            params=[("qualities", "high"), ("qualities", "medium")],
        )

        assert result is True


@pytest.mark.asyncio
async def test_delete_camera_rtsps_streams_single_quality():
    """Test deleting RTSPS streams with single quality."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="test_key",
        verify_ssl=False,
    )

    with patch.object(
        client, "api_request_raw", new=AsyncMock(return_value=b"")
    ) as mock_request:
        result = await client.delete_camera_rtsps_streams("camera123", "high")

        mock_request.assert_called_once_with(
            public_api=True,
            url="/v1/cameras/camera123/rtsps-stream",
            method="DELETE",
            params=[("qualities", "high")],
        )

        assert result is True


@pytest.mark.asyncio
async def test_delete_camera_rtsps_streams_failure():
    """Test deleting RTSPS streams with failure."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="test_key",
        verify_ssl=False,
    )

    with patch.object(
        client, "api_request_raw", new=AsyncMock(return_value=None)
    ) as mock_request:
        result = await client.delete_camera_rtsps_streams("camera123", ["high"])

        mock_request.assert_called_once_with(
            public_api=True,
            url="/v1/cameras/camera123/rtsps-stream",
            method="DELETE",
            params=[("qualities", "high")],
        )

        assert result is False


def test_rtsps_streams_class():
    """Test RTSPSStreams class functionality."""
    from uiprotect.api import RTSPSStreams

    # Test with multiple qualities
    streams = RTSPSStreams(
        high="rtsps://example.com/high",
        medium="rtsps://example.com/medium",
        low="rtsps://example.com/low",
    )

    assert streams.get_stream_url("high") == "rtsps://example.com/high"
    assert streams.get_stream_url("medium") == "rtsps://example.com/medium"
    assert streams.get_stream_url("low") == "rtsps://example.com/low"
    assert streams.get_stream_url("nonexistent") is None

    available_qualities = streams.get_available_stream_qualities()
    assert set(available_qualities) == {"high", "medium", "low"}

    # Test with single quality
    single_stream = RTSPSStreams(ultra="rtsps://example.com/ultra")
    assert single_stream.get_stream_url("ultra") == "rtsps://example.com/ultra"
    assert set(single_stream.get_available_stream_qualities()) == {"ultra"}

    # Test with empty streams
    empty_stream = RTSPSStreams()
    assert empty_stream.get_stream_url("any") is None
    assert empty_stream.get_available_stream_qualities() == []


def test_rtsps_streams_active_inactive():
    """Test RTSPSStreams active/inactive stream quality detection."""
    from uiprotect.api import RTSPSStreams

    # Test with mixed active and inactive streams
    streams = RTSPSStreams(
        high="rtsps://example.com/high",
        medium=None,  # inactive
        low="rtsps://example.com/low",
        ultra=None,  # inactive
    )

    # All available qualities (active + inactive)
    available = streams.get_available_stream_qualities()
    assert set(available) == {"high", "medium", "low", "ultra"}

    # Only active streams (with URLs)
    active = streams.get_active_stream_qualities()
    assert set(active) == {"high", "low"}

    # Only inactive streams (without URLs)
    inactive = streams.get_inactive_stream_qualities()
    assert set(inactive) == {"medium", "ultra"}


def test_rtsps_streams_all_active():
    """Test RTSPSStreams when all streams are active."""
    from uiprotect.api import RTSPSStreams

    streams = RTSPSStreams(
        high="rtsps://example.com/high",
        medium="rtsps://example.com/medium",
    )

    assert set(streams.get_available_stream_qualities()) == {"high", "medium"}
    assert set(streams.get_active_stream_qualities()) == {"high", "medium"}
    assert streams.get_inactive_stream_qualities() == []


def test_rtsps_streams_all_inactive():
    """Test RTSPSStreams when all streams are inactive."""
    from uiprotect.api import RTSPSStreams

    streams = RTSPSStreams(
        high=None,
        medium=None,
    )

    assert set(streams.get_available_stream_qualities()) == {"high", "medium"}
    assert streams.get_active_stream_qualities() == []
    assert set(streams.get_inactive_stream_qualities()) == {"high", "medium"}


def test_rtsps_streams_active_inactive_qualities():
    """Test RTSPSStreams active and inactive quality methods."""
    from uiprotect.api import RTSPSStreams

    # Test with mixed active (URLs) and inactive (None) streams
    streams = RTSPSStreams(
        high="rtsps://example.com/high",
        medium=None,  # inactive stream
        low="rtsps://example.com/low",
        ultra=None,  # inactive stream
    )

    # Test active streams (those with valid RTSPS URLs)
    active_qualities = streams.get_active_stream_qualities()
    assert set(active_qualities) == {"high", "low"}

    # Test inactive streams (those with None values)
    inactive_qualities = streams.get_inactive_stream_qualities()
    assert set(inactive_qualities) == {"medium", "ultra"}

    # Test all available streams (both active and inactive)
    available_qualities = streams.get_available_stream_qualities()
    assert set(available_qualities) == {"high", "medium", "low", "ultra"}

    # Test that active + inactive equals available
    assert set(active_qualities + inactive_qualities) == set(available_qualities)


def test_rtsps_streams_only_active():
    """Test RTSPSStreams with only active streams."""
    from uiprotect.api import RTSPSStreams

    streams = RTSPSStreams(
        high="rtsps://example.com/high",
        medium="rtsps://example.com/medium",
    )

    active_qualities = streams.get_active_stream_qualities()
    assert set(active_qualities) == {"high", "medium"}

    inactive_qualities = streams.get_inactive_stream_qualities()
    assert inactive_qualities == []

    available_qualities = streams.get_available_stream_qualities()
    assert set(available_qualities) == {"high", "medium"}


def test_rtsps_streams_only_inactive():
    """Test RTSPSStreams with only inactive streams."""
    from uiprotect.api import RTSPSStreams

    streams = RTSPSStreams(
        high=None,
        medium=None,
        low=None,
    )

    active_qualities = streams.get_active_stream_qualities()
    assert active_qualities == []

    inactive_qualities = streams.get_inactive_stream_qualities()
    assert set(inactive_qualities) == {"high", "medium", "low"}

    available_qualities = streams.get_available_stream_qualities()
    assert set(available_qualities) == {"high", "medium", "low"}


def test_rtsps_streams_empty():
    """Test RTSPSStreams with no streams."""
    from uiprotect.api import RTSPSStreams

    streams = RTSPSStreams()

    assert streams.get_active_stream_qualities() == []
    assert streams.get_inactive_stream_qualities() == []
    assert streams.get_available_stream_qualities() == []


def test_rtsps_streams_edge_cases():
    """Test RTSPSStreams edge cases and string validation."""
    from uiprotect.api import RTSPSStreams

    # Test with empty strings and various URL formats
    streams = RTSPSStreams(
        high="rtsps://valid.com/stream",
        medium="",  # empty string - still a string, so active
        low="http://invalid.com/stream",  # still a string, so active
        ultra="invalid_url",  # still a string, so active
    )

    # All string values are considered active (including empty strings)
    active_qualities = streams.get_active_stream_qualities()
    assert set(active_qualities) == {"high", "medium", "low", "ultra"}

    # Test with None values (should be inactive)
    streams_with_none = RTSPSStreams(
        high="rtsps://valid.com/stream",
        medium=None,  # None value - inactive
    )

    active_qualities = streams_with_none.get_active_stream_qualities()
    assert active_qualities == ["high"]

    inactive_qualities = streams_with_none.get_inactive_stream_qualities()
    assert inactive_qualities == ["medium"]

    available_qualities = streams.get_available_stream_qualities()
    assert set(available_qualities) == {"high", "medium", "low", "ultra"}


def test_rtsps_streams_none_pydantic_extra():
    """Test RTSPSStreams when __pydantic_extra__ is None."""
    from uiprotect.api import RTSPSStreams

    # Create an RTSPSStreams instance and manually set __pydantic_extra__ to None
    # This simulates edge cases where pydantic might not initialize extra fields
    streams = RTSPSStreams()

    # Manually set __pydantic_extra__ to None to test the None check
    streams.__pydantic_extra__ = None

    # All methods should return empty lists when __pydantic_extra__ is None
    assert streams.get_available_stream_qualities() == []
    assert streams.get_active_stream_qualities() == []
    assert streams.get_inactive_stream_qualities() == []

    # get_stream_url should still work (uses getattr, not __pydantic_extra__)
    assert streams.get_stream_url("any") is None


@pytest.mark.asyncio
async def test_camera_create_rtsps_streams():
    """Test Camera.create_rtsps_streams method."""
    from uiprotect.api import RTSPSStreams
    from uiprotect.data import Camera

    # Mock camera and API
    camera = AsyncMock(spec=Camera)
    camera.id = "test_camera_id"
    camera._api = AsyncMock()
    camera._api._api_key = "test_api_key"
    camera._api.create_camera_rtsps_streams = AsyncMock(
        return_value=RTSPSStreams(high="rtsps://example.com/high")
    )

    # Bind the actual method to the mock
    from uiprotect.data.devices import Camera

    camera.create_rtsps_streams = Camera.create_rtsps_streams.__get__(camera, Camera)

    # Test successful creation
    result = await camera.create_rtsps_streams(["high"])
    assert result is not None
    assert result.get_stream_url("high") == "rtsps://example.com/high"
    camera._api.create_camera_rtsps_streams.assert_called_once_with(
        "test_camera_id", ["high"]
    )


@pytest.mark.asyncio
async def test_camera_create_rtsps_streams_no_api_key():
    """Test Camera.create_rtsps_streams method without API key."""
    from uiprotect.data import Camera
    from uiprotect.exceptions import NotAuthorized

    # Mock camera and API without key
    camera = AsyncMock(spec=Camera)
    camera.id = "test_camera_id"
    camera._api = AsyncMock()
    camera._api._api_key = None

    # Bind the actual method to the mock
    from uiprotect.data.devices import Camera

    camera.create_rtsps_streams = Camera.create_rtsps_streams.__get__(camera, Camera)

    # Test that it raises NotAuthorized
    with pytest.raises(
        NotAuthorized, match="Cannot create RTSPS streams without an API key"
    ):
        await camera.create_rtsps_streams(["high"])


@pytest.mark.asyncio
async def test_camera_get_rtsps_streams():
    """Test Camera.get_rtsps_streams method."""
    from uiprotect.api import RTSPSStreams
    from uiprotect.data import Camera

    # Mock camera and API
    camera = AsyncMock(spec=Camera)
    camera.id = "test_camera_id"
    camera._api = AsyncMock()
    camera._api._api_key = "test_api_key"
    camera._api.get_camera_rtsps_streams = AsyncMock(
        return_value=RTSPSStreams(
            high="rtsps://example.com/high", medium="rtsps://example.com/medium"
        )
    )

    # Bind the actual method to the mock
    from uiprotect.data.devices import Camera

    camera.get_rtsps_streams = Camera.get_rtsps_streams.__get__(camera, Camera)

    # Test successful retrieval
    result = await camera.get_rtsps_streams()
    assert result is not None
    assert result.get_stream_url("high") == "rtsps://example.com/high"
    assert result.get_stream_url("medium") == "rtsps://example.com/medium"
    camera._api.get_camera_rtsps_streams.assert_called_once_with("test_camera_id")


@pytest.mark.asyncio
async def test_camera_get_rtsps_streams_no_api_key():
    """Test Camera.get_rtsps_streams method without API key."""
    from uiprotect.data import Camera
    from uiprotect.exceptions import NotAuthorized

    # Mock camera and API without key
    camera = AsyncMock(spec=Camera)
    camera.id = "test_camera_id"
    camera._api = AsyncMock()
    camera._api._api_key = None

    # Bind the actual method to the mock
    from uiprotect.data.devices import Camera

    camera.get_rtsps_streams = Camera.get_rtsps_streams.__get__(camera, Camera)

    # Test that it raises NotAuthorized
    with pytest.raises(
        NotAuthorized, match="Cannot get RTSPS streams without an API key"
    ):
        await camera.get_rtsps_streams()


@pytest.mark.asyncio
async def test_camera_delete_rtsps_streams():
    """Test Camera.delete_rtsps_streams method."""
    from uiprotect.data import Camera

    # Mock camera and API
    camera = AsyncMock(spec=Camera)
    camera.id = "test_camera_id"
    camera._api = AsyncMock()
    camera._api._api_key = "test_api_key"
    camera._api.delete_camera_rtsps_streams = AsyncMock(return_value=True)

    # Bind the actual method to the mock
    from uiprotect.data.devices import Camera

    camera.delete_rtsps_streams = Camera.delete_rtsps_streams.__get__(camera, Camera)

    # Test successful deletion
    result = await camera.delete_rtsps_streams(["high", "medium"])
    assert result is True
    camera._api.delete_camera_rtsps_streams.assert_called_once_with(
        "test_camera_id", ["high", "medium"]
    )


@pytest.mark.asyncio
async def test_camera_delete_rtsps_streams_no_api_key():
    """Test Camera.delete_rtsps_streams method without API key."""
    from uiprotect.data import Camera
    from uiprotect.exceptions import NotAuthorized

    # Mock camera and API without key
    camera = AsyncMock(spec=Camera)
    camera.id = "test_camera_id"
    camera._api = AsyncMock()
    camera._api._api_key = None

    # Bind the actual method to the mock
    from uiprotect.data.devices import Camera

    camera.delete_rtsps_streams = Camera.delete_rtsps_streams.__get__(camera, Camera)

    # Test that it raises NotAuthorized
    with pytest.raises(
        NotAuthorized, match="Cannot delete RTSPS streams without an API key"
    ):
        await camera.delete_rtsps_streams(["high"])


@pytest.mark.asyncio
async def test_raise_for_status_status_codes():
    """Test _raise_for_status with different HTTP status codes."""
    from uiprotect.api import ProtectApiClient
    from uiprotect.exceptions import BadRequest, NotAuthorized, NvrError

    # Create API client
    api = ProtectApiClient("test.com", 443, "username", "password")

    # Test success codes (2xx) - should not raise
    success_tests = [
        (199, True, NvrError),  # Just below 2xx range
        (200, False, None),  # Start of 2xx range
        (201, False, None),  # Common success code
        (299, False, None),  # End of 2xx range
        (300, True, NvrError),  # Just above 2xx range
    ]

    for status_code, should_raise, expected_exception in success_tests:
        response = Mock()
        response.status = status_code
        response.url = "https://test.com/api/test"

        with patch("uiprotect.api.get_response_reason", return_value="Test"):
            if should_raise:
                with pytest.raises(expected_exception):
                    await api._raise_for_status(response, raise_exception=True)
            else:
                # Should not raise any exception
                await api._raise_for_status(response, raise_exception=True)
                await api._raise_for_status(response, raise_exception=False)

    # Test specific error codes
    error_tests = [
        (400, BadRequest),
        (401, NotAuthorized),
        (403, NotAuthorized),
        (404, BadRequest),
        (429, NvrError),
        (500, NvrError),
    ]

    for status_code, expected_exception in error_tests:
        response = Mock()
        response.status = status_code
        response.url = "https://test.com/api/test"

        with patch("uiprotect.api.get_response_reason", return_value="Error"):
            # Should raise with raise_exception=True
            with pytest.raises(expected_exception):
                await api._raise_for_status(response, raise_exception=True)

            # Should not raise with raise_exception=False
            await api._raise_for_status(response, raise_exception=False)


@pytest.mark.asyncio
async def test_api_request_raw_error_handling():
    """Test api_request_raw error handling with raise_exception parameter."""
    from uiprotect.api import ProtectApiClient
    from uiprotect.exceptions import BadRequest

    # Create API client
    api = ProtectApiClient("test.com", 443, "username", "password")

    # Mock the request method to return a response with error status
    mock_response = AsyncMock()
    mock_response.status = 404
    mock_response.url = "https://test.com/api/test"
    mock_response.read = AsyncMock(return_value=b'{"error": "not found"}')
    mock_response.release = AsyncMock()

    api.request = AsyncMock(return_value=mock_response)

    # Mock get_response_reason
    with patch("uiprotect.api.get_response_reason", return_value="Not Found"):
        # Test with raise_exception=False - should return None
        result = await api.api_request_raw("/test", raise_exception=False)
        assert result is None

        # Verify that response.read() was NOT called since we return early
        mock_response.read.assert_not_called()

        # Reset mock for next test
        mock_response.reset_mock()
        api.request.reset_mock()

        # Test with raise_exception=True - should raise exception
        with pytest.raises(BadRequest):
            await api.api_request_raw("/test", raise_exception=True)

        # Verify that response.release() was called in the exception handler
        mock_response.release.assert_called_once()


@pytest.mark.asyncio
async def test_api_request_raw_success():
    """Test api_request_raw with successful response."""
    from uiprotect.api import ProtectApiClient

    # Create API client
    api = ProtectApiClient("test.com", 443, "username", "password")

    # Mock the request method to return a response with success status
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.url = "https://test.com/api/test"
    mock_response.read = AsyncMock(return_value=b'{"success": true}')
    mock_response.release = AsyncMock()

    api.request = AsyncMock(return_value=mock_response)

    # Test with success status - should return data
    result = await api.api_request_raw("/test", raise_exception=True)
    assert result == b'{"success": true}'

    # Verify that response.read() was called
    mock_response.read.assert_called_once()
    # Verify that response.release() was called
    mock_response.release.assert_called_once()


# Public API Tests


@pytest.mark.asyncio()
async def test_get_nvr_public_success():
    """Test successful NVR retrieval from public API."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="my_key",
        verify_ssl=False,
    )

    # Mock a simple valid object instead of trying to create full NVR
    mock_nvr = Mock()
    mock_nvr.id = "663d0a9d001d8803e40003ea"
    mock_nvr.name = "Test NVR"

    # Mock the NVR.from_unifi_dict method to return our mock
    with patch("uiprotect.data.nvr.NVR.from_unifi_dict", return_value=mock_nvr):
        client.api_request_obj = AsyncMock(return_value={"id": "test"})

        result = await client.get_nvr_public()

        assert result is not None
        assert result.id == "663d0a9d001d8803e40003ea"
        assert result.name == "Test NVR"
        client.api_request_obj.assert_called_with(url="/v1/nvrs", public_api=True)


@pytest.mark.asyncio()
async def test_get_nvr_public_error():
    """Test NVR retrieval error handling."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="my_key",
        verify_ssl=False,
    )

    client.api_request_obj = AsyncMock(side_effect=Exception("API Error"))

    with pytest.raises(Exception, match="API Error"):
        await client.get_nvr_public()

    client.api_request_obj.assert_called_with(url="/v1/nvrs", public_api=True)


@pytest.mark.asyncio()
async def test_get_lights_public_success():
    """Test successful lights retrieval from public API."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="my_key",
        verify_ssl=False,
    )

    # Mock simple light objects
    mock_light1 = Mock()
    mock_light1.id = "663d0aa401218803e4000449"
    mock_light1.name = "Light 1"

    mock_light2 = Mock()
    mock_light2.id = "663d0aa401218803e4000450"
    mock_light2.name = "Light 2"

    with patch(
        "uiprotect.data.devices.Light.from_unifi_dict",
        side_effect=[mock_light1, mock_light2],
    ):
        client.api_request_list = AsyncMock(return_value=[{"id": "1"}, {"id": "2"}])

        result = await client.get_lights_public()

        assert result is not None
        assert len(result) == 2
        assert result[0].id == "663d0aa401218803e4000449"
        assert result[1].id == "663d0aa401218803e4000450"
        client.api_request_list.assert_called_with(url="/v1/lights", public_api=True)


@pytest.mark.asyncio()
async def test_get_lights_public_error():
    """Test lights retrieval error handling."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="my_key",
        verify_ssl=False,
    )

    client.api_request_list = AsyncMock(side_effect=Exception("API Error"))

    with pytest.raises(Exception, match="API Error"):
        await client.get_lights_public()

    client.api_request_list.assert_called_with(url="/v1/lights", public_api=True)


@pytest.mark.asyncio()
async def test_get_light_public_success():
    """Test successful single light retrieval from public API."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="my_key",
        verify_ssl=False,
    )

    mock_light = Mock()
    mock_light.id = "663d0aa401218803e4000449"
    mock_light.name = "Light 1"

    with patch("uiprotect.data.devices.Light.from_unifi_dict", return_value=mock_light):
        client.api_request_obj = AsyncMock(return_value={"id": "test"})

        result = await client.get_light_public("663d0aa401218803e4000449")

        assert result is not None
        assert result.id == "663d0aa401218803e4000449"
        assert result.name == "Light 1"
        client.api_request_obj.assert_called_with(
            url="/v1/lights/663d0aa401218803e4000449", public_api=True
        )


@pytest.mark.asyncio()
async def test_get_light_public_error():
    """Test single light retrieval error handling."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="my_key",
        verify_ssl=False,
    )

    client.api_request_obj = AsyncMock(side_effect=Exception("API Error"))

    with pytest.raises(Exception, match="API Error"):
        await client.get_light_public("663d0aa401218803e4000449")

    client.api_request_obj.assert_called_with(
        url="/v1/lights/663d0aa401218803e4000449", public_api=True
    )


@pytest.mark.asyncio()
@patch("uiprotect.data.devices.Camera.from_unifi_dict")
async def test_get_cameras_public_success(mock_create):
    """Test successful cameras retrieval from public API."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="my_key",
        verify_ssl=False,
    )

    mock_cameras_data = [
        {
            "id": "663d0aa400918803e4000445",
            "name": "Camera 1",
            "type": "UVC G4 Doorbell Pro PoE",
            "mac": "E438830EDA97",
            "modelKey": "camera",
            "firmwareVersion": "4.74.78",
        },
        {
            "id": "663d0aa400918803e4000446",
            "name": "Camera 2",
            "type": "UVC G4 Doorbell Pro PoE",
            "mac": "E438830EDA98",
            "modelKey": "camera",
            "firmwareVersion": "4.74.78",
        },
    ]

    # Mock the API request
    client.api_request_list = AsyncMock(return_value=mock_cameras_data)

    # Mock the Camera.from_unifi_dict method
    mock_camera = Mock()
    mock_create.return_value = mock_camera

    result = await client.get_cameras_public()

    assert result is not None
    assert isinstance(result, list)
    assert len(result) == 2
    assert all(camera == mock_camera for camera in result)
    client.api_request_list.assert_called_with(url="/v1/cameras", public_api=True)
    assert mock_create.call_count == 2


@pytest.mark.asyncio()
async def test_get_cameras_public_error():
    """Test cameras retrieval error handling."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="my_key",
        verify_ssl=False,
    )

    client.api_request_list = AsyncMock(side_effect=Exception("API Error"))

    with pytest.raises(Exception, match="API Error"):
        await client.get_cameras_public()

    client.api_request_list.assert_called_with(url="/v1/cameras", public_api=True)


@pytest.mark.asyncio()
@patch("uiprotect.data.devices.Camera.from_unifi_dict")
async def test_get_camera_public_success(mock_create):
    """Test successful single camera retrieval from public API."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="my_key",
        verify_ssl=False,
    )

    mock_camera_data = {
        "id": "663d0aa400918803e4000445",
        "name": "Camera 1",
        "type": "UVC G4 Doorbell Pro PoE",
        "mac": "E438830EDA97",
        "modelKey": "camera",
        "firmwareVersion": "4.74.78",
    }

    # Mock the API request
    client.api_request_obj = AsyncMock(return_value=mock_camera_data)

    # Mock the Camera.from_unifi_dict method
    mock_camera = Mock()
    mock_camera.id = "663d0aa400918803e4000445"
    mock_camera.name = "Camera 1"
    mock_create.return_value = mock_camera

    result = await client.get_camera_public("663d0aa400918803e4000445")

    assert result is not None
    assert result.id == "663d0aa400918803e4000445"
    assert result.name == "Camera 1"
    client.api_request_obj.assert_called_with(
        url="/v1/cameras/663d0aa400918803e4000445", public_api=True
    )
    mock_create.assert_called_once_with(**mock_camera_data, api=client)


@pytest.mark.asyncio()
async def test_get_camera_public_error():
    """Test single camera retrieval error handling."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="my_key",
        verify_ssl=False,
    )

    client.api_request_obj = AsyncMock(side_effect=Exception("API Error"))

    with pytest.raises(Exception, match="API Error"):
        await client.get_camera_public("663d0aa400918803e4000445")

    client.api_request_obj.assert_called_with(
        url="/v1/cameras/663d0aa400918803e4000445", public_api=True
    )


@pytest.mark.asyncio()
@patch("uiprotect.data.devices.Chime.from_unifi_dict")
async def test_get_chimes_public_success(mock_create):
    """Test successful chimes retrieval from public API."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="my_key",
        verify_ssl=False,
    )

    mock_chimes_data = [
        {
            "id": "663d0aa401108803e4000447",
            "name": "Chime 1",
            "type": "UP Chime PoE",
            "mac": "E438830ABC01",
            "modelKey": "chime",
            "firmwareVersion": "1.5.4",
        }
    ]

    # Mock the API request
    client.api_request_list = AsyncMock(return_value=mock_chimes_data)

    # Mock the Chime.from_unifi_dict method
    mock_chime = Mock()
    mock_chime.id = "663d0aa401108803e4000447"
    mock_chime.name = "Chime 1"
    mock_create.return_value = mock_chime

    result = await client.get_chimes_public()

    assert result is not None
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0].id == "663d0aa401108803e4000447"
    assert result[0].name == "Chime 1"
    client.api_request_list.assert_called_with(url="/v1/chimes", public_api=True)
    mock_create.assert_called_once_with(**mock_chimes_data[0], api=client)


@pytest.mark.asyncio()
async def test_get_chimes_public_error():
    """Test chimes retrieval error handling."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="my_key",
        verify_ssl=False,
    )

    client.api_request_list = AsyncMock(side_effect=Exception("API Error"))

    with pytest.raises(Exception, match="API Error"):
        await client.get_chimes_public()

    client.api_request_list.assert_called_with(url="/v1/chimes", public_api=True)


@pytest.mark.asyncio()
@patch("uiprotect.data.devices.Chime.from_unifi_dict")
async def test_get_chime_public_success(mock_create):
    """Test successful single chime retrieval from public API."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="my_key",
        verify_ssl=False,
    )

    mock_chime_data = {
        "id": "663d0aa401108803e4000447",
        "name": "Chime 1",
        "type": "UP Chime PoE",
        "mac": "E438830ABC01",
        "modelKey": "chime",
        "firmwareVersion": "1.5.4",
    }

    # Mock the API request
    client.api_request_obj = AsyncMock(return_value=mock_chime_data)

    # Mock the Chime.from_unifi_dict method
    mock_chime = Mock()
    mock_chime.id = "663d0aa401108803e4000447"
    mock_chime.name = "Chime 1"
    mock_create.return_value = mock_chime

    result = await client.get_chime_public("663d0aa401108803e4000447")

    assert result is not None
    assert result.id == "663d0aa401108803e4000447"
    assert result.name == "Chime 1"
    client.api_request_obj.assert_called_with(
        url="/v1/chimes/663d0aa401108803e4000447", public_api=True
    )
    mock_create.assert_called_once_with(**mock_chime_data, api=client)


@pytest.mark.asyncio()
async def test_get_chime_public_error():
    """Test single chime retrieval error handling."""
    client = ProtectApiClient(
        "127.0.0.1",
        0,
        "user",
        "pass",
        api_key="my_key",
        verify_ssl=False,
    )

    client.api_request_obj = AsyncMock(side_effect=Exception("API Error"))

    with pytest.raises(Exception, match="API Error"):
        await client.get_chime_public("663d0aa401108803e4000447")

    client.api_request_obj.assert_called_with(
        url="/v1/chimes/663d0aa401108803e4000447", public_api=True
    )
