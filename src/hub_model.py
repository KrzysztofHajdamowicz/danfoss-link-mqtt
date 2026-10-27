"""Pure /api/world parsing and Home Assistant device-discovery payloads."""

import re
from datetime import datetime
from danfoss_client import numeric
from danfoss_readonly import ProtocolError
from mqtt_bridge import clean_world

VERSION = "0.3.0"
ORIGIN = {
    "name": "danfoss-link-mqtt",
    "sw_version": VERSION,
    "support_url": "https://github.com/KrzysztofHajdamowicz/danfoss-link-mqtt",
}
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MODE_KEYS = {"Pause": "pause", "Vacation": "vacation", "AtHome": "athome"}
SETTINGS = {
    "pause": ("pauseTemperature", (4, 10)),
    "vacation": ("vacationTemperature", (5, 35)),
}
SCHEDULE_TIME = re.compile(r"(?:([01])\.)?([0-2][0-9]):([0-5][0-9]):[0-5][0-9]")
CC_TIME = re.compile(
    r"(20[0-9]{2}-[01][0-9]-[0-3][0-9]T[0-2][0-9]:[0-5][0-9]:[0-5][0-9])(?:\.[0-9]{1,7})?"
)
SWITCHES = (
    ("pause", "Pause", "mdi:pause-circle-outline"),
    ("vacation", "Holiday", "mdi:airplane"),
    ("athome", "At home", "mdi:home-account"),
)


def _text(value, limit):
    if isinstance(value, str) and 1 <= len(value) <= limit and value.isprintable():
        return value
    return None


def _setpoint(value):
    return float(value) if numeric(value) and 5 <= value <= 35 else None


def _schedule_time(value):
    match = SCHEDULE_TIME.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None
    day, hours, minutes = match.groups()
    if day == "1":
        return "24:00" if (hours, minutes) == ("00", "00") else None
    return hours + ":" + minutes if int(hours) < 24 else None


def _schedule(raw):
    if not isinstance(raw, dict):
        return None
    schedule = {}
    for day in DAYS:
        entries = raw.get(day)
        if not isinstance(entries, list):
            continue
        schedule[day] = [
            {"state": state, "start": start, "end": end}
            for entry in entries[:48]
            if isinstance(entry, dict)
            and (state := _text(entry.get("state"), 32))
            and (start := _schedule_time(entry.get("startTimeFromMidnight")))
            and (end := _schedule_time(entry.get("endTimeFromMidnight")))
        ]
    return schedule


def _cc_time(value):
    match = CC_TIME.fullmatch(value) if isinstance(value, str) else None
    return match.group(1) if match else None


def _modes(raw):
    modes = {key: None for key in MODE_KEYS.values()}
    for mode in raw[:16] if isinstance(raw, list) else ():
        if not isinstance(mode, dict):
            continue
        key = MODE_KEYS.get(mode.get("type"))
        state = _text(mode.get("state"), 32)
        if key is None or state is None:
            continue
        modes[key] = {
            "active": state == "Active",
            "state": state,
            "start": _cc_time(mode.get("startTime")),
            "end": _cc_time(mode.get("endTime")),
        }
    return modes


def _settings(raw):
    settings = {}
    for key, (field, (low, high)) in SETTINGS.items():
        value = raw.get(field) if isinstance(raw, dict) else None
        point = value.get("setpoint") if isinstance(value, dict) else None
        if not numeric(point) or not -50 <= point <= 100:
            settings[key] = None
            continue
        if numeric(value.get("minSetpoint")):
            low = max(low, value["minSetpoint"])
        if numeric(value.get("maxSetpoint")):
            high = min(high, value["maxSetpoint"])
        settings[key] = (
            {"setpoint": float(point), "min": float(low), "max": float(high)}
            if low <= high
            else None
        )
    return settings


