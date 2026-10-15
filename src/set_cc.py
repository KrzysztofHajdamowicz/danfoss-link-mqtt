"""Explicit one-shot room target change with readback; never retries PATCH."""

import argparse, asyncio
from pathlib import Path
from danfoss_client import DanfossClient
from danfoss_mdg.control_transport import PairedControlTransport


async def main(args):
    transport = None
    try:
        transport = await PairedControlTransport.connect(args.state, allow_writes=True)
        client = DanfossClient(transport, allow_writes=True)
        await client.set_temperature(args.room_id, args.temperature)
        print("Temperature PATCH accepted and readback verified")
    except Exception:
        print("Temperature result not verified; check CC before retrying")
        return 1
    finally:
        if transport:
            await transport.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=Path("/state/apk-profile"))
    parser.add_argument("--room-id", type=int, required=True)
    parser.add_argument("--temperature", type=float, required=True)
    raise SystemExit(asyncio.run(main(parser.parse_args())))
