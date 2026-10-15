"""Read-only cc_json requests; authenticated transport is supplied by danfoss_mdg."""

from typing import Protocol


class ProtocolError(Exception):
    """Sanitized protocol failure; never includes a raw response."""


class AuthenticatedTransport(Protocol):
    async def exchange(self, request: dict) -> dict:
        """Send over an authenticated MDG cc_json channel, returning its envelope."""
        ...


def read_request(request_id: int, resource: str) -> dict:
    if type(request_id) is not int or request_id < 1:
        raise ValueError("Invalid request ID")
    if resource not in {
        "/api/rooms",
        "/api/world",
        "/api/system/info",
        "/api/system/apiversions",
    }:
        raise ValueError("Resource is outside the read-only allowlist")
    return {
        "id": request_id,
        "data": {
            "type": "JsonWebRequest",
            "data": {
                "method": "GET",
                "uri": resource,
                "headers": ["Accept: application/json; charset=utf-8"],
            },
        },
    }


def response_content(response: dict, request_id: int):
    try:
        if type(response["id"]) is not int or response["id"] != request_id:
            raise ProtocolError("Response ID mismatch")
        data = response["data"]["data"]
        status = data["status"]
        if type(status) is not int:
            raise ProtocolError("Invalid status")
        if not 200 <= status < 300:
            raise ProtocolError("CC rejected the read operation")
        return data.get("content")
    except (KeyError, TypeError, AttributeError):
        raise ProtocolError("Malformed CC response") from None


class ReadOnlyClient:
    """Client core; caller must supply a functioning, authenticated transport.

    Authentication and framing are provided by the supplied transport.
    No write operations are exposed. Calls are serialized for ID correlation.
    """

    def __init__(self, transport: AuthenticatedTransport):
        import asyncio

        self._transport = transport
        self._request_id = 0
        self._lock = asyncio.Lock()

    async def _read(self, resource: str):
        async with self._lock:
            self._request_id += 1
            request = read_request(self._request_id, resource)
            try:
                response = await self._transport.exchange(request)
            except Exception:
                # A transport exception may contain OTP, key paths or credentials.
                raise ProtocolError("Authenticated transport failed") from None
            return response_content(response, self._request_id)

    async def get_rooms(self) -> list[dict]:
        content = await self._read("/api/rooms")
        if not isinstance(content, list) or any(
            not isinstance(room, dict) or type(room.get("id")) is not int
            for room in content
        ):
            raise ProtocolError("Malformed rooms response")
        return content

    async def get_system_info(self):
        return await self._read("/api/system/info")

    async def get_api_versions(self):
        return await self._read("/api/system/apiversions")
