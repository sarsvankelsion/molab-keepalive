import unittest
import json
import base64

class TestStealthProtocol(unittest.TestCase):
    def test_stealth_payload_codec(self):
        from molab_ssh_pinggy import encode_stealth_telemetry, decode_stealth_telemetry
        
        host = "test-host.pinggy.link"
        port = 45505
        user = "molab"
        pwd = "secretpassword"
        
        output_line = encode_stealth_telemetry(host, port, user, pwd, ts=1700000000)
        self.assertNotIn("ssh", output_line.lower())
        self.assertNotIn("password", output_line.lower())
        self.assertNotIn("pinggy", output_line.lower())
        self.assertIn("[ModelMetrics]", output_line)
        self.assertIn("tensor_tag: MLX_", output_line)
        
        decoded = decode_stealth_telemetry(output_line)
        self.assertIsNotNone(decoded)
        h, p, u, pw = decoded
        self.assertEqual(h, host)
        self.assertEqual(p, port)
        self.assertEqual(u, user)
        self.assertEqual(pw, pwd)

if __name__ == "__main__":
    unittest.main()
