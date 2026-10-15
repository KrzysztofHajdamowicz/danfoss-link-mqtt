import unittest
from rooms import clean_rooms


class RoomTests(unittest.TestCase):
    def test_missing_temperature_sentinels_and_name_fallback(self):
        result = clean_rooms(
            [
                {
                    "id": 9,
                    "name": "bad\nname",
                    "ambientTemperature": 777.0,
                    "setpoint": {"temperature": 777.0},
                }
            ]
        )[0]
        self.assertIsNone(result["temperature"])
        self.assertIsNone(result["setpoint"])
        self.assertEqual(result["name"], "Room 9")
