"""cotcp packet=2: unsigned two-byte big-endian byte length followed by payload."""

import asyncio

MAX_FRAME = 65535


class FrameError(Exception):
    pass


async def read_frame(reader, timeout=15):
    try:
        async with asyncio.timeout(timeout):
            header = await reader.readexactly(2)
            size = int.from_bytes(header, "big")
            if not 1 <= size <= MAX_FRAME:
                raise FrameError("Invalid frame length")
            return await reader.readexactly(size)
    except (TimeoutError, asyncio.IncompleteReadError, OSError):
        raise FrameError("Frame receive failed") from None


async def send_frame(writer, payload, timeout=15):
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_FRAME:
        raise FrameError("Invalid frame length")
    try:
        async with asyncio.timeout(timeout):
            writer.write(len(payload).to_bytes(2, "big") + payload)
            await writer.drain()
    except (TimeoutError, OSError):
        raise FrameError("Frame send failed") from None
