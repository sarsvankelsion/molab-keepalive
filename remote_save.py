#!/usr/bin/env python3
"""Đồng bộ /home/marimo <-> Hugging Face dataset (Private) + tự nhớ gói apt/pip + gửi file Telegram.

Trong SSH:
  save              # đẩy ngay /home/marimo lên Hugging Face
  save <file>       # gửi <file> qua Telegram + đẩy /home/marimo lên Hugging Face
  save --restore    # tải dữ liệu từ Hugging Face về /home/marimo & cài lại gói apt/pip
"""

import fnmatch
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from huggingface_hub import HfApi, snapshot_download

CFG_PATH = Path("/root/.molab_sync_cfg.json")
WORK_DIR = Path("/home/marimo")
PKG_FILE = WORK_DIR / ".packages.json"
LOG_DIR = WORK_DIR / ".logs"
SYSTEMD_BACKUP = WORK_DIR / ".systemd"
SETUP_SH = WORK_DIR / "setup.sh"
MARKER = Path("/tmp/.molab_hf_restored")
PID_FILE = Path("/tmp/.molab_hf_daemon.pid")
APT_HOOK = Path("/etc/apt/apt.conf.d/99molab-save")
PIP_WRAP = Path("/tmp/uv-venv/bin/pip")
PIP3_WRAP = Path("/tmp/uv-venv/bin/pip3")
DEPLOY_BIN = Path("/usr/local/bin/deploy")
DOCKERD_BIN = Path("/usr/local/bin/molab-dockerd")
DOCKER_CLI_BIN = Path("/usr/local/bin/docker")
WINGS_BIN = Path("/usr/local/bin/wings")
PTERO_BACKUP = WORK_DIR / ".pterodactyl"
MAX_SYNC_FILE_BYTES = 100 * 1024 * 1024
IGNORE = [
    ".cache",
    ".cache/**",
    ".local",
    ".local/**",
    ".config",
    ".config/**",
    ".ipython",
    ".ipython/**",
    ".nv",
    ".nv/**",
    ".triton",
    ".triton/**",
    ".npm",
    ".npm/**",
    ".bash*",
    ".profile",
    ".wget-hsts",
    ".lesshst",
    ".git",
    "**/.git/**",
    "**/__pycache__/**",
    "models",
    "models/**",
    "**/models/**",
    "**/build/**",
    "**/CMakeFiles/**",
    "*.gguf",
    "**/*.gguf",
    "*.safetensors",
    "**/*.safetensors",
    "*.bin",
    "**/*.bin",
    "*.pt",
    "**/*.pt",
    "*.pth",
    "**/*.pth",
    "*.onnx",
    "**/*.onnx",
    "*.deb",
    "**/*.deb",
    "*.log",
    "**/*.log",
    "**/*.o",
    "**/*.a",
    "**/*.so",
    "**/*.so.*",
]

PIP_WRAP_CODE = """#!/usr/local/bin/python3
import subprocess, sys
rc = subprocess.call(["/usr/local/bin/uv", "pip", *sys.argv[1:], "--python", "/tmp/uv-venv/bin/python3"])
if rc != 0:
    rc = subprocess.call(["/usr/local/bin/pip", *sys.argv[1:]])
if rc == 0 and "install" in sys.argv[1:]:
    subprocess.call(["/usr/local/bin/save", "--record-pip", *sys.argv[1:]])
sys.exit(rc)
"""