def _onoff(raw):
    devices = {}
    for device in raw[:64] if isinstance(raw, list) else ():
        if (
            not isinstance(device, dict)
            or type(device.get("id")) is not int
            or device["id"] < 0
            or device["id"] in devices
        ):
            continue
        devices[device["id"]] = {
            "id": device["id"],
            "name": _text(device.get("name"), 64)
            or "On/off device " + str(device["id"]),
            "on": {"On": True, "Off": False}.get(device.get("state")),
        }
    return list(devices.values())


def parse_world(world):
    """Normalize an untrusted world response; core fields must be complete."""
    snapshot = clean_world(world)
    raw_rooms = {}
    for raw in world["allRooms"]:
        if raw["id"] in raw_rooms:
            raise ProtocolError("Duplicate room identifier")
        raw_rooms[raw["id"]] = raw
    for room in snapshot["rooms"]:
        raw = raw_rooms[room["id"]]
        regulation = raw.get("regulation")
        if not isinstance(regulation, dict):
            regulation = {}
        kind = regulation.get("type")
        kind = kind if kind in ("Manual", "Schedule") else None
        room.update(
            mode="auto" if kind == "Schedule" else "heat",
            regulation=kind,
            manual_setpoint=_setpoint(regulation.get("manualSetpoint")),
            athome_setpoint=_setpoint(regulation.get("atHomeSetpoint")),
            away_setpoint=_setpoint(regulation.get("awaySetpoint")),
            zone=_text(raw.get("zone"), 64),
            room_state=_text(raw.get("state"), 32),
            has_temperature_sensor=raw.get("hasTemperatureSensor")
            if type(raw.get("hasTemperatureSensor")) is bool
            else None,
            schedule=_schedule(raw.get("schedule")),
        )
    info = world.get("systemInfo")
    if not isinstance(info, dict):
        info = {}
    snapshot.update(
        modes=_modes(world.get("modes")),
        settings=_settings(world.get("temperatureSettings")),
        system={
            "name": _text(info.get("name"), 64),
            "serial": _text(info.get("serialNo"), 32),
            "version": _text(info.get("version"), 32),
        },
        onoff=_onoff(world.get("onOffDevices")),
    )
    return snapshot


def setpoints_blocked(snapshot):
    """Pause and Vacation pin rooms to frost protection and reject setpoint writes."""
    return any(
        (snapshot["modes"].get(key) or {}).get("active") for key in ("pause", "vacation")
    )


def cc_to_iso(local, tz):
    if local is None:
        return None
    return datetime.fromisoformat(local).replace(tzinfo=tz).isoformat()


def aware_to_cc(moment, tz):
    return moment.astimezone(tz).replace(tzinfo=None).isoformat(timespec="seconds")


def _switch(mode):
    return None if mode is None else ("ON" if mode["active"] else "OFF")


def hub_state(snapshot, tz, pending):
    """Retained hub JSON; inactive holiday times show the locally pending window."""
    modes, settings = snapshot["modes"], snapshot["settings"]
    vacation = modes["vacation"] or {}
    times = {}
    for key in ("start", "end"):
        if vacation.get("active") or pending.get(key) is None:
            times[key] = cc_to_iso(vacation.get(key), tz)
        else:
            times[key] = pending[key].isoformat()
    return {
        "pause": _switch(modes["pause"]),
        "vacation": _switch(modes["vacation"]),
        "athome": _switch(modes["athome"]),
        "vacation_start": times["start"],
        "vacation_end": times["end"],
        "pause_temperature": (settings["pause"] or {}).get("setpoint"),
        "vacation_temperature": (settings["vacation"] or {}).get("setpoint"),
    }


def room_state(room):
    keys = ("temperature", "setpoint", "mode", "action")
    return {k: room.get(k) for k in keys + ("athome_setpoint", "away_setpoint")}


def room_attributes(room):
    return {
        "zone": room["zone"],
        "regulation": room["regulation"],
        "room_state": room["room_state"],
        "has_temperature_sensor": room["has_temperature_sensor"],
        "manual_setpoint": room["manual_setpoint"],
        "schedule": room["schedule"],
    }


def _value(key):
    return "{{ value_json.%s if value_json.%s is not none else 'None' }}" % (key, key)


