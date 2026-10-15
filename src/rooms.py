"""Sanitize room fields before publishing them to MQTT."""

from danfoss_client import numeric


def clean_rooms(rooms):
    result = []
    for r in rooms:
        name = r.get("name")
        name = (
            name[:64]
            if isinstance(name, str) and name.isprintable()
            else "Room " + str(r["id"])
        )
        point = r.get("setpoint", {})
        if not isinstance(point, dict):
            point = {}
        temp = r.get("ambientTemperature")
        target = point.get("temperature")
        low = r.get("minSetpoint", 6)
        high = r.get("maxSetpoint", 35)
        result.append(
            {
                "id": r["id"],
                "name": name,
                "temperature": temp if numeric(temp) and -50 <= temp <= 100 else None,
                "setpoint": target if numeric(target) and 6 <= target <= 35 else None,
                "min_temp": max(6, low) if numeric(low) else 6,
                "max_temp": min(35, high) if numeric(high) else 35,
            }
        )
    return result
