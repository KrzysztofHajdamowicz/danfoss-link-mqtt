"""Local broker test; requires test-only amqtt and production paho-mqtt."""

import asyncio, json, socket, tempfile, unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

try:
    from amqtt.broker import Broker
    import paho.mqtt.client as mqtt
except ImportError:
    Broker = None
from mqtt_bridge import MQTTBridge, Settings, clean_world
from test_temperature_control import Fake
from rooms import clean_rooms


@unittest.skipIf(Broker is None, "Install amqtt and paho-mqtt to run wire test")
class WireTest(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_retained_replay_and_verified_write(self):
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
        observer = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id="synthetic-observer"
        )
        observer.on_connect = lambda *args: loop.call_soon_threadsafe(connected.set)
        observer.on_subscribe = lambda *args: loop.call_soon_threadsafe(subscribed.set)
        observer.on_message = lambda c, u, m: loop.call_soon_threadsafe(
            messages.put_nowait, (m.topic, m.payload, m.retain)
        )
        observer.connect_async("127.0.0.1", port)
        observer.loop_start()
        task = None
        try:
            await asyncio.wait_for(connected.wait(), 5)
            observer.subscribe([("homeassistant/#", 1), ("danfoss_link/#", 1)])
            await asyncio.wait_for(subscribed.wait(), 5)
            with tempfile.TemporaryDirectory() as directory:
                with patch(
                    "mqtt_bridge.load_trust",
                    return_value=(None, None, "synthetic-peer", b"fixture"),
                ):
                    bridge = MQTTBridge(Settings("127.0.0.1", port), Path(directory))
                command = bridge.base + "/room/77/temperature/set"
                publication = observer.publish(command, "10", qos=1, retain=True)
                await asyncio.to_thread(publication.wait_for_publish, 3)
                transport = Fake()
                transport.close = AsyncMock()
                original_exchange = transport.exchange

                async def exchange(req):
                    if req["data"]["data"]["uri"] == "/api/world":
                        world = {
                            "allRooms": [
                                {
                                    "id": 77,
                                    "name": "Køkken",
                                    "ambientTemperature": 20.4,
                                    "minSetpoint": 6,
                                    "maxSetpoint": 25,
                                    "setpoint": {"temperature": transport.target},
                                    "regulationState": "Deadband",
                                }
                            ],
                            "systemStatus": {"currentState": "Asleep"},
                            "allUnresolvedAlarms": [],
                        }
                        return {
                            "id": req["id"],
                            "data": {
                                "type": "JsonWebResponse",
                                "data": {"status": 200, "content": world},
                            },
                        }
                    return await original_exchange(req)

                transport.exchange = exchange

                async def read(*args):
                    return clean_world(
                        {
                            "allRooms": [
                                {
                                    "id": 77,
                                    "name": "Køkken",
                                    "ambientTemperature": 20.4,
                                    "minSetpoint": 6,
                                    "maxSetpoint": 25,
                                    "setpoint": {"temperature": transport.target},
                                    "regulationState": "Deadband",
                                }
                            ],
                            "systemStatus": {"currentState": "Asleep"},
                            "allUnresolvedAlarms": [],
                        }
                    )

                with (
                    patch("mqtt_bridge.cloud_operation", side_effect=read),
                    patch(
                        "mqtt_bridge.PairedControlTransport.connect",
                        new_callable=AsyncMock,
                        return_value=transport,
                    ) as connect,
                ):
                    task = asyncio.create_task(bridge.run())

                    async def until(predicate):
                        async with asyncio.timeout(8):
                            while True:
                                item = await messages.get()
                                history.append(item)
                                if predicate(item):
                                    return item

                    await until(
                        lambda m: m[0].endswith("/config")
                        and json.loads(m[1]).get("name") == "Køkken"
                    )
                    await until(
                        lambda m: m[0] == bridge.base + "/availability"
                        and m[1] == b"online"
                    )
                    connect.assert_not_called()
                    values = {
                        topic: json.loads(payload)
                        for topic, payload, _ in history
                        if topic.endswith(
                            ("/system/state", "/alarms/state", "/alarms/attributes")
                        )
                    }
                    self.assertEqual(
                        values[bridge.base + "/system/state"]["current_state"], "Asleep"
                    )
                    self.assertEqual(values[bridge.base + "/alarms/state"]["count"], 0)
                    self.assertEqual(
                        values[bridge.base + "/alarms/attributes"]["alarms"], []
                    )
                    room_state = next(
                        json.loads(p)
                        for t, p, _ in history
                        if t == bridge.base + "/room/77/state"
                    )
                    self.assertEqual(room_state["action"], "idle")
                    # Fresh command delivered over real MQTT; all cloud calls remain synthetic.
                    observer.publish(command, "10", qos=0, retain=False)
                    await until(
                        lambda m: m[0] == bridge.base + "/room/77/state"
                        and json.loads(m[1])["setpoint"] == 10
                    )
                    self.assertEqual(
                        sum(
                            r["data"]["data"]["method"] == "PATCH"
                            for r in transport.requests
                        ),
                        1,
                    )
                    bridge.stop.set()
                    await asyncio.wait_for(task, 5)
                    task = None
                    await until(
                        lambda m: m[0] == bridge.base + "/availability"
                        and m[1] == b"offline"
                    )
                    barrier = observer.publish("fixture/barrier", "done", qos=1)
                    await asyncio.to_thread(barrier.wait_for_publish, 3)
                    await asyncio.sleep(0.1)
        finally:
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            observer.disconnect()
            await asyncio.to_thread(observer.loop_stop)
            await broker.shutdown()


if __name__ == "__main__":
    unittest.main()
