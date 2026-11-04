import unittest
import time

class TestSleepMode(unittest.TestCase):
    def test_parse_sleep_duration(self):
        from keepalive import parse_sleep_duration

        self.assertEqual(parse_sleep_duration(""), 8.0)
        self.assertEqual(parse_sleep_duration("4"), 4.0)
        self.assertEqual(parse_sleep_duration("2.5"), 2.5)
        self.assertEqual(parse_sleep_duration("0.5"), 0.5)
        self.assertEqual(parse_sleep_duration("72"), 72.0)

        # Clamping / validation
        self.assertEqual(parse_sleep_duration("100"), 72.0)  # max 72h
        self.assertEqual(parse_sleep_duration("0.1"), 0.5)   # min 0.5h
        self.assertEqual(parse_sleep_duration("abc"), 8.0)   # fallback default

    def test_is_user_sleeping(self):
        from keepalive import is_user_sleeping

        now = 1700000000.0
        # No sleep
        self.assertFalse(is_user_sleeping({}, now=now)[0])
        self.assertFalse(is_user_sleeping({"sleep_until": 0}, now=now)[0])
        self.assertFalse(is_user_sleeping({"sleep_until": now - 100}, now=now)[0])

        # Active sleep
        sleeping, rem = is_user_sleeping({"sleep_until": now + 3600}, now=now)
        self.assertTrue(sleeping)
        self.assertAlmostEqual(rem, 3600, delta=1)

    def test_vps_status_card_shows_sleep_mode(self):
        from keepalive import get_vps_status_card
        cfg = {
            "session_restart_minutes": 725,
            "users": {
                "12345": {
                    "notebooks": [{"idx": 1, "static_port": 25000}],
                    "sleep_until": time.time() + 7200,
                }
            }
        }
        card = get_vps_status_card(cfg, None, uid="12345", idx=1)
        self.assertIn("Đang ngủ", card)
        self.assertIn("/wake", card)

if __name__ == "__main__":
    unittest.main()
