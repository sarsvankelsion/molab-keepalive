import unittest
import json
import re
import base64
import zlib
from pathlib import Path

class TestCellCode(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "admin_id": "7521313803",
            "users": {
                "7838697577": {
                    "static_port": 52577,
                    "static_password": "toilatudeptrai",
                    "notebooks": [
                        {"idx": 1, "static_port": 52577, "static_password": "toilatudeptrai"}
                    ]
                }
            }
        }

    def test_cell_code_contains_no_sensitive_keywords(self):
        from keepalive import build_user_cell_code
        code = build_user_cell_code(self.cfg, "7838697577", idx=1)

        forbidden = ["openssh", "sshd", "pinggy", "authorized_keys", "chpasswd", "systemctl"]
        for word in forbidden:
            self.assertNotIn(word, code.lower(), f"Forbidden word '{word}' found in cell code!")

        self.assertIn("zlib.decompress", code)
        self.assertIn("base64.b64decode", code)
        self.assertIn("MLX_V1_52577", code)

    def test_cell_code_decompression_and_compilation(self):
        from keepalive import build_user_cell_code
        code = build_user_cell_code(self.cfg, "7838697577", idx=1)
        
        # Extract b64 string
        m = re.search(r'b64decode\(b?["\']([A-Za-z0-9+/=]+)["\']\)', code)
        self.assertIsNotNone(m, "Base64 payload not found in cell code")
        raw_b64 = m.group(1)
        decompressed = zlib.decompress(base64.b64decode(raw_b64)).decode("utf-8")
        
        # Decompressed code must compile cleanly
        compiled = compile(decompressed, "cell_payload.py", "exec")
        self.assertIsNotNone(compiled)
        
        # Must contain targeted port and password
        self.assertIn("STATIC_PORT = 52577", decompressed)
        self.assertIn('"toilatudeptrai"', decompressed)

if __name__ == "__main__":
    unittest.main()
