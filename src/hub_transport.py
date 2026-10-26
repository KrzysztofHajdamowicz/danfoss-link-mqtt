"""Paired cc_json transport with an explicit allowlist for hub reads and writes."""

import re
from danfoss_mdg.readonly_transport import PairedReadTransport
from danfoss_client import numeric
from danfoss_readonly import ProtocolError

READ_HEADERS = ["Accept: application/json; charset=utf-8"]
WRITE_HEADERS = READ_HEADERS + ["Content-Type: application/json; charset=utf-8"]
ROOM_ID = r"(0|[1-9][0-9]{0,9})"
READS = re.compile(
    r"/api/(?:world|rooms|rooms/"
    + ROOM_ID
    + r"|modes|settings/temperature|system/info|system/status|onoffdevices)"
)
MODE_TYPES = {"pause": "Pause", "vacation": "Vacation", "athome": "AtHome"}
LOCAL_TIME = re.compile(r"20[0-9]{2}-[01][0-9]-[0-3][0-9]T[0-2][0-9]:[0-5][0-9]:[0-5][0-9]")
# Hard client limits; the CC's own, usually narrower, limits are applied by HubClient.
ROOM_LIMITS = (6, 35)
SETTING_LIMITS = {"pauseTemperature": (4, 10), "vacationTemperature": (5, 35)}


class HubRejected(ProtocolError):
    """The CC refused the operation; messages are sanitized CC text."""

    def __init__(self, status, messages=()):
        self.status = status
        self.messages = list(messages)
        super().__init__("CC rejected the operation (status " + str(status) + ")")


def sanitize_text(value, limit=200):
    if not isinstance(value, str):
        return None
    text = "".join(c for c in value if c.isprintable())[:limit].strip()
    return text or None


def _temperature(value, limits):
    return numeric(value) and limits[0] <= value <= limits[1]


def _exact(content, keys):
    return isinstance(content, dict) and set(content) == set(keys)


def _valid_room_patch(room_id, content):
    if not _exact(content, ("id", "setpoint")) and not _exact(
        content, ("id", "regulation")
    ):
        return False
    if type(content["id"]) is not int or content["id"] != room_id:
        return False
    if "setpoint" in content:
        point = content["setpoint"]
        return _exact(point, ("temperature",)) and _temperature(
            point["temperature"], ROOM_LIMITS
        )
    regulation = content["regulation"]
    return (
        isinstance(regulation, dict)
        and len(regulation) == 1
        and next(iter(regulation)) in ("atHomeSetpoint", "awaySetpoint")
        and _temperature(next(iter(regulation.values())), ROOM_LIMITS)
    )


def _valid_settings_patch(content):
    if not isinstance(content, dict) or len(content) != 1:
        return False
    key, value = next(iter(content.items()))
    return (
        key in SETTING_LIMITS
        and _exact(value, ("setpoint",))
        and _temperature(value["setpoint"], SETTING_LIMITS[key])
    )


def _valid_mode_put(mode, content):
    if not isinstance(content, dict) or not {"type", "state"} <= set(content):
        return False
    if content["type"] != MODE_TYPES[mode] or content["state"] not in (
        "Active",
        "Inactive",
    ):
        return False
    extra = set(content) - {"type", "state"}
    if mode == "vacation" and extra:
        return (
            content["state"] == "Active"
            and extra == {"startTime", "endTime"}
            and all(
                isinstance(content[k], str) and LOCAL_TIME.fullmatch(content[k])
                for k in extra
            )
            and content["startTime"] < content["endTime"]
        )
    return not extra


def _valid_onoff_patch(device_id, content):
    return (
        _exact(content, ("id", "state"))
        and type(content["id"]) is int
        and content["id"] == device_id
        and content["state"] in ("On", "Off")
    )


def hub_request(request_id, method, uri, content=None):
    """Build the exact cc_json envelope, or raise if it is outside the allowlist."""
    if type(request_id) is not int or request_id < 1 or not isinstance(uri, str):
        raise ProtocolError("Invalid hub request")
    valid = False
    if method == "GET":
        valid = content is None and READS.fullmatch(uri) is not None
    elif method == "PATCH":
        if match := re.fullmatch(r"/api/rooms/" + ROOM_ID, uri):
            valid = _valid_room_patch(int(match.group(1)), content)
        elif match := re.fullmatch(r"/api/onoffdevices/" + ROOM_ID, uri):
            valid = _valid_onoff_patch(int(match.group(1)), content)
        elif uri == "/api/settings/temperature":
            valid = _valid_settings_patch(content)
    elif method == "PUT":
        if match := re.fullmatch(r"/api/modes/(pause|vacation|athome)", uri):
            valid = _valid_mode_put(match.group(1), content)
    if not valid:
        raise ProtocolError("Hub request rejected by allowlist")
    data = {
        "method": method,
        "uri": uri,
        "headers": list(READ_HEADERS if content is None else WRITE_HEADERS),
    }
    if content is not None:
        data["content"] = content
    return {"id": request_id, "data": {"type": "JsonWebRequest", "data": data}}


def hub_content(response, request_id):
    """Return response content, or raise HubRejected with sanitized CC messages."""
    try:
        if type(response["id"]) is not int or response["id"] != request_id:
            raise ProtocolError("Response ID mismatch")
        data = response["data"]["data"]
        status = data["status"]
        if type(status) is not int:
            raise ProtocolError("Invalid status")
        content = data.get("content")
    except (KeyError, TypeError, AttributeError):
        raise ProtocolError("Malformed CC response") from None
    if 200 <= status < 300:
        return content
    messages = []
    if isinstance(content, dict) and isinstance(content.get("errorMessages"), list):
        messages = [
            m for m in map(sanitize_text, content["errorMessages"][:4]) if m is not None
        ]
    raise HubRejected(status, messages)


class HubTransport(PairedReadTransport):
    def __init__(self, control, peer):
        super().__init__(control, peer)
        self._request_id = 0

    async def request(self, method, uri, content=None):
        self._request_id += 1
        request_id = self._request_id
        request = hub_request(request_id, method, uri, content)
        return hub_content(await self._exchange_checked(request), request_id)
