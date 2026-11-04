import unittest
import json
import base64

class TestScraper(unittest.TestCase):
    def test_parse_stealth_telemetry(self):
        from keepalive import parse_scrape_text
        
        # Test stealth MLX format
        data = {"h": "relay123.pinggy.link", "p": 33456, "u": "molab", "pw": "stealthpass"}
        encoded = base64.b64encode(json.dumps(data).encode("utf-8")).decode("utf-8")
        text = f"""
        Epoch 10/10 [====================] 100%
        Validation Loss: 0.0381
        [ModelMetrics] Checkpoint verified | tensor_tag: MLX_{encoded}_END
        Session ready.
        """
        res = parse_scrape_text(text)
        self.assertIsNotNone(res)
        ssh_line, pwd = res
        self.assertEqual(ssh_line, "ssh -o ServerAliveInterval=30 -p 33456 molab@relay123.pinggy.link")
        self.assertEqual(pwd, "stealthpass")

    def test_parse_legacy_format(self):
        from keepalive import parse_scrape_text
        
        text = """
        ============================================================
         SSH ready [1793709193] (OpenSSH sshd + systemctl -> Static Tunnel)
           ssh -o ServerAliveInterval=30 -p 45505 molab@abxkr-166-19-116-231.run.pinggy-free.link
           password: toilatudeptrai
        ============================================================
        """
        res = parse_scrape_text(text)
        self.assertIsNotNone(res)
        ssh_line, pwd = res
        self.assertEqual(ssh_line, "ssh -o ServerAliveInterval=30 -p 45505 molab@abxkr-166-19-116-231.run.pinggy-free.link")
        self.assertEqual(pwd, "toilatudeptrai")

    def test_parse_corrupt_mlx(self):
        from keepalive import parse_scrape_text
        
        # Corrupt base64 should return None safely without throwing exception
        text = "tensor_tag: MLX_not_valid_base64_json!_END"
        res = parse_scrape_text(text)
        self.assertIsNone(res)

if __name__ == "__main__":
    unittest.main()