DOCKER_SHIM_CODE = r"""#!/usr/bin/env python3
import datetime, hashlib, http.server, json, os, pty, re, select, shutil, signal, socket, socketserver, struct, subprocess, threading, time, urllib.parse
from pathlib import Path

SOCK_PATH = "/var/run/docker.sock"
STATE_DIR = Path("/var/lib/molab-docker")
STATE_DIR.mkdir(parents=True, exist_ok=True)
CONTAINERS = {}
EVENTS = []
LOCK = threading.Lock()

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

def push_event(cid, action, name=""):
    ev = {
        "status": action,
        "id": cid,
        "from": "molab-host-shim",
        "Type": "container",
        "Action": action,
        "Actor": {"ID": cid, "Attributes": {"name": name}},
        "scope": "local",
        "time": int(time.time()),
        "timeNano": int(time.time_ns()),
    }
    with LOCK:
        EVENTS.append(ev)
        if len(EVENTS) > 200:
            del EVENTS[:100]

def resolve_cid(prefix):
    prefix = urllib.parse.unquote(prefix).strip("/")
    with LOCK:
        if prefix in CONTAINERS:
            return prefix
        for cid, c in CONTAINERS.items():
            if cid.startswith(prefix) or c["name"].lstrip("/") == prefix.lstrip("/"):
                return cid
    return None

def start_container_proc(c):
    if c.get("running") and c.get("proc") and c["proc"].poll() is None:
        return True
    binds = (c["spec"].get("HostConfig") or {}).get("Binds") or []
    vol_dir = f"/var/lib/pterodactyl/volumes/{c['name'].lstrip('/')}"
    for b in binds:
        parts = b.split(":")
        if len(parts) >= 2 and parts[1].rstrip("/") == "/home/container":
            vol_dir = parts[0]
            break
    os.makedirs(vol_dir, exist_ok=True)
    try:
        if os.path.islink("/home/container") or not os.path.exists("/home/container"):
            if os.path.islink("/home/container"):
                os.unlink("/home/container")
            os.symlink(vol_dir, "/home/container")
    except Exception:
        pass

    env = os.environ.copy()
    env["HOME"] = vol_dir
    env["USER"] = "container"
    env["PATH"] = "/tmp/uv-venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    for item in (c["spec"].get("Env") or []):
        if "=" in item:
            k, v = item.split("=", 1)
            env[k] = v.replace("/home/container", vol_dir) if k != "STARTUP" else v

    cmd_list = list(c["spec"].get("Entrypoint") or []) + list(c["spec"].get("Cmd") or [])
    if not cmd_list and env.get("STARTUP"):
        raw_startup = env["STARTUP"]
        for k, v in env.items():
            raw_startup = raw_startup.replace(f"{{{{{k}}}}}", v).replace(f"${{{k}}}", v)
        cmd_str = raw_startup.replace("/home/container", vol_dir)
    elif cmd_list:
        cmd_str = " ".join(cmd_list).replace("/home/container", vol_dir)
    else:
        cmd_str = "bash"

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        ["/bin/bash", "-lc", cmd_str],
        cwd=vol_dir,
        env=env,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        preexec_fn=os.setsid,
        close_fds=True,
    )
    os.close(slave_fd)
    c["proc"] = proc
    c["pty_fd"] = master_fd
    c["pid"] = proc.pid
    c["running"] = True
    c["started_at"] = now_iso()
    c["exit_code"] = 0
    push_event(c["id"], "start", c["name"])

    def _reader():
        try:
            while True:
                r, _, _ = select.select([master_fd], [], [], 1.0)
                if master_fd in r:
                    chunk = os.read(master_fd, 4096)
                    if not chunk:
                        break
                    with c["lock"]:
                        c["logs"].extend(chunk)
                        if len(c["logs"]) > 524288:
                            del c["logs"][:262144]
                        for q in list(c["subscribers"]):
                            try:
                                q.append(chunk)
                            except Exception:
                                pass
                if proc.poll() is not None:
                    break
        except Exception:
            pass
        finally:
            rc = proc.wait()
            c["running"] = False
            c["exit_code"] = rc if rc is not None else 0
            c["finished_at"] = now_iso()
            try:
                os.close(master_fd)
            except Exception:
                pass
            push_event(c["id"], "die", c["name"])

    threading.Thread(target=_reader, daemon=True).start()
    return True

def stop_container_proc(c, sig=signal.SIGTERM):
    proc = c.get("proc")
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        for _ in range(25):
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
    c["running"] = False
    push_event(c["id"], "stop", c["name"])

class DockerHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, fmt, *args):
        pass

    def _clean_path(self):
        p = self.path.split("?", 1)[0]
        return re.sub(r"^/v\d+\.\d+", "", p) or "/"

    def _query(self):
        if "?" not in self.path:
            return {}
        return urllib.parse.parse_qs(self.path.split("?", 1)[1])

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8", "replace"))

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Api-Version", "1.45")
        self.send_header("Docker-Experimental", "false")
        self.send_header("Ostype", "linux")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, code=204):
        self.send_response(code)
        self.send_header("Api-Version", "1.45")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        p = self._clean_path()
        q = self._query()
        if p == "/_ping":
            body = b"OK"
            self.send_response(200)
            self.send_header("Api-Version", "1.45")
            self.send_header("Ostype", "linux")
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            return
        if p == "/version":
            return self._send_json(200, {
                "Platform": {"Name": "Docker Engine - Molab gVisor Shim"},
                "Version": "26.1.4",
                "ApiVersion": "1.45",
                "MinAPIVersion": "1.24",
                "GitCommit": "molab-shim",
                "GoVersion": "go1.22.4",
                "Os": "linux",
                "Arch": "amd64",
                "KernelVersion": "4.19.0-gvisor",
            })
        if p == "/info":
            with LOCK:
                total = len(CONTAINERS)
                running = sum(1 for c in CONTAINERS.values() if c.get("running"))
            return self._send_json(200, {
                "ID": "molab-gvisor-docker-shim",
                "Containers": total,
                "ContainersRunning": running,
                "ContainersPaused": 0,
                "ContainersStopped": total - running,
                "Images": 1,
                "Driver": "molab-host-shim",
                "MemoryLimit": True,
                "SwapLimit": True,
                "CpuCfsPeriod": True,
                "CpuCfsQuota": True,
                "CPUShares": True,
                "OSType": "linux",
                "Architecture": "x86_64",
                "NCPU": os.cpu_count() or 4,
                "MemTotal": 34359738368,
                "Name": socket.gethostname(),
                "ServerVersion": "26.1.4",
                "DockerRootDir": "/var/lib/docker",
                "OperatingSystem": "Debian GNU/Linux 12 (bookworm)",
                "KernelVersion": "4.19.0-gvisor",
            })
        if p == "/networks" or p.startswith("/networks/"):
            net_obj = {
                "Name": "pterodactyl_nw",
                "Id": "pterodactyl_nw_id_000000000000",
                "Created": now_iso(),
                "Scope": "local",
                "Driver": "bridge",
                "EnableIPv6": False,
                "IPAM": {"Driver": "default", "Config": [{"Subnet": "172.18.0.0/16", "Gateway": "172.18.0.1"}]},
                "Internal": False,
                "Containers": {},
                "Options": {},
                "Labels": {},
            }
            return self._send_json(200, [net_obj] if p == "/networks" else net_obj)
        if p == "/images/json":
            return self._send_json(200, [{"Id": "sha256:" + "a" * 64, "RepoTags": ["molab/host:latest"], "Created": int(time.time()), "Size": 1024}])
        if p.startswith("/images/") and p.endswith("/json"):
            return self._send_json(200, {"Id": "sha256:" + "a" * 64, "RepoTags": ["molab/host:latest"], "Created": now_iso(), "Size": 1024, "Config": {"Env": ["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"]}})
        if p == "/containers/json":
            out = []
            with LOCK:
                for cid, c in CONTAINERS.items():
                    out.append({
                        "Id": cid,
                        "Names": ["/" + c["name"].lstrip("/")],
                        "Image": c["image"],
                        "ImageID": "sha256:" + "a" * 64,
                        "Command": " ".join(c["spec"].get("Cmd") or ["bash"]),
                        "Created": c["created_ts"],
                        "State": "running" if c["running"] else "exited",
                        "Status": "Up" if c["running"] else f"Exited ({c['exit_code']})",
                        "Ports": [],
                        "Labels": c["spec"].get("Labels") or {},
                    })
            return self._send_json(200, out)
        m = re.match(r"^/containers/([^/]+)/json$", p)
        if m:
            cid = resolve_cid(m.group(1))
            if not cid:
                return self._send_json(404, {"message": "No such container"})
            c = CONTAINERS[cid]
            return self._send_json(200, {
                "Id": cid,
                "Created": c["created_at"],
                "Path": "/bin/bash",
                "Args": c["spec"].get("Cmd") or [],
                "State": {
                    "Status": "running" if c["running"] else "exited",
                    "Running": c["running"],
                    "Paused": False,
                    "Restarting": False,
                    "OOMKilled": False,
                    "Dead": False,
                    "Pid": c.get("pid") or 0,
                    "ExitCode": c.get("exit_code") or 0,
                    "Error": "",
                    "StartedAt": c.get("started_at") or now_iso(),
                    "FinishedAt": c.get("finished_at") or "0001-01-01T00:00:00Z",
                },
                "Image": "sha256:" + "a" * 64,
                "Name": "/" + c["name"].lstrip("/"),
                "RestartCount": 0,
                "Mounts": [],
                "Config": c["spec"],
                "HostConfig": c["spec"].get("HostConfig") or {},
                "NetworkSettings": {"IPAddress": "127.0.0.1", "Networks": {"pterodactyl_nw": {"IPAddress": "127.0.0.1"}}},
            })
        m = re.match(r"^/containers/([^/]+)/logs$", p)
        if m:
            cid = resolve_cid(m.group(1))
            if not cid:
                return self._send_json(404, {"message": "No such container"})
            c = CONTAINERS[cid]
            follow = (q.get("follow") or ["0"])[0].lower() in ("1", "true")
            tty = bool(c["spec"].get("Tty", True))
            with c["lock"]:
                initial = bytes(c["logs"][-32768:])
                sub = []
                if follow:
                    c["subscribers"].append(sub)
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.docker.raw-stream" if tty else "application/vnd.docker.multiplexed-stream")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            def _write_chunk(data):
                if not data:
                    return
                if tty:
                    self.wfile.write(data)
                else:
                    self.wfile.write(struct.pack(">BxxxI", 1, len(data)) + data)
                self.wfile.flush()
            try:
                _write_chunk(initial)
                while follow and c.get("running"):
                    chunk = None
                    with c["lock"]:
                        if sub:
                            chunk = sub.pop(0)
                    if chunk:
                        _write_chunk(chunk)
                    else:
                        time.sleep(0.15)
            except Exception:
                pass
            finally:
                with c["lock"]:
                    if sub in c["subscribers"]:
                        c["subscribers"].remove(sub)
            return
        m = re.match(r"^/containers/([^/]+)/stats$", p)
        if m:
            cid = resolve_cid(m.group(1))
            if not cid:
                return self._send_json(404, {"message": "No such container"})
            stream = (q.get("stream") or ["1"])[0].lower() not in ("0", "false")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            try:
                while True:
                    stat_obj = {
                        "read": now_iso(),
                        "preread": now_iso(),
                        "pids_stats": {"current": 4},
                        "cpu_stats": {"cpu_usage": {"total_usage": int(time.time() * 1e8), "percpu_usage": [int(time.time() * 1e8)]}, "system_cpu_usage": int(time.time() * 1e9), "online_cpus": os.cpu_count() or 4},
                        "precpu_stats": {"cpu_usage": {"total_usage": int((time.time() - 1) * 1e8)}, "system_cpu_usage": int((time.time() - 1) * 1e9), "online_cpus": os.cpu_count() or 4},
                        "memory_stats": {"usage": 268435456, "max_usage": 536870912, "limit": 34359738368},
                        "networks": {"eth0": {"rx_bytes": 4096, "tx_bytes": 4096}},
                    }
                    self.wfile.write((json.dumps(stat_obj) + "\n").encode("utf-8"))
                    self.wfile.flush()
                    if not stream or not CONTAINERS.get(cid, {}).get("running"):
                        break
                    time.sleep(1.5)
            except Exception:
                pass
            return
        if p == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            idx = len(EVENTS)
            try:
                while True:
                    with LOCK:
                        pending = EVENTS[idx:]
                        idx = len(EVENTS)
                    for ev in pending:
                        self.wfile.write((json.dumps(ev) + "\n").encode("utf-8"))
                        self.wfile.flush()
                    time.sleep(0.5)
            except Exception:
                pass
            return
        return self._send_json(200, {})

    def do_POST(self):
        p = self._clean_path()
        q = self._query()
        if p == "/networks/create":
            return self._send_json(201, {"Id": "pterodactyl_nw_id_000000000000", "Warning": ""})
        if p == "/images/create":
            img = (q.get("fromImage") or ["molab/host:latest"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            for msg in ({"status": f"Pulling from {img}"}, {"status": "Digest: sha256:" + "a" * 64}, {"status": f"Status: Image is up to date for {img}"}):
                self.wfile.write((json.dumps(msg) + "\n").encode("utf-8"))
            self.wfile.flush()
            return
        if p == "/containers/create":
            spec = self._read_json()
            name = (q.get("name") or [f"molab-{int(time.time()*1000)}"])[0]
            cid = hashlib.sha256(f"{name}-{time.time_ns()}".encode()).hexdigest()
            with LOCK:
                for old_id, old_c in list(CONTAINERS.items()):
                    if old_c["name"].lstrip("/") == name.lstrip("/"):
                        stop_container_proc(old_c)
                        CONTAINERS.pop(old_id, None)
                CONTAINERS[cid] = {
                    "id": cid,
                    "name": name,
                    "image": spec.get("Image") or "molab/host:latest",
                    "spec": spec,
                    "created_at": now_iso(),
                    "created_ts": int(time.time()),
                    "started_at": None,
                    "finished_at": None,
                    "running": False,
                    "pid": 0,
                    "exit_code": 0,
                    "proc": None,
                    "pty_fd": None,
                    "logs": bytearray(),
                    "subscribers": [],
                    "lock": threading.Lock(),
                }
            push_event(cid, "create", name)
            return self._send_json(201, {"Id": cid, "Warnings": []})
        m = re.match(r"^/containers/([^/]+)/(start|stop|restart|kill|wait|attach|resize)$", p)
        if m:
            cid = resolve_cid(m.group(1))
            action = m.group(2)
            if not cid:
                return self._send_json(404, {"message": "No such container"})
            c = CONTAINERS[cid]
            if action == "start":
                start_container_proc(c)
                return self._send_empty(204)
            if action in ("stop", "kill"):
                stop_container_proc(c, signal.SIGKILL if action == "kill" else signal.SIGTERM)
                return self._send_empty(204)
            if action == "restart":
                stop_container_proc(c)
                start_container_proc(c)
                return self._send_empty(204)
            if action == "resize":
                return self._send_empty(200)
            if action == "wait":
                while c.get("running"):
                    time.sleep(0.2)
                return self._send_json(200, {"StatusCode": c.get("exit_code") or 0})
            if action == "attach":
                sub = []
                with c["lock"]:
                    c["subscribers"].append(sub)
                    history = bytes(c["logs"][-16384:])
                self.send_response(101 if self.headers.get("Upgrade") else 200)
                self.send_header("Content-Type", "application/vnd.docker.raw-stream")
                if self.headers.get("Upgrade"):
                    self.send_header("Connection", "Upgrade")
                    self.send_header("Upgrade", "tcp")
                self.end_headers()
                conn = self.connection
                conn.setblocking(False)
                try:
                    if history:
                        conn.sendall(history)
                    while c.get("running") or sub:
                        with c["lock"]:
                            chunk = sub.pop(0) if sub else None
                        if chunk:
                            conn.sendall(chunk)
                        try:
                            inp = conn.recv(4096)
                            if inp and c.get("pty_fd"):
                                os.write(c["pty_fd"], inp)
                        except BlockingIOError:
                            pass
                        except Exception:
                            break
                        if not chunk:
                            time.sleep(0.08)
                except Exception:
                    pass
                finally:
                    with c["lock"]:
                        if sub in c["subscribers"]:
                            c["subscribers"].remove(sub)
                return
        return self._send_json(200, {})

    def do_DELETE(self):
        p = self._clean_path()
        m = re.match(r"^/containers/([^/]+)$", p)
        if m:
            cid = resolve_cid(m.group(1))
            if cid:
                c = CONTAINERS.pop(cid, None)
                if c:
                    stop_container_proc(c)
                    push_event(cid, "destroy", c["name"])
            return self._send_empty(204)
        return self._send_empty(204)

class ThreadedUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

def main():
    os.makedirs("/var/run", exist_ok=True)
    if os.path.exists(SOCK_PATH) or os.path.islink(SOCK_PATH):
        os.unlink(SOCK_PATH)
    srv = ThreadedUnixHTTPServer(SOCK_PATH, DockerHandler)
    os.chmod(SOCK_PATH, 0o666)
    srv.serve_forever()

if __name__ == "__main__":
    main()
"""

