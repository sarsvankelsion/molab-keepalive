"""OpenSSH (sshd thật) + giả lập systemd (systemctl) + Pinggy TCP tunnel trên molab."""

import os
import secrets
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pinggy

USER = "molab"
PASSWORD = secrets.token_urlsafe(18)
LOCAL_PORT = 2222
TOKEN = "SiQz0RQJdDo"
SYSTEMCTL_URL = "https://raw.githubusercontent.com/gdraheim/docker-systemctl-replacement/master/files/docker/systemctl3.py"
_tunnel = None


def _port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _setup_sshd_and_systemd():
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
        input=f"molab:{PASSWORD}\nroot:{PASSWORD}\n".encode(),
        check=True,
    )

    path_line = f"PATH={os.environ.get('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')}\n"
    for env_file in (Path("/home/marimo/.ssh/environment"), Path("/root/.ssh/environment")):
        env_file.write_text(path_line, encoding="utf-8")
        env_file.chmod(0o600)

    Path("/etc/ssh/sshd_config.d/molab.conf").write_text(
        f"Port {LOCAL_PORT}\n"
        "ListenAddress 127.0.0.1\n"
        "PermitRootLogin yes\n"
        "PasswordAuthentication yes\n"
        "PermitUserEnvironment yes\n"
        "AllowTcpForwarding yes\n"
        "GatewayPorts yes\n"
        "UsePAM no\n",
        encoding="utf-8",
    )
    subprocess.run(["ssh-keygen", "-A"], check=True)
    if not _port_open(LOCAL_PORT):
        subprocess.run([str(sysctl), "restart", "ssh"], check=False)
        if not _port_open(LOCAL_PORT):
            subprocess.run(["/usr/sbin/sshd", "-p", str(LOCAL_PORT)], check=True)


def _start_tunnel(local_port):
    last_error = None
    for attempt in range(1, 6):
        try:
            kwargs = {"forwardto": f"127.0.0.1:{local_port}", "type": "tcp"}
            if TOKEN:
                kwargs["token"] = TOKEN
                kwargs["force"] = True
            tunnel = pinggy.start_tunnel(**kwargs)
            if tunnel.urls:
                url = tunnel.urls[0]
                host_port = url.split("://", 1)[-1].rstrip("/")
                host, port = host_port.rsplit(":", 1)
                return tunnel, host, int(port)
            last_error = "tunnel.urls rỗng"
        except Exception as exc:
            last_error = repr(exc)
        time.sleep(5 * attempt)
    raise RuntimeError(f"Không tạo được tunnel sau 5 lần: {last_error}")


def start():
    global _tunnel
    if _tunnel is not None:
        try:
            _tunnel.stop()
        except Exception:
            pass
    _setup_sshd_and_systemd()
    _tunnel, host, public_port = _start_tunnel(LOCAL_PORT)
    return host, public_port


def main():
    host, public_port = start()
    print("=" * 60)
    print(" SSH ready (OpenSSH sshd + systemctl -> pinggy)")
    print(f"   ssh -o ServerAliveInterval=30 -p {public_port} {USER}@{host}")
    print(f"   password: {PASSWORD}")
    print("=" * 60)


main()