def _root(config, controller_id, device):
    base = config.prefix + "/" + controller_id
    return {
        "device": device,
        "origin": dict(ORIGIN),
        "availability": [{"topic": base + "/availability"}],
        "qos": 0,
        "components": {},
    }


def hub_discovery(config, controller_id, snapshot):
    uid = "danfoss_link_" + controller_id
    base = config.prefix + "/" + controller_id
    state = base + "/hub/state"
    writes = config.allow_writes
    expire = max(180, config.poll_interval * 3)
    device = {
        "identifiers": [uid],
        "name": "Danfoss Link CC",
        "manufacturer": "Danfoss",
        "model": "Link CC",
    }
    if snapshot["system"]["version"]:
        device["sw_version"] = snapshot["system"]["version"]
    if snapshot["system"]["serial"]:
        device["serial_number"] = snapshot["system"]["serial"]
    payload = _root(config, controller_id, device)
    components = payload["components"]
    # Unique IDs of the first three match the flat bridge, so entities migrate; their
    # keys differ from its object IDs so Home Assistant sees new discovery hashes and
    # clearing the flat topics afterwards does not conflict.
    components["status"] = {
        "platform": "sensor",
        "name": "System status",
        "unique_id": uid + "_system_state",
        "icon": "mdi:home-thermometer",
        "state_topic": base + "/system/state",
        "value_template": "{{ value_json.current_state }}",
        "expire_after": expire,
    }
    components["problem"] = {
        "platform": "binary_sensor",
        "name": "Alarm",
        "unique_id": uid + "_alarm",
        "device_class": "problem",
        "state_topic": base + "/alarms/state",
        "value_template": "{{ 'ON' if value_json.count > 0 else 'OFF' }}",
        "json_attributes_topic": base + "/alarms/attributes",
        "expire_after": expire,
    }
    components["alarms"] = {
        "platform": "sensor",
        "name": "Alarm count",
        "unique_id": uid + "_alarm_count",
        "icon": "mdi:alert-circle-outline",
        "entity_category": "diagnostic",
        "state_topic": base + "/alarms/state",
        "value_template": "{{ value_json.count }}",
        "expire_after": expire,
    }
    for key, name, icon in SWITCHES:
        component = {
            "name": name,
            "unique_id": uid + "_" + key,
            "icon": icon,
            "state_topic": state,
            "value_template": _value(key),
            "payload_on": "ON",
            "payload_off": "OFF",
        }
        if writes:
            component.update(
                platform="switch",
                command_topic=base + "/hub/" + key + "/set",
                state_on="ON",
                state_off="OFF",
                optimistic=False,
                retain=False,
            )
        else:
            component["platform"] = "binary_sensor"
        components[key] = component
    for edge, name, icon in (
        ("start", "Holiday start", "mdi:calendar-start"),
        ("end", "Holiday end", "mdi:calendar-end"),
    ):
        key = "vacation_" + edge
        component = {
            "name": name,
            "unique_id": uid + "_" + key,
            "icon": icon,
            "state_topic": state,
            "value_template": _value(key),
        }
        if writes:
            component.update(
                platform="datetime",
                command_topic=base + "/hub/" + key + "/set",
                retain=False,
            )
        else:
            component.update(platform="sensor", device_class="timestamp")
        components[key] = component
    for kind, name in (("pause", "Pause temperature"), ("vacation", "Holiday temperature")):
        key = kind + "_temperature"
        limits = snapshot["settings"][kind] or {}
        low, high = SETTINGS[kind][1]
        component = {
            "name": name,
            "unique_id": uid + "_" + key,
            "device_class": "temperature",
            "unit_of_measurement": "°C",
            "state_topic": state,
            "value_template": _value(key),
        }
        if writes:
            component.update(
                platform="number",
                entity_category="config",
                command_topic=base + "/hub/" + key + "/set",
                min=limits.get("min", low),
                max=limits.get("max", high),
                step=0.5,
                mode="box",
                optimistic=False,
                retain=False,
            )
        else:
            component.update(platform="sensor", entity_category="diagnostic")
        components[key] = component
    if writes:
        components["last_command"] = {
            "platform": "sensor",
            "name": "Last command",
            "unique_id": uid + "_last_command",
            "icon": "mdi:console-line",
            "entity_category": "diagnostic",
            "state_topic": base + "/hub/last_command",
            "value_template": "{{ value_json.result }}",
            "json_attributes_topic": base + "/hub/last_command",
        }
    for device_ in snapshot["onoff"]:
        key = "onoff_" + str(device_["id"])
        component = {
            "name": device_["name"],
            "unique_id": uid + "_" + key,
            "state_topic": base + "/onoff/" + str(device_["id"]) + "/state",
            "payload_on": "ON",
            "payload_off": "OFF",
        }
        if writes:
            component.update(
                platform="switch",
                command_topic=base + "/onoff/" + str(device_["id"]) + "/set",
                state_on="ON",
                state_off="OFF",
                optimistic=False,
                retain=False,
            )
        else:
            component["platform"] = "binary_sensor"
        components[key] = component
    return config.discovery_prefix + "/device/" + uid + "/config", payload


