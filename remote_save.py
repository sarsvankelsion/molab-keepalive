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
SETUP_SH = WORK_DIR / "setup.sh"
MARKER = Path("/tmp/.molab_hf_restored")
PID_FILE = Path("/tmp/.molab_hf_daemon.pid")
APT_HOOK = Path("/etc/apt/apt.conf.d/99molab-save")
PIP_WRAP = Path("/tmp/uv-venv/bin/pip")
PIP3_WRAP = Path("/tmp/uv-venv/bin/pip3")
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
    ".bash*",
    ".profile",
    ".wget-hsts",
    ".lesshst",
    ".git",
    "**/.git/**",
    "**/__pycache__/**",
]

PIP_WRAP_CODE = """#!/usr/bin/env python3
import subprocess, sys
rc = subprocess.call(["/usr/local/bin/uv", "pip", *sys.argv[1:], "--python", "/tmp/uv-venv/bin/python3"])
if rc != 0:
    rc = subprocess.call(["/usr/local/bin/pip", *sys.argv[1:]])
if rc == 0 and "install" in sys.argv[1:]:
    subprocess.call(["/usr/local/bin/save", "--record-pip", *sys.argv[1:]])
sys.exit(rc)
"""


def load_cfg():
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_pkgs():
    if PKG_FILE.exists():
        try:
            data = json.loads(PKG_FILE.read_text(encoding="utf-8"))
            return {"apt": list(data.get("apt", [])), "pip": list(data.get("pip", []))}
        except Exception:
            pass
    return {"apt": [], "pip": []}


def save_pkgs(pkgs):
    PKG_FILE.write_text(
        json.dumps(pkgs, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


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
    api = HfApi(token=cfg["hf_token"])
    api.create_repo(
        repo_id=cfg["hf_repo"], repo_type="dataset", private=True, exist_ok=True
    )
    api.upload_folder(
        repo_id=cfg["hf_repo"],
        repo_type="dataset",
        folder_path=str(WORK_DIR),
        ignore_patterns=IGNORE,
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
    elif arg == "--push":
        push(load_cfg())
    else:
        cfg = load_cfg()
        if Path(arg).is_file():
            send_tg_file(cfg, arg)
        push(cfg)
