# Danfoss Link CC (MQTT)

Danfoss Link CC hub and room thermostats in Home Assistant over MQTT discovery, through the
Danfoss cloud relay used by the retired phone app. No CC firmware changes.

- Hub device: Pause, Holiday and At home switches, holiday window, pause/holiday temperatures,
  system status and alarms.
- One device per CC room: thermostat, temperature sensor, at-home and away setpoints.
- Verified writes: fresh read, one write, readback; uncertain writes are never retried.

See the **Documentation** tab for pairing and options.
