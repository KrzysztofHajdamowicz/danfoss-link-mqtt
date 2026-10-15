"""Explicit opt-in transport accepts exactly the APK room temperature PATCH."""

from .readonly_transport import PairedReadTransport
from danfoss_readonly import ProtocolError
from danfoss_client import temperature_request


class PairedControlTransport(PairedReadTransport):
    def __init__(self, control, peer):
        super().__init__(control, peer)
        self.allow_writes = False

    @classmethod
    async def connect(cls, state, *, allow_writes=False):
        if type(allow_writes) is not bool:
            raise ProtocolError("Invalid write option")
        transport = await super().connect(state)
        transport.allow_writes = allow_writes
        return transport

    async def exchange(self, request):
        try:
            body = request["data"]["data"]
        except (KeyError, TypeError):
            raise ProtocolError("Request rejected") from None
        if body.get("method") != "PATCH":
            return await super().exchange(request)
        if not self.allow_writes:
            raise ProtocolError("Temperature writes disabled")
        try:
            content = body["content"]
            expected = temperature_request(
                request["id"], content["id"], content["setpoint"]["temperature"]
            )
        except (KeyError, TypeError):
            raise ProtocolError("Temperature request rejected") from None
        if request != expected:
            raise ProtocolError("Temperature request rejected")
        return await self._exchange_checked(request)
