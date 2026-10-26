"""Synthetic CC world and an in-memory CC that enforces the real allowlist."""

import copy
from hub_transport import HubRejected, hub_request

DAY = [
    {"state": "Away", "startTimeFromMidnight": "00:00:00", "endTimeFromMidnight": "06:00:00"},
    {"state": "AtHome", "startTimeFromMidnight": "06:00:00", "endTimeFromMidnight": "22:00:00"},
    {"state": "Away", "startTimeFromMidnight": "22:00:00", "endTimeFromMidnight": "1.00:00:00"},
]


def room(room_id, name, temperature, setpoint):
    return {
        "id": room_id,
        "name": name,
        "zone": "Living",
        "hasTemperatureSensor": True,
        "ambientTemperature": temperature,
        "minSetpoint": 6.0,
        "maxSetpoint": 28.0,
        "setpoint": {"temperature": setpoint, "type": "Adjustable"},
        "regulation": {
            "type": "Manual",
            "manualSetpoint": setpoint,
            "atHomeSetpoint": 21.0,
            "awaySetpoint": 18.0,
        },
        "regulationState": "Deadband",
        "state": "Normal",
        "mode": "Manual",
        "schedule": {"monday": copy.deepcopy(DAY)},
    }


WORLD = {
    "allRooms": [room(1, "Hallway", 22.6, 21.5), room(5, "Køkken", 20.4, 20.0)],
    "systemStatus": {"currentState": "Manual"},
    "systemInfo": {"name": "Synthetic home", "serialNo": "0000000001", "version": "4.2.1724"},
    "modes": [
        {"type": "AtHome", "state": "Inactive"},
        {"type": "Vacation", "state": "Inactive"},
        {"type": "Pause", "state": "Inactive"},
    ],
    "temperatureSettings": {
        "temperatureScale": "Celcius",
        "pauseTemperature": {"setpoint": 6.0, "minSetpoint": 4.0, "maxSetpoint": 10.0},
        "vacationTemperature": {"setpoint": 6.0, "minSetpoint": 5.0, "maxSetpoint": 35.0},
    },
    "onOffDevices": [],
    "allUnresolvedAlarms": [],
}
STATES = {"Pause": "Pause", "Vacation": "Vacation", "AtHome": "AtHome"}
BLOCKED = "Setpoint can only be changed while room is not in paused or vacation mode."


class FakeCC:
    """Behaves like the CC for the verified operations, including the Pause gotcha."""

    def __init__(self, world=None, *, fail_write=False, ignore_writes=False):
        self.world = copy.deepcopy(world or WORLD)
        self.fail_write = fail_write
        self.ignore_writes = ignore_writes
        self.requests = []

    @property
    def writes(self):
        return [r for r in self.requests if r[0] != "GET"]

    def mode(self, kind):
        return next(m for m in self.world["modes"] if m["type"] == kind)

    def _status(self):
        active = [m["type"] for m in self.world["modes"] if m["state"] == "Active"]
        self.world["systemStatus"]["currentState"] = (
            STATES[active[0]] if active else "Manual"
        )

    async def request(self, method, uri, content=None):
        hub_request(1, method, uri, content)
        self.requests.append((method, uri, copy.deepcopy(content)))
        if method == "GET":
            return copy.deepcopy(self.world)
        if self.fail_write:
            raise OSError("synthetic-secret-that-must-not-escape")
        if self.ignore_writes:
            return {}
        if uri.startswith("/api/rooms/"):
            target = next(r for r in self.world["allRooms"] if r["id"] == content["id"])
            if "setpoint" in content:
                if any(
                    self.mode(k)["state"] == "Active" for k in ("Pause", "Vacation")
                ):
                    raise HubRejected(400, [BLOCKED])
                target["setpoint"]["temperature"] = content["setpoint"]["temperature"]
            else:
                target["regulation"].update(content["regulation"])
        elif uri.startswith("/api/modes/"):
            mode = self.mode(content["type"])
            mode["state"] = content["state"]
            for key in ("startTime", "endTime"):
                if key in content:
                    mode[key] = content[key]
            self._status()
        elif uri == "/api/settings/temperature":
            for key, value in content.items():
                self.world["temperatureSettings"][key].update(value)
        elif uri.startswith("/api/onoffdevices/"):
            device = next(d for d in self.world["onOffDevices"] if d["id"] == content["id"])
            device["state"] = content["state"]
        return {}
