"""One-shot read test; no raw payloads, identity IDs, keys or tokens in output."""

import argparse, asyncio, json, math, ssl, sys
from pathlib import Path
from danfoss_readonly import ReadOnlyClient
from danfoss_mdg.readonly_transport import PairedReadTransport


def number(value):
    return (
        value
        if type(value) in (int, float) and math.isfinite(value) and -50 <= value <= 100
        else None
    )


async def run(state):
    transport = None
    stage = "connect"
    try:
        transport = await PairedReadTransport.connect(state)
        print(
            "Saved PACE certificate matched; paired read channel established",
            flush=True,
        )
        client = ReadOnlyClient(transport)
        stage = "system_info"
        await client.get_system_info()
        print("GET system info succeeded; raw response omitted", flush=True)
        stage = "rooms"
        rooms = await client.get_rooms()
        print("GET rooms succeeded; room_count=" + str(len(rooms)), flush=True)
        for index, room in enumerate(rooms, 1):
            sensor = room.get("roomTemperature", {})
            if not isinstance(sensor, dict):
                sensor = {}
            setpoint = room.get("setpoint", {})
            if not isinstance(setpoint, dict):
                setpoint = {}
            print(
                json.dumps(
                    {
                        "room_index": index,
                        "temperature": number(room.get("ambientTemperature")),
                        "setpoint": number(setpoint.get("temperature")),
                    }
                ),
                flush=True,
            )
        return 0
    except Exception:
        print(
            "Paired read test failed at "
            + stage
            + "; no writes sent and no raw response logged",
            file=sys.stderr,
        )
        return 1
    finally:
        if transport:
            await transport.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=Path("/state/apk-profile"))
    args = parser.parse_args()
    if not ssl.OPENSSL_VERSION.startswith("OpenSSL 3.5.9 "):
        print("Use the isolated private MDG runtime", file=sys.stderr)
        return 1
    return asyncio.run(run(args.state))


if __name__ == "__main__":
    sys.exit(main())
