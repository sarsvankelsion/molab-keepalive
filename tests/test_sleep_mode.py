import unittest
import time

class TestSleepMode(unittest.TestCase):
    def setUp(self):
        import keepalive
        self._orig_save = keepalive.save_config
        keepalive.save_config = lambda *args, **kwargs: None

    def tearDown(self):
        import keepalive
        keepalive.save_config = self._orig_save

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

    def test_check_session_idle(self):
        from keepalive import check_session_idle

        st = {"last_busy": 1000.0}
        
        # When busy, reset last_busy and not idle
        should_idle, idle_sec = check_session_idle(st, idle_limit_minutes=60, now_mono=2000.0, is_busy=True)
        self.assertFalse(should_idle)
        self.assertEqual(st["last_busy"], 2000.0)

        # 30 mins later not busy -> not idle yet
        should_idle, idle_sec = check_session_idle(st, idle_limit_minutes=60, now_mono=3800.0, is_busy=False)
        self.assertFalse(should_idle)
        self.assertEqual(idle_sec, 1800.0)

        # 61 mins later not busy -> should idle!
        should_idle, idle_sec = check_session_idle(st, idle_limit_minutes=60, now_mono=5700.0, is_busy=False)
        self.assertTrue(should_idle)
        self.assertGreaterEqual(idle_sec, 3600.0)

if __name__ == "__main__":
    unittest.main()