def room_discovery(config, controller_id, room, *, suggest_area=True):
    uid = "danfoss_link_" + controller_id
    room_uid = uid + "_room_" + str(room["id"])
    topic = config.prefix + "/" + controller_id + "/room/" + str(room["id"])
    state = topic + "/state"
    device = {
        "identifiers": [room_uid],
        "name": "Danfoss Link – " + room["name"],
        "manufacturer": "Danfoss",
        "model": "Link room",
        "via_device": uid,
    }
    if suggest_area:
        device["suggested_area"] = room["name"]
    payload = _root(config, controller_id, device)
    climate = {
        "platform": "climate",
        "name": None,
        # Same unique ID as the flat bridge's climate entity.
        "unique_id": room_uid,
        "current_temperature_topic": state,
        "current_temperature_template": _value("temperature"),
        "temperature_state_topic": state,
        "temperature_state_template": _value("setpoint"),
        "mode_state_topic": state,
        "mode_state_template": "{{ value_json.mode }}",
        # Manual/Schedule is zone-governed on the CC; shown but not commandable.
        "modes": ["heat", "auto"],
        "action_topic": state,
        "action_template": _value("action"),
        "json_attributes_topic": topic + "/attributes",
        "temperature_unit": "C",
        "precision": 0.1,
        "temp_step": 0.5,
        "min_temp": room["min_temp"],
        "max_temp": room["max_temp"],
        "optimistic": False,
        "retain": False,
    }
    if config.allow_writes:
        climate["temperature_command_topic"] = topic + "/temperature/set"
    components = payload["components"]
    components["climate"] = climate
    components["temperature"] = {
        "platform": "sensor",
        "name": "Temperature",
        "unique_id": room_uid + "_temperature",
        "device_class": "temperature",
        "state_class": "measurement",
        "unit_of_measurement": "°C",
        "suggested_display_precision": 1,
        "state_topic": state,
        "value_template": _value("temperature"),
        "expire_after": max(180, config.poll_interval * 3),
    }
    for key, name in (
        ("athome_setpoint", "At home setpoint"),
        ("away_setpoint", "Away setpoint"),
    ):
        component = {
            "name": name,
            "unique_id": room_uid + "_" + key,
            "device_class": "temperature",
            "unit_of_measurement": "°C",
            "state_topic": state,
            "value_template": _value(key),
        }
        if config.allow_writes:
            component.update(
                platform="number",
                entity_category="config",
                command_topic=topic + "/" + key + "/set",
                min=room["min_temp"],
                max=room["max_temp"],
                step=0.5,
                mode="box",
                optimistic=False,
                retain=False,
            )
        else:
            component.update(platform="sensor", entity_category="diagnostic")
        components[key] = component
    return (
        config.discovery_prefix + "/device/" + room_uid + "/config",
        payload,
    )
