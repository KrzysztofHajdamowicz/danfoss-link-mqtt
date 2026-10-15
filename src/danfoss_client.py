"""Paired Danfoss client: allowlisted reads and explicit room setpoint writes."""

import asyncio, math
from danfoss_readonly import ReadOnlyClient, ProtocolError, response_content


def numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def temperature_request(request_id, room_id, temperature):
    if (
        type(request_id) is not int
        or request_id < 1
        or type(room_id) is not int
        or room_id < 0
    ):
        raise ProtocolError("Invalid temperature request identifier")
    if not numeric(temperature) or not 6 <= temperature <= 35:
        raise ProtocolError("Temperature outside client limits")
    return {
        "id": request_id,
        "data": {
            "type": "JsonWebRequest",
            "data": {
                "method": "PATCH",
                "uri": "/api/rooms/" + str(room_id),
                "headers": ["Accept: application/json; charset=utf-8"],
                "content": {
                    "id": room_id,
                    "setpoint": {"temperature": float(temperature)},
                },
            },
        },
    }


class DanfossClient(ReadOnlyClient):
    def __init__(self, transport, *, allow_writes=False):
        super().__init__(transport)
        self.allow_writes = allow_writes
        self.write_lock = asyncio.Lock()

    async def set_temperature(self, room_id, temperature):
        if not self.allow_writes:
            raise ProtocolError("Temperature writes disabled")
        temperature_request(1, room_id, temperature)
        async with self.write_lock:
            rooms = await self.get_rooms()
            room = next((r for r in rooms if r["id"] == room_id), None)
            if room is None:
                raise ProtocolError("Unknown room")
            low = room.get("minSetpoint", 6)
            high = room.get("maxSetpoint", 35)
            if (
                not numeric(low)
                or not numeric(high)
                or not max(6, low) <= temperature <= min(35, high)
            ):
                raise ProtocolError("Temperature outside room limits")
            point = room.get("setpoint", {})
            current = point.get("temperature") if isinstance(point, dict) else None
            if numeric(current) and abs(current - temperature) <= 0.05:
                return room
            async with self._lock:
                self._request_id += 1
                req = temperature_request(self._request_id, room_id, temperature)
                try:
                    reply = await self._transport.exchange(req)
                except Exception:
                    raise ProtocolError(
                        "Temperature result uncertain; check CC before retrying"
                    ) from None
                response_content(reply, self._request_id)
            # Never automatically retry PATCH after timeout or failed readback.
            updated = next(
                (r for r in await self.get_rooms() if r["id"] == room_id), None
            )
            point = updated.get("setpoint", {}) if updated else {}
            actual = point.get("temperature") if isinstance(point, dict) else None
            if not numeric(actual) or abs(actual - temperature) > 0.05:
                raise ProtocolError(
                    "Temperature readback did not confirm requested value"
                )
            return updated
