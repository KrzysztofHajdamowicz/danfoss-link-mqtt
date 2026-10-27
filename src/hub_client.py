"""Verified hub operations: fresh read, one write, readback, never an automatic retry."""

from danfoss_client import numeric
from danfoss_readonly import ProtocolError
from hub_model import SETTINGS, parse_world, setpoints_blocked
from hub_transport import MODE_TYPES, HubRejected, hub_request

ROOM_FIELDS = {
    "setpoint": lambda value: {"setpoint": {"temperature": value}},
    "athome_setpoint": lambda value: {"regulation": {"atHomeSetpoint": value}},
    "away_setpoint": lambda value: {"regulation": {"awaySetpoint": value}},
}


class HubPrecondition(HubRejected):
    """Refused locally from a fresh read; nothing was sent to the CC."""

    def __init__(self, message):
        super().__init__(None, [message])


class HubUnverified(ProtocolError):
    """A write was sent but not confirmed; check the CC before retrying."""


def _close(actual, expected):
    return numeric(actual) and abs(actual - expected) <= 0.05


class HubClient:
    def __init__(self, transport, *, allow_writes=False):
        self._transport = transport
        self.allow_writes = allow_writes

    async def snapshot(self):
        try:
            world = await self._transport.request("GET", "/api/world")
        except ProtocolError:
            raise
        except Exception:
            # A transport exception may contain key paths or credentials.
            raise ProtocolError("Authenticated transport failed") from None
        return parse_world(world)

    async def _write(self, method, uri, content):
        if not self.allow_writes:
            raise ProtocolError("Writes disabled")
        hub_request(1, method, uri, content)  # allowlist failures are definite: not sent
        try:
            await self._transport.request(method, uri, content)
        except HubRejected:
            raise
        except Exception:
            raise HubUnverified("Write result uncertain; check the CC before retrying") from None

    async def _verify(self, check):
        try:
            snapshot = await self.snapshot()
        except Exception:
            raise HubUnverified("Write sent but readback failed") from None
        if not check(snapshot):
            raise HubUnverified("Readback did not confirm the requested value")
        return snapshot

    async def set_room(self, room_id, field, value):
        """Set a room's setpoint, athome_setpoint or away_setpoint."""
        if field not in ROOM_FIELDS or not numeric(value):
            raise ProtocolError("Invalid room write")
        snapshot = await self.snapshot()
        room = next((r for r in snapshot["rooms"] if r["id"] == room_id), None)
        if room is None:
            raise HubPrecondition("Unknown room")
        if not room["min_temp"] <= value <= room["max_temp"]:
            raise HubPrecondition("Temperature outside room limits")
        if field == "setpoint" and setpoints_blocked(snapshot):
            raise HubPrecondition(
                "Setpoint changes are blocked while Pause or Holiday is active"
            )
        if _close(room[field], value):
            return snapshot
        content = {"id": room_id, **ROOM_FIELDS[field](float(value))}
        await self._write("PATCH", "/api/rooms/" + str(room_id), content)

        def confirmed(fresh):
            room = next((r for r in fresh["rooms"] if r["id"] == room_id), None)
            return room is not None and _close(room[field], value)

        return await self._verify(confirmed)

    async def set_temperature_setting(self, kind, value):
        """Set the global pause or vacation (holiday) temperature."""
        if kind not in SETTINGS or not numeric(value):
            raise ProtocolError("Invalid temperature setting")
        snapshot = await self.snapshot()
        setting = snapshot["settings"][kind]
        if setting is None:
            raise HubPrecondition("Temperature setting not reported by the CC")
        if not setting["min"] <= value <= setting["max"]:
            raise HubPrecondition("Temperature outside CC limits")
        if _close(setting["setpoint"], value):
            return snapshot
        content = {SETTINGS[kind][0]: {"setpoint": float(value)}}
        await self._write("PATCH", "/api/settings/temperature", content)
        return await self._verify(
            lambda fresh: _close((fresh["settings"][kind] or {}).get("setpoint"), value)
        )

    async def set_mode(self, key, active, start=None, end=None):
        """Switch pause/vacation/athome; start/end are CC-local ISO times."""
        if key not in MODE_TYPES or type(active) is not bool:
            raise ProtocolError("Invalid mode write")
        window = (start, end) if key == "vacation" and active else None
        if window is None and (start, end) != (None, None):
            raise ProtocolError("Times only apply when activating Holiday")
        if window is not None and None in window:
            raise HubPrecondition("Set the holiday end time first")
        snapshot = await self.snapshot()
        mode = snapshot["modes"][key]
        if mode is None:
            raise HubPrecondition("Mode not reported by the CC")
        if mode["active"] == active and (
            window is None or (mode["start"], mode["end"]) == window
        ):
            return snapshot
        content = {"type": MODE_TYPES[key], "state": "Active" if active else "Inactive"}
        if window is not None:
            content.update(startTime=start, endTime=end)
        await self._write("PUT", "/api/modes/" + key, content)

        def confirmed(fresh):
            mode = fresh["modes"][key]
            if mode is None:
                return False
            # A holiday starting later may be reported as scheduled, not yet active.
            return mode["active"] == active or (
                window is not None and (mode["start"], mode["end"]) == window
            )

        return await self._verify(confirmed)

    async def set_onoff(self, device_id, on):
        if type(device_id) is not int or type(on) is not bool:
            raise ProtocolError("Invalid on/off write")
        snapshot = await self.snapshot()
        device = next((d for d in snapshot["onoff"] if d["id"] == device_id), None)
        if device is None:
            raise HubPrecondition("Unknown on/off device")
        if device["on"] is on:
            return snapshot
        content = {"id": device_id, "state": "On" if on else "Off"}
        await self._write("PATCH", "/api/onoffdevices/" + str(device_id), content)
        return await self._verify(
            lambda fresh: any(
                d["id"] == device_id and d["on"] is on for d in fresh["onoff"]
            )
        )
