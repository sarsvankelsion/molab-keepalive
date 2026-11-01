"""OpenSSH (sshd thật) + giả lập systemd (systemctl) + mật khẩu & host key cố định trên molab."""

import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pinggy

USER = "molab"
LOCAL_PORT = 2222
STATIC_PORT = 43803
STATIC_PASSWORD = ""
TOKEN = "SiQz0RQJdDo"
STATIC_CFG_PATH = Path("/home/marimo/.ssh_static.json")
SYSTEMCTL_URL = "https://raw.githubusercontent.com/gdraheim/docker-systemctl-replacement/master/files/docker/systemctl3.py"


def _load_or_init_static():
    data = {}
    if STATIC_CFG_PATH.exists():
        try:
            data = json.loads(STATIC_CFG_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    port = int(STATIC_PORT or data.get("port") or 43803)
    password = str(STATIC_PASSWORD or data.get("password") or "").strip()
    if not password:
        password = secrets.token_urlsafe(16)
    if data.get("port") != port or data.get("password") != password:
        try:
            STATIC_CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATIC_CFG_PATH.write_text(
                json.dumps({"port": port, "password": password}, indent=2) + "\n",
                encoding="utf-8",
            )
            STATIC_CFG_PATH.chmod(0o600)
        except Exception:
            pass
    return port, password


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _write_deterministic_host_key(seed_text):
    key_priv = Path("/etc/ssh/ssh_host_ed25519_key")
    key_pub = Path("/etc/ssh/ssh_host_ed25519_key.pub")
    try:
        from cryptography.hazmat.primitives import serialization as s
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        seed = hashlib.sha256(f"molab-hostkey:{seed_text}".encode("utf-8")).digest()
        priv_obj = Ed25519PrivateKey.from_private_bytes(seed)
        priv_pem = priv_obj.private_bytes(s.Encoding.PEM, s.PrivateFormat.OpenSSH, s.NoEncryption())
        pub_openssh = priv_obj.public_key().public_bytes(s.Encoding.OpenSSH, s.PublicFormat.OpenSSH)
        key_priv.write_bytes(priv_pem)
        key_priv.chmod(0o600)
        key_pub.write_bytes(pub_openssh + b" root@molab\n")
        key_pub.chmod(0o644)
    except Exception:
        subprocess.run(["ssh-keygen", "-A"], check=True)


def _setup_sshd_and_systemd(password, port_seed):
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"

    if not Path("/usr/sbin/sshd").exists():
        subprocess.run(["apt-get", "update", "-qq"], env=env, check=True)
        subprocess.run(
            ["apt-get", "install", "-y", "-qq", "openssh-server"],
            env=env,
            check=True,
        )

    sysctl = Path("/usr/local/bin/systemctl")
    if not sysctl.exists():
        urllib.request.urlretrieve(SYSTEMCTL_URL, str(sysctl))
        sysctl.chmod(0o755)
        usr_sysctl = Path("/usr/bin/systemctl")
        if usr_sysctl.exists() or usr_sysctl.is_symlink():
            usr_sysctl.unlink()
        usr_sysctl.symlink_to(sysctl)

    for d in ("/run/sshd", "/etc/ssh/sshd_config.d", "/home/marimo/.ssh", "/root/.ssh"):
        Path(d).mkdir(parents=True, exist_ok=True)
    for d in ("/home/marimo", "/home/marimo/.ssh", "/root/.ssh"):
        Path(d).chmod(0o700)

    subprocess.run(
        "useradd -o -u 0 -g 0 -M -d /home/marimo -s /bin/bash molab 2>/dev/null "
        "|| usermod -o -u 0 -g 0 -d /home/marimo -s /bin/bash molab",
        shell=True,
        check=True,
    )
    subprocess.run(
        ["chpasswd"],
        input=f"molab:{password}\nroot:{password}\n".encode(),
        check=True,
    )

    path_line = f"PATH={os.environ.get('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')}\n"
    for env_file in (Path("/home/marimo/.ssh/environment"), Path("/root/.ssh/environment")):
        env_file.write_text(path_line, encoding="utf-8")
        env_file.chmod(0o600)

    _write_deterministic_host_key(str(port_seed))

    Path("/etc/ssh/sshd_config.d/molab.conf").write_text(
        f"Port {LOCAL_PORT}\n"
        "ListenAddress 127.0.0.1\n"
        "HostKey /etc/ssh/ssh_host_ed25519_key\n"
        "PermitRootLogin yes\n"
        "PasswordAuthentication yes\n"
        "PermitUserEnvironment yes\n"
        "AllowTcpForwarding yes\n"
        "GatewayPorts yes\n"
        "ClientAliveInterval 30\n"
        "ClientAliveCountMax 6\n"
        "UsePAM no\n",
        encoding="utf-8",
    )
    if not _port_open(LOCAL_PORT):
        subprocess.run([str(sysctl), "restart", "ssh"], check=False)
        if not _port_open(LOCAL_PORT):
            subprocess.run(["/usr/sbin/sshd", "-p", str(LOCAL_PORT)], check=True)


def _start_pinggy_subprocess(local_port, token):
    subprocess.run(["pkill", "-9", "-f", "/tmp/molab_pinggy_worker.py"], check=False)
    time.sleep(1.0)
    url_file = Path("/tmp/molab_pinggy_url.json")
    if url_file.exists():
        url_file.unlink()
    worker = Path("/tmp/molab_pinggy_worker.py")
    worker.write_text(
        f"import sys; sys.path[:] = {sys.path!r}\n"
        "import json, time, pinggy\n"
        "from pathlib import Path\n"
        f"for use_tok in ([{bool(token)}, False] if {bool(token)} else [False]):\n"
        "    try:\n"
        f"        kw = {{'forwardto': '127.0.0.1:{local_port}', 'type': 'tcp'}}\n"
        "        if use_tok:\n"
        f"            kw['token'] = {token!r}\n"
        "            kw['force'] = True\n"
        "        t = pinggy.start_tunnel(**kw)\n"
        "        if getattr(t, 'urls', None):\n"
        "            u = t.urls[0].split('://', 1)[-1].rstrip('/')\n"
        "            h, p = u.rsplit(':', 1)\n"
        "            Path('/tmp/molab_pinggy_url.json').write_text(json.dumps({'host': h, 'port': int(p)}), encoding='utf-8')\n"
        "            while True:\n"
        "                time.sleep(60)\n"
        "    except Exception as e:\n"
        "        Path('/tmp/molab_pinggy_err.txt').write_text(str(e), encoding='utf-8')\n"
        "        time.sleep(1)\n",
        encoding="utf-8",
    )
    subprocess.Popen(
        [sys.executable, str(worker)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    for _ in range(40):
        time.sleep(0.5)
        if url_file.exists():
            try:
                data = json.loads(url_file.read_text(encoding="utf-8"))
                return data["host"], int(data["port"])
            except Exception:
                pass
    raise RuntimeError("Không tạo được tunnel Pinggy")


def start():
    desired_port, password = _load_or_init_static()
    _setup_sshd_and_systemd(password, desired_port)
    host, public_port = _start_pinggy_subprocess(LOCAL_PORT, TOKEN)
    return host, public_port, password


def main():
    host, public_port, password = start()
    print("=" * 60)
    print(f" SSH ready [{int(time.time())}] (OpenSSH sshd + systemctl -> Static Tunnel)")
    print(f"   ssh -o ServerAliveInterval=30 -p {public_port} {USER}@{host}")
    print(f"   password: {password}")
    print("=" * 60)


main()
