import subprocess
import time
import sys
import os

key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "molab_key")

cmd = [
    "ssh", "-4", "-N",
    "-L", "8000:127.0.0.1:8000",
    "-i", key_path,
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=NUL",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=3",
    "-o", "ExitOnForwardFailure=yes",
    "-p", "43803",
    "root@bore.pub"
]

print("Starting Molab Port Forwarder (8000 -> bore.pub:43803:8000)...", flush=True)
while True:
    try:
        p = subprocess.run(cmd)
    except Exception as e:
        print("Tunnel exception:", e, flush=True)
    time.sleep(2)
