import os
import socket
import subprocess
import sys
import time

key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "molab_key")
PORTS = [43803, 43804]


def is_port_open(host, port, timeout=2.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.close()
        return True
    except Exception:
        return False


print(
    "Starting Molab Port Forwarder with Active-Standby Failover (8000 -> bore.pub:43803 / 43804)...",
    flush=True,
)

while True:
    target_port = 43803
    if not is_port_open("bore.pub", 43803):
        if is_port_open("bore.pub", 43804):
            target_port = 43804
            print(
                f"⚡ Primary port 43803 offline -> Failover to Standby port {target_port}",
                flush=True,
            )
        else:
            time.sleep(2)
            continue

    cmd = [
        "ssh",
        "-4",
        "-N",
        "-L",
        "8000:127.0.0.1:8000",
        "-i",
        key_path,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=NUL",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "ExitOnForwardFailure=yes",
        "-p",
        str(target_port),
        "root@bore.pub",
    ]
    try:
        subprocess.run(cmd)
    except Exception as e:
        print("Tunnel exception:", e, flush=True)
    time.sleep(2)
