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

def _ensure_py_deps():
    for py in dict.fromkeys([sys.executable, "/tmp/uv-venv/bin/python3"]):
        if not Path(py).exists():
            continue
        rc = subprocess.call(
            [py, "-c", "import pinggy, huggingface_hub, cryptography"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if rc != 0:
            subprocess.run(
                [
                    "/usr/local/bin/uv",
                    "pip",
                    "install",
                    "--python",
                    py,
                    "-q",
                    "pinggy",
                    "huggingface_hub",
                    "cryptography",
                ],
                check=False,
            )


_ensure_py_deps()
import pinggy

USER = "molab"
LOCAL_PORT = 2222
STATIC_PORT = 43803
STATIC_PASSWORD = ""
TOKEN = ""
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
    relay_port = int(local_port) + 1
    url_file = Path("/tmp/molab_pinggy_url.json")
    rot_file = Path("/tmp/molab_pinggy_rotate")
    worker = Path("/tmp/molab_pinggy_worker.py")

    if (
        worker.exists()
        and "# MSSH_V6" in worker.read_text(encoding="utf-8", errors="ignore")
        and _port_open(relay_port)
    ):
        old_ts = 0.0
        try:
            old_ts = float(json.loads(url_file.read_text(encoding="utf-8")).get("ts", 0))
        except Exception:
            pass
        rot_file.write_text(str(time.time()), encoding="utf-8")
        for _ in range(50):
            time.sleep(0.5)
            if url_file.exists():
                try:
                    data = json.loads(url_file.read_text(encoding="utf-8"))
                    if float(data.get("ts", 0)) > old_ts:
                        return data["host"], int(data["port"])
                except Exception:
                    pass

    subprocess.run(["pkill", "-9", "-f", "/tmp/molab_pinggy_"], check=False)
    time.sleep(1.0)
    if url_file.exists():
        url_file.unlink()
    if rot_file.exists():
        rot_file.unlink()

    child_File = Path("/tmp/molab_pinggy_tun.py")
    child_File.write_text(
        f"import sys; sys.path[:] = {sys.path!r}\n"
        "import json, os, time, pinggy\n"
        "from pathlib import Path\n"
        f"RELAY_PORT = {relay_port}\n"
        "URL_FILE = Path('/tmp/molab_pinggy_url.json')\n"
        "t = pinggy.start_tunnel(forwardto=f'127.0.0.1:{RELAY_PORT}', type='tcp')\n"
        "if getattr(t, 'urls', None):\n"
        "    u = t.urls[0].split('://', 1)[-1].rstrip('/')\n"
        "    h, p = u.rsplit(':', 1)\n"
        "    tmp = URL_FILE.with_suffix('.tmp')\n"
        "    tmp.write_text(json.dumps({'host': h, 'port': int(p), 'ts': time.time()}), encoding='utf-8')\n"
        "    os.replace(tmp, URL_FILE)\n"
        "    while True:\n"
        "        time.sleep(60)\n",
        encoding="utf-8",
    )

    worker_code = f"""# MSSH_V6
import sys
sys.path[:] = {sys.path!r}
import json, os, select, socket, struct, subprocess, threading, time
from pathlib import Path

SSH_PORT = {int(local_port)}
RELAY_PORT = {relay_port}
URL_FILE = Path("/tmp/molab_pinggy_url.json")
ROT_FILE = Path("/tmp/molab_pinggy_rotate")
CHILD_SCRIPT = "/tmp/molab_pinggy_tun.py"
SESSIONS = {{}}
SESS_LOCK = threading.Lock()


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError()
        buf.extend(chunk)
    return bytes(buf)


def _raw_pipe(a, b):
    try:
        while True:
            d = a.recv(16384)
            if not d:
                break
            b.sendall(d)
    except Exception:
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except Exception:
                pass


def _sshd_reader(sess):
    s = sess["ssh"]
    try:
        while True:
            data = s.recv(16384)
            if not data:
                break
            with sess["lock"]:
                sess["tx_buf"].extend(data)
                if len(sess["tx_buf"]) > 8 * 1024 * 1024:
                    drop = len(sess["tx_buf"]) - 4 * 1024 * 1024
                    del sess["tx_buf"][:drop]
                    sess["tx_base"] += drop
                c = sess["carrier"]
                if c is not None:
                    try:
                        c.sendall(struct.pack("!BI", 1, len(data)) + data)
                    except Exception:
                        pass
    except Exception:
        pass
    finally:
        with sess["lock"]:
            sess["closed"] = True
            c = sess["carrier"]
            if c is not None:
                try:
                    c.sendall(struct.pack("!BI", 3, 0))
                except Exception:
                    pass
        try:
            s.close()
        except Exception:
            pass
        time.sleep(30)
        with SESS_LOCK:
            SESSIONS.pop(sess["sid"], None)


def _handle_client(conn):
    try:
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.settimeout(10.0)
        r, _, _ = select.select([conn], [], [], 4.0)
        first = _recv_exact(conn, 4) if r else b""
        if r and not first:
            conn.close()
            return
        if first == b"MURL":
            try:
                conn.sendall(URL_FILE.read_bytes() if URL_FILE.exists() else b"{{}}")
            except Exception:
                pass
            conn.close()
            return
        if first != b"MSSH":
            s = socket.create_connection(("127.0.0.1", SSH_PORT), timeout=5)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(None)
            conn.settimeout(None)
            if first and first != b"RAW0":
                s.sendall(first)
            threading.Thread(target=_raw_pipe, args=(conn, s), daemon=True).start()
            _raw_pipe(s, conn)
            return

        conn.settimeout(15.0)
        sid, peer_rx = struct.unpack("!16sQ", _recv_exact(conn, 24))
        with SESS_LOCK:
            sess = SESSIONS.get(sid)
            if sess is None:
                s = socket.create_connection(("127.0.0.1", SSH_PORT), timeout=5)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.settimeout(None)
                sess = {{
                    "sid": sid,
                    "ssh": s,
                    "lock": threading.Lock(),
                    "tx_base": 0,
                    "tx_buf": bytearray(),
                    "rx_off": 0,
                    "carrier": None,
                    "closed": False,
                }}
                SESSIONS[sid] = sess
                threading.Thread(target=_sshd_reader, args=(sess,), daemon=True).start()

        with sess["lock"]:
            old_c = sess["carrier"]
            if old_c is not None and old_c is not conn:
                try:
                    old_c.close()
                except Exception:
                    pass
            conn.sendall(struct.pack("!4sQ", b"MSOK", sess["rx_off"]))
            drop = max(0, min(len(sess["tx_buf"]), peer_rx - sess["tx_base"]))
            if drop:
                del sess["tx_buf"][:drop]
                sess["tx_base"] += drop
            if sess["tx_buf"]:
                conn.sendall(struct.pack("!BI", 1, len(sess["tx_buf"])) + bytes(sess["tx_buf"]))
            if sess["closed"]:
                conn.sendall(struct.pack("!BI", 3, 0))
                conn.close()
                return
            conn.settimeout(30.0)
            sess["carrier"] = conn

        seen_url = URL_FILE.read_bytes() if URL_FILE.exists() else b""
        last_ka = time.monotonic()
        while True:
            if URL_FILE.exists():
                try:
                    cur_u = URL_FILE.read_bytes()
                    if cur_u and cur_u != seen_url:
                        seen_url = cur_u
                        with sess["lock"]:
                            if sess["carrier"] is conn:
                                conn.sendall(struct.pack("!BI", 5, len(cur_u)) + cur_u)
                except Exception:
                    pass
            r, _, _ = select.select([conn], [], [], 2.0)
            if not r:
                if time.monotonic() - last_ka >= 15.0:
                    with sess["lock"]:
                        if sess["carrier"] is not conn:
                            break
                        conn.sendall(struct.pack("!BI", 4, 0))
                    last_ka = time.monotonic()
                continue
            last_ka = time.monotonic()
            ftype, flen = struct.unpack("!BI", _recv_exact(conn, 5))
            body = _recv_exact(conn, flen) if flen else b""
            if ftype == 1:
                sess["ssh"].sendall(body)
                with sess["lock"]:
                    sess["rx_off"] += len(body)
                    if sess["carrier"] is conn:
                        conn.sendall(struct.pack("!BIQ", 2, 8, sess["rx_off"]))
            elif ftype == 2 and flen == 8:
                ack_off = struct.unpack("!Q", body)[0]
                with sess["lock"]:
                    drop = max(0, min(len(sess["tx_buf"]), ack_off - sess["tx_base"]))
                    if drop:
                        del sess["tx_buf"][:drop]
                        sess["tx_base"] += drop
            elif ftype == 3:
                try:
                    sess["ssh"].close()
                except Exception:
                    pass
                break
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _serve_relay():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", RELAY_PORT))
    srv.listen(64)
    while True:
        try:
            c, _ = srv.accept()
            threading.Thread(target=_handle_client, args=(c,), daemon=True).start()
        except Exception:
            time.sleep(0.5)


def _spawn_pinggy_proc():
    prev_ts = 0.0
    if URL_FILE.exists():
        try:
            prev_ts = float(json.loads(URL_FILE.read_text(encoding="utf-8")).get("ts", 0))
        except Exception:
            pass
    log_f = open("/tmp/molab_pinggy_tun.log", "ab")
    proc = subprocess.Popen(
        [sys.executable, CHILD_SCRIPT],
        stdin=subprocess.DEVNULL,
        stdout=log_f,
        stderr=log_f,
        start_new_session=True,
    )
    log_f.close()
    for _ in range(45):
        time.sleep(0.5)
        if URL_FILE.exists():
            try:
                cur_ts = float(json.loads(URL_FILE.read_text(encoding="utf-8")).get("ts", 0))
                if cur_ts > prev_ts:
                    return proc
            except Exception:
                pass
        if proc.poll() is not None:
            break
    try:
        proc.kill()
    except Exception:
        pass
    return None


threading.Thread(target=_serve_relay, daemon=True).start()
time.sleep(0.2)
cur_proc = _spawn_pinggy_proc()
last_rot = time.time()
old_procs = []

while True:
    time.sleep(2.0)
    now = time.time()
    kept = []
    for exp, op in old_procs:
        if now >= exp:
            try:
                op.kill()
            except Exception:
                pass
        else:
            kept.append((exp, op))
    old_procs = kept

    need_rot = ROT_FILE.exists() or cur_proc is None or cur_proc.poll() is not None or (now - last_rot >= 2100)
    if need_rot:
        forced = ROT_FILE.exists()
        if forced:
            try:
                ROT_FILE.unlink()
            except Exception:
                pass
        new_p = _spawn_pinggy_proc()
        if new_p is None:
            time.sleep(1.0)
            new_p = _spawn_pinggy_proc()
        if new_p is None and cur_proc is not None and (forced or cur_proc.poll() is not None or now - last_rot >= 3200):
            try:
                cur_proc.kill()
            except Exception:
                pass
            time.sleep(1.0)
            new_p = _spawn_pinggy_proc()
            cur_proc = None
        if new_p is not None:
            if cur_proc is not None:
                old_procs.append((now + 300, cur_proc))
            cur_proc = new_p
            last_rot = now
        elif cur_proc is not None and cur_proc.poll() is None:
            last_rot = now - 2040
"""
    worker.write_text(worker_code, encoding="utf-8")
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