DOCKER_CLI_CODE = """#!/usr/bin/env python3
import http.client, json, socket, sys

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost")
        self.unix_path = path
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.unix_path)

def req(method, path, body=None):
    c = UnixHTTPConnection("/var/run/docker.sock")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    r = c.getresponse()
    data = r.read().decode("utf-8", "replace")
    return r.status, data

args = sys.argv[1:]
cmd = args[0] if args else "ps"
if cmd in ("version", "-v", "--version"):
    _, d = req("GET", "/version")
    v = json.loads(d)
    print(f"Docker version {v.get('Version')} ({v.get('Platform',{}).get('Name')})")
elif cmd == "info":
    _, d = req("GET", "/info")
    print(json.dumps(json.loads(d), indent=2))
elif cmd == "ps":
    _, d = req("GET", "/containers/json")
    items = json.loads(d)
    print(f"{'CONTAINER ID':<14} {'NAME':<28} {'STATUS':<18} {'COMMAND'}")
    for it in items:
        print(f"{it['Id'][:12]:<14} {it['Names'][0].lstrip('/'):<28} {it['Status']:<18} {it['Command'][:40]}")
elif cmd in ("stop", "start", "restart", "kill") and len(args) > 1:
    req("POST", f"/containers/{args[1]}/{cmd}")
    print(args[1])
elif cmd == "rm" and len(args) > 1:
    req("DELETE", f"/containers/{args[-1]}")
    print(args[-1])
elif cmd == "logs" and len(args) > 1:
    _, d = req("GET", f"/containers/{args[-1]}/logs")
    print(d, end="")
else:
    print("Molab Docker Shim CLI (commands: ps, info, version, logs <id>, start/stop/restart/rm <id>)")
"""


