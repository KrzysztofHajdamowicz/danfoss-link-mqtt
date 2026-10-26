"""Local broker test of the hub bridge; requires test-only amqtt and paho-mqtt."""

import asyncio, contextlib, io, json, socket, tempfile, unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

try:
    from amqtt.broker import Broker
    import paho.mqtt.client as mqtt
except ImportError:
    Broker = None
from hub_bridge import HubBridge
from hub_fixtures import FakeCC
from mqtt_bridge import Settings


@unittest.skipIf(Broker is None, "Install amqtt and paho-mqtt to run wire test")
class HubWireTest(unittest.IsolatedAsyncioTestCase):
    async def test_migration_discovery_and_verified_command_round_trip(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        broker = Broker(
            {
                "listeners": {"default": {"type": "tcp", "bind": f"127.0.0.1:{port}"}},
                "plugins": {
                    "amqtt.plugins.authentication.AnonymousAuthPlugin": {
                        "allow_anonymous": True
                    }
                },
            }
        )
        await broker.start()
        loop = asyncio.get_running_loop()
        messages = asyncio.Queue()
        history = []
        connected = asyncio.Event()
        subscribed = asyncio.Event()
        observer = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="synthetic-observer")
        observer.on_connect = lambda *args: loop.call_soon_threadsafe(connected.set)
        observer.on_subscribe = lambda *args: loop.call_soon_threadsafe(subscribed.set)
        observer.on_message = lambda c, u, m: loop.call_soon_threadsafe(
            messages.put_nowait, (m.topic, m.payload.decode(), m.retain)
        )
        observer.connect_async("127.0.0.1", port)
        observer.loop_start()
        task = None

        async def until(predicate):
            async with asyncio.timeout(10):
                while True:
                    item = await messages.get()
                    history.append(item)
                    if predicate(item):
                        return item

        try:
            await asyncio.wait_for(connected.wait(), 5)
            with tempfile.TemporaryDirectory() as directory:
                Path(directory).chmod(0o700)
                with patch(
                    "hub_bridge.load_trust",
                    return_value=(None, None, "synthetic-peer", b"fixture"),
                ):
                    bridge = HubBridge(Settings("127.0.0.1", port), Path(directory))
                cid = bridge.controller_id
                sent = []
                publish = bridge.client.publish

                def recording_publish(topic, payload=None, *args, **kwargs):
                    sent.append((topic, payload))
                    return publish(topic, payload, *args, **kwargs)

                bridge.client.publish = recording_publish
                # A retained flat-bridge entity from an earlier deployment.
                legacy = "homeassistant/climate/danfoss_link_" + cid + "/room_5/config"
                publication = observer.publish(legacy, '{"name":"Køkken"}', qos=1, retain=True)
                await asyncio.to_thread(publication.wait_for_publish, 3)
                # QoS 0: amqtt intermittently stalls QoS 1 delivery to subscribers.
                observer.subscribe([("homeassistant/#", 0), ("danfoss_link/#", 0)])
                await asyncio.wait_for(subscribed.wait(), 5)
                await until(lambda m: m[0] == legacy)
                cc = FakeCC()
                cc.close = AsyncMock()
                with (
                    patch("hub_bridge.HubTransport.connect", new_callable=AsyncMock, return_value=cc),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    task = asyncio.create_task(bridge.run())
                    await until(lambda m: m[0] == legacy and "migrate_discovery" in m[1])
                    room = await until(lambda m: m[0].endswith("_room_5/config"))
                    self.assertEqual(json.loads(room[1])["device"]["via_device"], "danfoss_link_" + cid)
                    await until(lambda m: m[0] == legacy and m[1] == "")
                    await until(lambda m: m[0] == bridge.base + "/availability" and m[1] == "online")
                    hub = next(json.loads(p) for t, p, _ in history if t.endswith("danfoss_link_" + cid + "/config"))
                    self.assertIn("pause", hub["components"])
                    # Retained commands are never applied; fresh ones are verified and republished.
                    observer.publish(bridge.base + "/hub/pause/set", "ON", qos=0, retain=True)
                    observer.publish(bridge.base + "/hub/pause/set", "ON", qos=0, retain=False)
                    await until(
                        lambda m: m[0] == bridge.base + "/hub/state" and json.loads(m[1])["pause"] == "ON"
                    )
                    observer.publish(bridge.base + "/hub/pause/set", "OFF", qos=0)
                    await until(
                        lambda m: m[0] == bridge.base + "/system/state"
                        and json.loads(m[1])["current_state"] == "Manual"
                        and any(w[2]["state"] == "Inactive" for w in cc.writes)
                    )
                    self.assertEqual(
                        [w[2] for w in cc.writes],
                        [{"type": "Pause", "state": "Active"}, {"type": "Pause", "state": "Inactive"}],
                    )
                    bridge.stop.set()
                    await asyncio.wait_for(task, 5)
                    task = None
                # amqtt does not keep one client's QoS 1 publish order when forwarding,
                # so the shutdown state is checked at the bridge's MQTT client.
                availability = [p for t, p in sent if t == bridge.base + "/availability"]
                self.assertEqual(availability[-1], "offline")
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            observer.disconnect()
            await asyncio.to_thread(observer.loop_stop)
            # amqtt's shutdown can hang after many retained QoS 1 messages.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(broker.shutdown(), 5)


if __name__ == "__main__":
    unittest.main()