def load_cfg():
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_pkgs():
    if PKG_FILE.exists():
        try:
            data = json.loads(PKG_FILE.read_text(encoding="utf-8"))
            return {
                "apt": list(data.get("apt", [])),
                "pip": list(data.get("pip", [])),
                "apps": list(data.get("apps", [])),
                "wings": bool(data.get("wings", False)),
            }
        except Exception:
            pass
    return {"apt": [], "pip": [], "apps": [], "wings": False}


def save_pkgs(pkgs):
    PKG_FILE.write_text(
        json.dumps(pkgs, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def ensure_docker_shim():
    try:
        old_code = DOCKERD_BIN.read_text(encoding="utf-8", errors="replace") if DOCKERD_BIN.exists() else ""
        changed = old_code != DOCKER_SHIM_CODE
        DOCKERD_BIN.write_text(DOCKER_SHIM_CODE, encoding="utf-8")
        DOCKERD_BIN.chmod(0o755)
        DOCKER_CLI_BIN.write_text(DOCKER_CLI_CODE, encoding="utf-8")
        DOCKER_CLI_BIN.chmod(0o755)
        Path("/etc/systemd/system").mkdir(parents=True, exist_ok=True)
        unit_path = Path("/etc/systemd/system/docker.service")
        unit_path.write_text(
            "[Unit]\n"
            "Description=Molab Docker Engine Shim (/var/run/docker.sock for gVisor & Pterodactyl Wings)\n"
            "After=network.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            "User=root\n"
            "ExecStart=/usr/bin/python3 /usr/local/bin/molab-dockerd\n"
            "Restart=always\n"
            "RestartSec=2\n\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n",
            encoding="utf-8",
        )
        rc = subprocess.call(
            ["systemctl", "is-active", "--quiet", "docker.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if changed or rc != 0 or not Path("/var/run/docker.sock").exists():
            subprocess.call(["systemctl", "enable", "docker.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.call(["systemctl", "restart", "docker.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        print("docker shim error:", exc, flush=True)


def install_hooks():
    try:
        APT_HOOK.write_text(
            'DPkg::Post-Invoke {"/usr/local/bin/save --record-apt || true";};\n',
            encoding="utf-8",
        )
        for wrap in (PIP_WRAP, PIP3_WRAP):
            if wrap.parent.exists():
                wrap.write_text(PIP_WRAP_CODE, encoding="utf-8")
                wrap.chmod(0o755)
        DEPLOY_BIN.write_text(
            '#!/bin/sh\nexec /usr/local/bin/save --deploy "$@"\n',
            encoding="utf-8",
        )
        DEPLOY_BIN.chmod(0o755)
        ensure_docker_shim()
    except Exception as exc:
        print("hook error:", exc, flush=True)


def record_apt():
    hist = Path("/var/log/apt/history.log")
    if not hist.exists():
        return
    cmds = [
        line.split("Commandline:", 1)[1].strip()
        for line in hist.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.startswith("Commandline:")
    ]
    pkgs = load_pkgs()
    apt_list = list(pkgs["apt"])
    # 6 dòng Commandline đầu tiên là lúc build Docker image của molab -> bỏ qua
    for cmd in cmds[6:]:
        tokens = shlex.split(cmd)
        if "install" in tokens:
            idx = tokens.index("install")
            for tok in tokens[idx + 1 :]:
                if not tok.startswith("-") and tok != "openssh-server" and tok not in apt_list:
                    apt_list.append(tok)
        for action in ("remove", "purge"):
            if action in tokens:
                idx = tokens.index(action)
                for tok in tokens[idx + 1 :]:
                    if tok in apt_list:
                        apt_list.remove(tok)
    if apt_list != pkgs["apt"]:
        pkgs["apt"] = apt_list
        save_pkgs(pkgs)


def record_pip(args):
    if "install" not in args:
        return
    pkgs = load_pkgs()
    pip_list = list(pkgs["pip"])
    idx = args.index("install")
    skip_next = False
    for tok in args[idx + 1 :]:
        if skip_next:
            skip_next = False
            continue
        if tok in ("-r", "--requirement", "-c", "--constraint", "-i", "--index-url", "--extra-index-url", "-f", "--find-links", "-t", "--target", "--python"):
            skip_next = True
            continue
        if not tok.startswith("-") and tok not in pip_list:
            pip_list.append(tok)
    if pip_list != pkgs["pip"]:
        pkgs["pip"] = pip_list
        save_pkgs(pkgs)


def write_app_unit(idx, cmd):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    Path("/etc/systemd/system").mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"app-{idx}.log"
    unit_path = Path(f"/etc/systemd/system/molab-app-{idx}.service")
    runner = f"cd /home/marimo && exec {cmd} >> {shlex.quote(str(log_file))} 2>&1"
    unit_path.write_text(
        f"[Unit]\n"
        f"Description=Molab App #{idx}: {cmd}\n"
        f"After=network.target\n\n"
        f"[Service]\n"
        f"Type=simple\n"
        f"User=root\n"
        f"WorkingDirectory=/home/marimo\n"
        f'Environment="PATH=/tmp/uv-venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"\n'
        f"ExecStart=/bin/bash -lc {shlex.quote(runner)}\n"
        f"Restart=always\n"
        f"RestartSec=5\n\n"
        f"[Install]\n"
        f"WantedBy=multi-user.target\n",
        encoding="utf-8",
    )
    return f"molab-app-{idx}.service"


def app_is_active(idx):
    unit = f"molab-app-{idx}.service"
    rc = subprocess.call(
        ["systemctl", "is-active", "--quiet", unit],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return rc == 0


def write_wings_unit():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    Path("/etc/systemd/system").mkdir(parents=True, exist_ok=True)
    Path("/etc/pterodactyl").mkdir(parents=True, exist_ok=True)
    PTERO_BACKUP.mkdir(parents=True, exist_ok=True)
    (PTERO_BACKUP / "volumes").mkdir(parents=True, exist_ok=True)
    Path("/var/lib/pterodactyl").mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "wings.log"
    unit_path = Path("/etc/systemd/system/wings.service")
    runner = f"exec /usr/local/bin/wings --config /etc/pterodactyl/config.yml >> {shlex.quote(str(log_file))} 2>&1"
    unit_path.write_text(
        "[Unit]\n"
        "Description=Pterodactyl Wings Daemon (via Molab Docker Socket Shim)\n"
        "After=network.target docker.service\n\n"
        "[Service]\n"
        "Type=simple\n"
        "User=root\n"
        "WorkingDirectory=/etc/pterodactyl\n"
        "LimitNOFILE=4096\n"
        'Environment="DOCKER_HOST=unix:///var/run/docker.sock"\n'
        f"ExecStart=/bin/bash -lc {shlex.quote(runner)}\n"
        "Restart=always\n"
        "RestartSec=5\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n",
        encoding="utf-8",
    )


def sync_wings_config():
    Path("/etc/pterodactyl").mkdir(parents=True, exist_ok=True)
    PTERO_BACKUP.mkdir(parents=True, exist_ok=True)
    etc_cfg = Path("/etc/pterodactyl/config.yml")
    bak_cfg = PTERO_BACKUP / "config.yml"
    if etc_cfg.exists() and etc_cfg.stat().st_size > 10:
        txt = etc_cfg.read_text(encoding="utf-8", errors="replace")
        if "ignore_panel_config_updates:" not in txt:
            txt += "\nignore_panel_config_updates: true\n"
            etc_cfg.write_text(txt, encoding="utf-8")
        bak_cfg.write_text(txt, encoding="utf-8")
    elif bak_cfg.exists() and bak_cfg.stat().st_size > 10:
        etc_cfg.write_text(bak_cfg.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")


def ensure_wings_installed(start_if_configured=True):
    ensure_docker_shim()
    if not WINGS_BIN.exists() or WINGS_BIN.stat().st_size < 1000000:
        arch = "amd64"
        url = f"https://github.com/pterodactyl/wings/releases/latest/download/wings_linux_{arch}"
        subprocess.run(["curl", "-L", "-fsS", "-o", str(WINGS_BIN), url], check=True)
        WINGS_BIN.chmod(0o755)
    write_wings_unit()
    sync_wings_config()
    subprocess.call(["systemctl", "enable", "wings.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if start_if_configured and Path("/etc/pterodactyl/config.yml").exists():
        subprocess.call(["systemctl", "restart", "wings.service"])


def handle_wings(args):
    ensure_docker_shim()
    pkgs = load_pkgs()
    sub = (args[0] if args else "status").lower()
    if sub == "status":
        d_rc = subprocess.call(["systemctl", "is-active", "--quiet", "docker.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        w_rc = subprocess.call(["systemctl", "is-active", "--quiet", "wings.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ver = ""
        if WINGS_BIN.exists():
            try:
                ver = subprocess.check_output([str(WINGS_BIN), "--version"], text=True, stderr=subprocess.STDOUT, timeout=5).strip()
            except Exception:
                ver = "installed"
        print(json.dumps({
            "docker_active": d_rc == 0 and Path("/var/run/docker.sock").exists(),
            "wings_enabled": bool(pkgs.get("wings", False)),
            "wings_installed": WINGS_BIN.exists(),
            "wings_version": ver,
            "wings_configured": Path("/etc/pterodactyl/config.yml").exists() or (PTERO_BACKUP / "config.yml").exists(),
            "wings_active": w_rc == 0,
        }))
        return
    if sub == "install":
        ensure_wings_installed(start_if_configured=True)
        pkgs["wings"] = True
        save_pkgs(pkgs)
        push(load_cfg())
        ver = subprocess.check_output([str(WINGS_BIN), "--version"], text=True, stderr=subprocess.STDOUT, timeout=5).strip()
        print(f"Đã cài đặt Pterodactyl {ver} + Docker Socket Shim (/var/run/docker.sock)!", flush=True)
        return
    if sub in ("remove", "uninstall", "off"):
        subprocess.call(["systemctl", "stop", "wings.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        Path("/etc/systemd/system/wings.service").unlink(missing_ok=True)
        WINGS_BIN.unlink(missing_ok=True)
        pkgs["wings"] = False
        save_pkgs(pkgs)
        push(load_cfg())
        print("Đã dừng & gỡ bỏ binary Pterodactyl Wings (giải phóng ~45MB).", flush=True)
        return
    if sub == "restart":
        ensure_wings_installed(start_if_configured=True)
        print("Đã khởi động lại docker.service & wings.service!", flush=True)
        return
    if sub == "configure":
        ensure_wings_installed(start_if_configured=False)
        pkgs["wings"] = True
        save_pkgs(pkgs)
        raw_cmd = " ".join(args[1:]).strip()
        if raw_cmd.startswith("wings "):
            raw_cmd = "/usr/local/bin/" + raw_cmd
        elif not raw_cmd.startswith("/usr/local/bin/wings"):
            raw_cmd = "/usr/local/bin/wings configure " + raw_cmd
        if "--allow-insecure" not in raw_cmd:
            raw_cmd += " --allow-insecure"
        rc = subprocess.call(["/bin/bash", "-lc", raw_cmd])
        sync_wings_config()
        if Path("/etc/pterodactyl/config.yml").exists():
            subprocess.call(["systemctl", "restart", "wings.service"])
            push(load_cfg())
            print("Đã cấu hình /etc/pterodactyl/config.yml, lưu HF & bật wings.service thành công!", flush=True)
        else:
            print(f"Lệnh wings configure trả về mã {rc} (chưa tạo được config.yml).", flush=True)
        return


def sync_apps_and_systemd(restart_all=False):
    ensure_docker_shim()
    if SYSTEMD_BACKUP.is_dir():
        Path("/etc/systemd/system").mkdir(parents=True, exist_ok=True)
        for f in SYSTEMD_BACKUP.glob("*.service"):
            if f.name in ("docker.service", "wings.service"):
                continue
            dst = Path("/etc/systemd/system") / f.name
            if not dst.exists():
                dst.write_text(f.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
                subprocess.call(["systemctl", "enable", f.name])
                subprocess.call(["systemctl", "start", f.name])

    pkgs = load_pkgs()
    if pkgs.get("wings"):
        try:
            ensure_wings_installed(start_if_configured=True)
        except Exception as exc:
            print("wings restore error:", exc, flush=True)
    apps = pkgs.get("apps") or []
    for i, cmd in enumerate(apps, 1):
        unit = write_app_unit(i, cmd)
        subprocess.call(["systemctl", "enable", unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if restart_all or not app_is_active(i):
            subprocess.call(["systemctl", "restart", unit])


def backup_custom_systemd():
    sync_wings_config()
    sys_dir = Path("/etc/systemd/system")
    if not sys_dir.is_dir():
        return
    for f in sys_dir.glob("*.service"):
        if f.is_symlink() or f.name.startswith("molab-app-") or f.name in ("docker.service", "wings.service"):
            continue
        SYSTEMD_BACKUP.mkdir(parents=True, exist_ok=True)
        (SYSTEMD_BACKUP / f.name).write_text(
            f.read_text(encoding="utf-8", errors="replace"), encoding="utf-8"
        )


def handle_deploy(args):
    pkgs = load_pkgs()
    apps = list(pkgs.get("apps") or [])
    if not args or args[0] in ("--list", "-l"):
        if not apps:
            print("Chưa có app nào chạy ngầm. Dùng: deploy <lệnh>", flush=True)
            return
        for i, cmd in enumerate(apps, 1):
            st = "🟢 Đang chạy" if app_is_active(i) else "🔴 Đã dừng"
            print(f"[{i}] {st} | {cmd} (log: /home/marimo/.logs/app-{i}.log)", flush=True)
        return

    if args[0] in ("--del", "--stop", "-d") and len(args) > 1 and args[1].isdigit():
        idx = int(args[1])
        if idx < 1 or idx > len(apps):
            print(f"Không tìm thấy app số #{idx}.", flush=True)
            return
        for i in range(1, len(apps) + 1):
            subprocess.call(["systemctl", "stop", f"molab-app-{i}.service"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            Path(f"/etc/systemd/system/molab-app-{i}.service").unlink(missing_ok=True)
        removed = apps.pop(idx - 1)
        pkgs["apps"] = apps
        save_pkgs(pkgs)
        sync_apps_and_systemd(restart_all=True)
        push(load_cfg())
        print(f"Đã dừng & xoá app #{idx}: {removed}", flush=True)
        return

    if args[0] in ("--restart", "-r") and len(args) > 1 and args[1].isdigit():
        idx = int(args[1])
        if idx < 1 or idx > len(apps):
            print(f"Không tìm thấy app số #{idx}.", flush=True)
            return
        unit = write_app_unit(idx, apps[idx - 1])
        subprocess.call(["systemctl", "restart", unit])
        print(f"Đã khởi động lại app #{idx}: {apps[idx - 1]}", flush=True)
        return

    cmd = " ".join(args).strip()
    if cmd not in apps:
        apps.append(cmd)
        pkgs["apps"] = apps
        save_pkgs(pkgs)
    idx = apps.index(cmd) + 1
    unit = write_app_unit(idx, cmd)
    subprocess.call(["systemctl", "enable", unit], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.call(["systemctl", "restart", unit])
    push(load_cfg())
    print(f"Đã chạy ngầm & lưu tự động bật lại sau 12h [App #{idx}]: {cmd}", flush=True)


def reinstall_saved_pkgs():
    pkgs = load_pkgs()
    apt_pkgs = pkgs.get("apt") or []
    pip_pkgs = pkgs.get("pip") or []
    if apt_pkgs:
        print(f"Đang cài lại apt: {' '.join(apt_pkgs)}", flush=True)
        env = os.environ.copy()
        env["DEBIAN_FRONTEND"] = "noninteractive"
        subprocess.call(["apt-get", "update", "-qq"], env=env)
        subprocess.call(["apt-get", "install", "-y", "-qq", *apt_pkgs], env=env)
    if pip_pkgs:
        print(f"Đang cài lại pip: {' '.join(pip_pkgs)}", flush=True)
        subprocess.call(
            ["/usr/local/bin/uv", "pip", "install", "--python", "/tmp/uv-venv/bin/python3", *pip_pkgs]
        )
    sync_apps_and_systemd(restart_all=True)
    if SETUP_SH.is_file():
        print("Đang chạy /home/marimo/setup.sh", flush=True)
        subprocess.call(["bash", str(SETUP_SH)], cwd=str(WORK_DIR))


def ignored(rel_posix):
    top = rel_posix.split("/")[0]
    return any(
        fnmatch.fnmatch(rel_posix, pat) or fnmatch.fnmatch(top, pat) for pat in IGNORE
    )


def scan_state():
    state = {}
    for root, dirs, files in os.walk(WORK_DIR):
        rel_root = Path(root).relative_to(WORK_DIR).as_posix()
        dirs[:] = [
            d
            for d in dirs
            if not ignored(f"{rel_root}/{d}".lstrip("./")) and d != ".git"
        ]
        for name in files:
            rel = f"{rel_root}/{name}".lstrip("./") if rel_root != "." else name
            if ignored(rel):
                continue
            try:
                st = (Path(root) / name).stat()
                if st.st_size > MAX_SYNC_FILE_BYTES:
                    continue
                state[rel] = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass
    return state


def send_tg_file(cfg, file_path):
    token = cfg.get("telegram_bot_token") or ""
    chat = cfg.get("telegram_chat_id") or ""
    if not token or not chat:
        print("Chưa có telegram_chat_id (hãy nhắn /start cho @sixthousand_bot).", flush=True)
        return
    subprocess.run(
        [
            "curl",
            "-fsS",
            "-F",
            f"chat_id={chat}",
            "-F",
            f"document=@{file_path}",
            f"https://api.telegram.org/bot{token}/sendDocument",
        ],
        check=True,
    )
    print(f"Đã gửi {file_path} lên Telegram.", flush=True)


def restore(cfg):
    install_hooks()
    if MARKER.exists():
        return
    api = HfApi(token=cfg["hf_token"])
    api.create_repo(
        repo_id=cfg["hf_repo"], repo_type="dataset", private=True, exist_ok=True
    )
    snapshot_download(
        repo_id=cfg["hf_repo"],
        repo_type="dataset",
        local_dir=str(WORK_DIR),
        token=cfg["hf_token"],
        ignore_patterns=[".gitattributes"],
    )
    reinstall_saved_pkgs()
    MARKER.write_text(str(time.time()), encoding="utf-8")
    print(f"Đã khôi phục dữ liệu & gói từ {cfg['hf_repo']} về {WORK_DIR}", flush=True)


def push(cfg):
    record_apt()
    backup_custom_systemd()
    large_ignores = []
    for root, dirs, files in os.walk(WORK_DIR):
        rel_root = Path(root).relative_to(WORK_DIR).as_posix()
        dirs[:] = [
            d
            for d in dirs
            if not ignored(f"{rel_root}/{d}".lstrip("./")) and d != ".git"
        ]
        for name in files:
            rel = f"{rel_root}/{name}".lstrip("./") if rel_root != "." else name
            if ignored(rel):
                continue
            try:
                if (Path(root) / name).stat().st_size > MAX_SYNC_FILE_BYTES:
                    large_ignores.append(rel)
            except OSError:
                pass
    api = HfApi(token=cfg["hf_token"])
    api.create_repo(
        repo_id=cfg["hf_repo"], repo_type="dataset", private=True, exist_ok=True
    )
    api.upload_folder(
        repo_id=cfg["hf_repo"],
        repo_type="dataset",
        folder_path=str(WORK_DIR),
        ignore_patterns=IGNORE + large_ignores,
        commit_message=f"molab sync {time.strftime('%Y-%m-%d %H:%M:%S')}",
    )
    print(
        f"Đã lưu {WORK_DIR} lên https://huggingface.co/datasets/{cfg['hf_repo']}",
        flush=True,
    )


def daemon():
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    install_hooks()
    cfg = load_cfg()
    restore(cfg)
    last = scan_state()
    while True:
        time.sleep(300)
        try:
            cfg = load_cfg()
            cur = scan_state()
            if cur and cur != last:
                push(cfg)
                last = cur
        except Exception as exc:
            print("sync error:", exc, flush=True)


def start_daemon():
    install_hooks()
    sync_apps_and_systemd(restart_all=False)
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            return
        except Exception:
            pass
    log_fh = open("/tmp/molab_sync.log", "ab")
    subprocess.Popen(
        [sys.executable, "/usr/local/bin/save", "--daemon"],
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        close_fds=True,
        start_new_session=True,
    )


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "--push"
    if arg == "--daemon":
        daemon()
    elif arg == "--start-daemon":
        start_daemon()
    elif arg == "--restore":
        restore(load_cfg())
    elif arg == "--record-apt":
        record_apt()
    elif arg == "--record-pip":
        record_pip(sys.argv[2:])
    elif arg == "--deploy":
        handle_deploy(sys.argv[2:])
    elif arg == "--wings":
        handle_wings(sys.argv[2:])
    elif arg == "--push":
        push(load_cfg())
    else:
        cfg = load_cfg()
        if Path(arg).is_file():
            send_tg_file(cfg, arg)
        push(cfg)
