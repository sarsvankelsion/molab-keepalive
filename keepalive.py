"""Giữ session molab sống headless và tự đăng lại địa chỉ SSH.

  python keepalive.py --login   # một lần: đăng nhập molab trong cửa sổ hiện
  python keepalive.py           # chạy nền, headless, mãi mãi
  python keepalive.py --once    # test: mở, chạy cell, in địa chỉ rồi thoát

Script làm 3 việc:
  1. Mở notebook trong Chromium headless (profile đã login) -> giữ kết nối,
     nên molab không idle-shutdown.
  2. Cứ cell_rerun_minutes phút bấm Shift+Enter để tạo tunnel Pinggy mới
     (Pinggy free chết sau 60 phút), ghi địa chỉ + password mới.
  3. Cứ session_restart_minutes phút reload notebook (molab cap 12h).

Địa chỉ mới ghi vào output_file; gửi Telegram nếu điền token/chat id.
"""

import argparse
import base64
import html
import json
import logging
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"


def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(f"Thiếu {CONFIG_PATH}. Copy config.example.json -> config.json rồi điền notebook_url.")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    if not cfg.get("notebook_url"):
        sys.exit("config.json: notebook_url đang trống.")
    cfg.setdefault("profile_dir", "profile")
    cfg.setdefault("headless", True)
    cfg.setdefault("session_restart_minutes", 725)
    cfg.setdefault("cell_rerun_minutes", 55)
    cfg.setdefault("output_file", "last_ssh.txt")
    cfg.setdefault("telegram_bot_token", "")
    cfg.setdefault("telegram_chat_id", "")
    cfg.setdefault("telegram_allowed_ids", [])
    cfg.setdefault("hf_token", "")
    cfg.setdefault("hf_repo", "")
    if cfg["cell_rerun_minutes"] >= 60:
        logging.warning("cell_rerun_minutes >= 60: tunnel Pinggy free sẽ chết trước khi rerun")
    return cfg


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(ROOT / "keepalive.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def visible_text(page):
    texts = []
    for frame in page.frames:
        try:
            texts.append(frame.inner_text("body", timeout=3000))
        except Exception:
            continue
    return "\n".join(texts)


def scrape(page):
    """Trả (ssh_line, password) từ output của cell, hoặc None."""
    text = visible_text(page)
    match = re.search(r"ssh\b[^\n]*?-p\s+(\d+)\s+(\S+@\S+)", text)
    # Bỏ các match nằm trong code cell (vd. "password: {PASSWORD}") và lấy
    # match cuối, vì output nằm sau code trong DOM.
    passwords = [p for p in re.findall(r"password:\s*(\S+)", text) if "{" not in p]
    if not match or not passwords:
        return None
    ssh_line = f"ssh -o ServerAliveInterval=30 -p {match.group(1)} {match.group(2)}"
    return ssh_line, passwords[-1]


def find_ssh_cell(page):
    fallback = None
    for frame in page.frames:
        editors = frame.locator(".cm-content")
        count = editors.count()
        if count and fallback is None:
            fallback = editors.first
        for index in range(count):
            try:
                if "import pinggy" in editors.nth(index).inner_text(timeout=1500):
                    return editors.nth(index)
            except Exception:
                continue
    return fallback


def editor_frame(page):
    """Frame chứa editor marimo (preview hoặc session)."""
    for frame in page.frames:
        try:
            if frame.locator(".cm-content").count():
                return frame
        except Exception:
            continue
    return None


def wait_editor_frame(page, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        frame = editor_frame(page)
        if frame is not None:
            return frame
        page.wait_for_timeout(2000)  # molab đang "Scheduling sandbox..."
    return None


def palette(page, frame, command, confirm=None):
    """Mở command palette (Ctrl+K), chạy `command`, xác nhận nếu có."""
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    btn = frame.get_by_test_id("command-palette-button")
    if btn.count():
        btn.first.click()
    else:
        frame.locator(".cm-content").first.click(position={"x": 80, "y": 20})
        page.wait_for_timeout(300)
        page.keyboard.press("Control+k")
    dialog = frame.locator("[role=dialog]").first
    try:
        dialog.wait_for(state="visible", timeout=8000)
    except Exception:
        logging.warning("Không mở được command palette, bỏ qua '%s'", command)
        return False
    page.keyboard.type(command, delay=40)
    page.wait_for_timeout(800)
    page.keyboard.press("Enter")
    if confirm:
        page.wait_for_timeout(1000)
        alert = frame.locator("[role=alertdialog]").first
        alert.wait_for(state="visible", timeout=10000)
        alert.get_by_role("button", name=confirm).click()
        page.wait_for_timeout(2000)
    return True


def restart_kernel(page):
    """Kernel restart xoá biến `_tunnel` cũ, nếu không Pinggy free từ chối tunnel thứ 2."""
    frame = wait_editor_frame(page)
    if frame is None:
        return False
    logging.info("Restart kernel")
    ok = palette(page, frame, "restart kernel", confirm="Confirm Restart")
    if ok:
        page.wait_for_timeout(15000)  # đợi kernel restart + iframe load lại
    return ok


def run_cell(page, timeout=420):
    deadline = time.time() + timeout
    editor = None
    while time.time() < deadline:
        take_over(page)
        editor = find_ssh_cell(page)
        if editor is not None:
            break
        page.wait_for_timeout(3000)  # molab đang "Scheduling sandbox..."
    if editor is None:
        logging.warning("Không thấy editor .cm-content sau %ss", timeout)
        return None
    page.wait_for_timeout(5000)  # đợi output cũ (nếu có) render xong qua WebSocket
    take_over(page)
    before = scrape(page)
    frame = editor_frame(page)
    run_btn = frame.get_by_test_id("run-button") if frame else None
    if run_btn and run_btn.count():
        run_btn.first.click()
    else:
        editor.click(position={"x": 80, "y": 20})
        page.keyboard.press("Shift+Enter")
    page.wait_for_timeout(4000)
    started = time.time()
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        result = scrape(page)
        if result and result != before:
            return result
        if time.time() - started > 70:
            text = visible_text(page)
            if (
                "IndexError" in text
                or "Tunnel failed to start" in text
                or "Không tạo được tunnel" in text
            ):
                logging.warning("Pinggy từ chối tunnel, chạy lại cell")
                return None
    return None


def take_over(page):
    """Molab chỉ cho 1 writer; reader phải bấm Take over mới chạy được cell."""
    for frame in page.frames:
        try:
            button = frame.get_by_role("button", name=re.compile("take over", re.I))
            if button.count() and button.first.is_visible():
                logging.info("Bấm Take over để giành quyền writer")
                button.first.click()
                page.wait_for_timeout(3000)
                return True
        except Exception:
            continue
    return False


def tunnel_alive(result):
    if not result:
        return False
    match = re.search(r"-p\s+(\d+)\s+\S+@(\S+)", result[0])
    if not match:
        return False
    try:
        with socket.create_connection((match.group(2), int(match.group(1))), timeout=5) as sock:
            sock.settimeout(5)
            return sock.recv(16).startswith(b"SSH-")
    except OSError:
        return False


def ensure_running(page, timeout=420, attempts=3, force_rerun=False):
    if not force_rerun and wait_editor_frame(page, timeout=60) is not None:
        page.wait_for_timeout(5000)
        take_over(page)
        existing = scrape(page)
        if tunnel_alive(existing):
            logging.info("Dùng lại tunnel đang sống trên trang")
            return existing
    for attempt in range(1, attempts + 1):
        take_over(page)
        result = run_cell(page, timeout)  # chạy lại cell; tunnel cũ bị force=True đá
        if result:
            return result
        logging.warning("Lần %s/%s không lấy được địa chỉ SSH", attempt, attempts)
        # Fallback: restart kernel để xoá sạch tunnel cũ rồi thử lại
        restart_kernel(page)
        page.wait_for_timeout(5000)
    return None


ADMIN_ID = "7521313803"
TG_OFFSET = 0

MAIN_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "🔑 Thông tin SSH", "callback_data": "/ssh"},
            {"text": "📊 Cấu hình & GPU", "callback_data": "/status"},
        ],
        [
            {"text": "🔄 Đổi cổng SSH", "callback_data": "/rotate"},
            {"text": "💾 Backup HF ngay", "callback_data": "/save"},
        ],
        [
            {"text": "📦 Gói tự cài", "callback_data": "/pkgs"},
            {"text": "👥 Quản lý ID", "callback_data": "/ids"},
        ],
        [
            {"text": "♻️ Reset phiên 12h", "callback_data": "/restart"},
            {"text": "📖 Bảng lệnh", "callback_data": "/help"},
        ],
    ]
}

BOT_HELP = (
    "⚡ <b>MOLAB CLOUD VPS — BẢNG ĐIỀU KHIỂN</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "🖥 <b>Kết nối & Phiên</b>\n"
    "• <code>/ssh</code> — Lấy lệnh SSH, mật khẩu & trạng thái\n"
    "• <code>/status</code> — Xem GPU, VRAM, RAM, ổ cứng & Uptime\n"
    "• <code>/rotate</code> — Đổi cổng & mật khẩu SSH mới ngay\n"
    "• <code>/restart</code> — Lưu dữ liệu & tạo phiên 12h mới\n"
    "• <code>/set 55 725</code> — Chỉnh chu kỳ xoay (phút)\n\n"
    "💾 <b>Lưu trữ & Gói cài đặt</b>\n"
    "• <code>/save</code> — Đồng bộ ngay lên Hugging Face\n"
    "• <code>/pkgs</code> — Xem các gói <code>apt</code> / <code>pip</code> tự cài\n"
    "• <code>/addpkg apt tên_gói</code> — Cài & nhớ gói mới\n"
    "• <code>/delpkg tên_gói</code> — Xoá gói khỏi danh sách nhớ\n"
    "• <code>/sh lệnh</code> — Chạy lệnh trực tiếp trên VPS\n\n"
    "👥 <b>Phân quyền người dùng</b>\n"
    "• <code>/ids</code> — Xem danh sách ID có quyền\n"
    "• <code>/addid 123456789</code> — Cấp quyền cho ID mới\n"
    "• <code>/delid 123456789</code> — Thu hồi quyền ID\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "💡 <i>Chạm trực tiếp vào lệnh SSH hoặc mật khẩu để copy 1 chạm, hoặc bấm các nút bên dưới:</i>"
)


def save_config(cfg):
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def get_allowed_ids(cfg):
    ids = []
    for raw in [ADMIN_ID, cfg.get("telegram_chat_id", ""), *(cfg.get("telegram_allowed_ids") or [])]:
        val = str(raw).strip()
        if val and val not in ids:
            ids.append(val)
    if cfg.get("telegram_allowed_ids") != ids or cfg.get("telegram_chat_id") != ids[0]:
        cfg["telegram_allowed_ids"] = ids
        cfg["telegram_chat_id"] = ids[0]
        save_config(cfg)
    return ids


def tg_api(cfg, method, payload=None, timeout=10):
    token = cfg.get("telegram_bot_token") or ""
    if not token:
        return None
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(payload).encode() if payload else None
    try:
        with urllib.request.urlopen(url, data=data, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        logging.debug("Telegram API %s lỗi: %s", method, exc)
        return None


def send_telegram(cfg, text, chat_id=None, html_mode=True, menu=True):
    targets = [str(chat_id)] if chat_id else get_allowed_ids(cfg)
    for target in targets:
        payload = {
            "chat_id": target,
            "text": text[:4000],
            "disable_web_page_preview": "true",
        }
        if html_mode:
            payload["parse_mode"] = "HTML"
        if menu:
            payload["reply_markup"] = json.dumps(MAIN_KEYBOARD)
        tg_api(cfg, "sendMessage", payload)


def format_ssh_card(cfg, result, state=None, title="🔑 <b>THÔNG TIN KẾT NỐI SSH</b>"):
    ssh_line, password = result
    alive = "🟢 Đang hoạt động" if tunnel_alive(result) else "🔴 Mất kết nối"
    timers = ""
    if state:
        rem_ssh = max(0, int((state["next_rerun"] - time.monotonic()) // 60))
        rem_ses = max(0, int((state["next_restart"] - time.monotonic()) // 60))
        timers = f"⏱ <b>Đổi cổng sau:</b> {rem_ssh} phút  |  <b>Reset phiên:</b> {rem_ses} phút\n"
    repo = cfg.get("hf_repo") or "chưa cấu hình"
    return (
        f"{title}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡 <b>Trạng thái:</b> {alive}\n"
        f"🕒 <b>Cập nhật:</b> <code>{time.strftime('%H:%M:%S — %d/%m/%Y')}</code>\n"
        f"{timers}"
        f"☁️ <b>Backup HF:</b> <code>{html.escape(repo)}</code>\n\n"
        "🖥 <b>Lệnh SSH</b> <i>(chạm để copy)</i>:\n"
        f"<code>{html.escape(ssh_line)}</code>\n\n"
        "🔐 <b>Mật khẩu</b> <i>(chạm để copy)</i>:\n"
        f"<code>{html.escape(password)}</code>"
    )


def init_telegram_bot(cfg):
    global TG_OFFSET
    if not cfg.get("telegram_bot_token"):
        return
    get_allowed_ids(cfg)
    commands = [
        {"command": "ssh", "description": "🔑 Lấy lệnh SSH & mật khẩu (copy 1 chạm)"},
        {"command": "status", "description": "📊 Xem GPU, RAM, Disk & trạng thái VPS"},
        {"command": "rotate", "description": "🔄 Đổi cổng & mật khẩu SSH mới ngay"},
        {"command": "save", "description": "💾 Đồng bộ dữ liệu lên Hugging Face ngay"},
        {"command": "pkgs", "description": "📦 Danh sách gói apt/pip tự cài"},
        {"command": "addpkg", "description": "➕ Cài & lưu gói: /addpkg <apt|pip> <tên>"},
        {"command": "delpkg", "description": "➖ Xoá gói tự cài: /delpkg <tên>"},
        {"command": "sh", "description": "💻 Chạy lệnh bash: /sh <lệnh>"},
        {"command": "ids", "description": "👥 Xem danh sách ID có quyền"},
        {"command": "addid", "description": "👤 Cấp quyền ID mới: /addid <id>"},
        {"command": "delid", "description": "🚫 Xoá quyền ID: /delid <id>"},
        {"command": "set", "description": "⚙️ Đổi chu kỳ: /set <phút_ssh> [phút_phiên]"},
        {"command": "restart", "description": "♻️ Lưu HF & khởi động lại phiên 12h"},
        {"command": "help", "description": "📖 Mở bảng điều khiển & hướng dẫn"},
    ]
    tg_api(cfg, "setMyCommands", {"commands": json.dumps(commands)})
    res = tg_api(cfg, "getUpdates", {"offset": -1, "timeout": 0})
    items = (res or {}).get("result") or []
    if items:
        TG_OFFSET = items[-1]["update_id"] + 1


def ssh_exec(result, cmd, timeout=180):
    if not result:
        return "Chưa có kết nối SSH."
    match = re.search(r"-p\s+(\d+)\s+(\S+)@(\S+)", result[0])
    if not match:
        return "Lệnh SSH không hợp lệ."
    port, user, host = int(match.group(1)), match.group(2), match.group(3)
    _, password = result
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=host,
        port=port,
        username=user,
        password=password,
        timeout=20,
        allow_agent=False,
        look_for_keys=False,
    )
    try:
        _, stdout, _ = client.exec_command(cmd, timeout=timeout)
        return stdout.read().decode("utf-8", "replace").strip()
    finally:
        client.close()


def sync_remote(cfg, result, mode="--start-daemon"):
    if not result or not cfg.get("hf_token") or not cfg.get("hf_repo"):
        return
    remote_cfg = json.dumps(
        {
            "hf_token": cfg["hf_token"],
            "hf_repo": cfg["hf_repo"],
            "telegram_bot_token": cfg.get("telegram_bot_token", ""),
            "telegram_chat_id": cfg.get("telegram_chat_id", ADMIN_ID),
        }
    ).encode("utf-8")
    b64_cfg = base64.b64encode(remote_cfg).decode("ascii")
    b64_script = base64.b64encode((ROOT / "remote_save.py").read_bytes()).decode("ascii")
    cmd = (
        f"printf '%s' '{b64_cfg}' | base64 -d > /root/.molab_sync_cfg.json && "
        "chmod 600 /root/.molab_sync_cfg.json && "
        f"printf '%s' '{b64_script}' | base64 -d > /usr/local/bin/save && "
        "chmod 755 /usr/local/bin/save && "
        f"/usr/local/bin/save {mode}"
    )
    try:
        out = ssh_exec(result, cmd, timeout=180)
        if out:
            logging.info("HF sync (%s): %s", mode, out)
        return out
    except Exception as exc:
        logging.warning("Không cài/chạy được HF sync qua SSH: %s", exc)
        return f"Lỗi: {exc}"


def publish(cfg, result, state=None):
    ssh_line, password = result
    text = f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n{ssh_line}\npassword: {password}\n"
    path = ROOT / cfg["output_file"]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    logging.info("Địa chỉ mới:\n%s", text.strip())
    send_telegram(cfg, format_ssh_card(cfg, result, state))
    sync_remote(cfg, result, mode="--start-daemon")


def open_context(playwright, cfg, headless):
    profile = ROOT / cfg["profile_dir"]
    profile.mkdir(exist_ok=True)
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        headless=headless,
        viewport={"width": 1400, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
        ignore_default_args=["--enable-automation"],
    )


def has_editor(page):
    for frame in page.frames:
        try:
            if frame.locator(".cm-content").count():
                return True
        except Exception:
            continue
    return False


def login(cfg):
    with sync_playwright() as playwright:
        context = open_context(playwright, cfg, headless=False)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(cfg["notebook_url"], wait_until="domcontentloaded", timeout=120000)
            print("Đăng nhập molab trong cửa sổ Chromium. Script tự phát hiện khi xong...", flush=True)
            deadline = time.time() + 900
            while time.time() < deadline:
                page.wait_for_timeout(3000)
                if "/sign-in" in page.url:
                    continue
                texts = []
                for frame in page.frames:
                    try:
                        texts.append(frame.inner_text("body", timeout=2000))
                    except Exception:
                        pass
                body = "\n".join(texts)
                if "Sign in to marimo" in body:
                    continue
                if "/server" in page.url:
                    print("Đã đăng nhập (server mode).", flush=True)
                    return
                if has_editor(page) and "Read-only preview" not in body:
                    print("Đã đăng nhập (editor).", flush=True)
                    return
            raise RuntimeError("Hết thời gian đăng nhập (15 phút)")
        finally:
            context.close()


def get_vps_status_card(cfg, state):
    probe = (
        "echo GPU: $(nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader 2>/dev/null || echo 'N/A'); "
        "echo CPU: $(nproc) cores; "
        "echo RAM: $(free -h | awk '/^Mem:/ {print $3 \" / \" $2}'); "
        "echo DISK: $(df -h /home/marimo | awk 'NR==2 {print $3 \" / \" $2 \" (\" $5 \")\"}'); "
        "echo UP: $(uptime -p)"
    )
    try:
        raw = ssh_exec(state["last"], probe, timeout=25)
    except Exception as exc:
        raw = f"Không đọc được thông số: {exc}"
    info = {}
    for line in raw.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    rem_ssh = max(0, int((state["next_rerun"] - time.monotonic()) // 60))
    rem_ses = max(0, int((state["next_restart"] - time.monotonic()) // 60))
    return (
        "📊 <b>THÔNG SỐ HỆ THỐNG MOLAB VPS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎮 <b>GPU:</b> <code>{html.escape(info.get('GPU', 'N/A'))}</code>\n"
        f"🧠 <b>CPU:</b> <code>{html.escape(info.get('CPU', 'N/A'))}</code>\n"
        f"⚡ <b>RAM:</b> <code>{html.escape(info.get('RAM', 'N/A'))}</code>\n"
        f"💿 <b>Ổ cứng:</b> <code>{html.escape(info.get('DISK', 'N/A'))}</code>\n"
        f"⏳ <b>Uptime:</b> <code>{html.escape(info.get('UP', 'N/A'))}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔄 <b>Đổi cổng SSH sau:</b> {rem_ssh} phút <i>(chu kỳ {cfg['cell_rerun_minutes']}p)</i>\n"
        f"♻️ <b>Reset phiên sau:</b> {rem_ses} phút <i>(chu kỳ {cfg['session_restart_minutes']}p)</i>\n"
        f"☁️ <b>Kho lưu trữ:</b> <code>{html.escape(cfg.get('hf_repo', ''))}</code>"
    )


def handle_telegram_commands(cfg, page, state):
    global TG_OFFSET
    if not cfg.get("telegram_bot_token"):
        return
    res = tg_api(cfg, "getUpdates", {"offset": TG_OFFSET, "timeout": 0})
    for item in (res or {}).get("result") or []:
        TG_OFFSET = item["update_id"] + 1
        cb = item.get("callback_query")
        if cb:
            tg_api(cfg, "answerCallbackQuery", {"callback_query_id": cb["id"]})
            msg = cb.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id") or "").strip()
            text = (cb.get("data") or "").strip()
        else:
            msg = item.get("message") or item.get("edited_message") or {}
            chat_id = str((msg.get("chat") or {}).get("id") or "").strip()
            text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            continue
        allowed = get_allowed_ids(cfg)
        if chat_id not in allowed:
            send_telegram(
                cfg,
                "⛔ <b>TRUY CẬP BỊ TỪ CHỐI</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"ID Telegram của bạn: <code>{html.escape(chat_id)}</code>\n"
                f"Hãy gửi lệnh sau cho Admin để được cấp quyền:\n<code>/addid {html.escape(chat_id)}</code>",
                chat_id=chat_id,
                menu=False,
            )
            continue
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/start", "/help"):
            send_telegram(cfg, BOT_HELP, chat_id=chat_id)
        elif cmd == "/ssh":
            send_telegram(cfg, format_ssh_card(cfg, state["last"], state), chat_id=chat_id)
        elif cmd == "/status":
            send_telegram(cfg, get_vps_status_card(cfg, state), chat_id=chat_id)
        elif cmd == "/rotate":
            send_telegram(
                cfg,
                "⏳ <b>Đang khởi tạo cổng & mật khẩu SSH mới...</b>\n<i>Vui lòng đợi khoảng 10–70 giây.</i>",
                chat_id=chat_id,
                menu=False,
            )
            new = ensure_running(page, force_rerun=True)
            if new:
                state["last"] = new
                state["next_rerun"] = time.monotonic() + cfg["cell_rerun_minutes"] * 60
                publish(cfg, new, state)
            else:
                send_telegram(cfg, "❌ <b>Không tạo được cổng SSH mới.</b>", chat_id=chat_id)
        elif cmd == "/restart":
            send_telegram(
                cfg,
                "⏳ <b>Đang đồng bộ dữ liệu lên Hugging Face & khởi động lại phiên 12h...</b>",
                chat_id=chat_id,
                menu=False,
            )
            sync_remote(cfg, state["last"], mode="--push")
            page.reload(wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(5000)
            new = ensure_running(page)
            if new:
                now = time.monotonic()
                state["last"] = new
                state["next_restart"] = now + cfg["session_restart_minutes"] * 60
                state["next_rerun"] = now + cfg["cell_rerun_minutes"] * 60
                publish(cfg, new, state)
            else:
                send_telegram(cfg, "❌ <b>Reload xong nhưng chưa lấy được SSH.</b>", chat_id=chat_id)
        elif cmd == "/save":
            send_telegram(
                cfg,
                "⏳ <b>Đang đồng bộ <code>/home/marimo</code> lên Hugging Face...</b>",
                chat_id=chat_id,
                menu=False,
            )
            out = sync_remote(cfg, state["last"], mode="--push")
            send_telegram(
                cfg,
                "✅ <b>ĐỒNG BỘ HUGGING FACE HOÀN TẤT</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<pre>{html.escape(out or 'Đã lưu thành công.')}</pre>",
                chat_id=chat_id,
            )
        elif cmd in ("/sh", "/exec"):
            if not arg:
                send_telegram(
                    cfg,
                    "💻 <b>Chạy lệnh từ xa trên VPS</b>\nCách dùng: <code>/sh &lt;lệnh&gt;</code>\nVí dụ: <code>/sh fastfetch --pipe</code>",
                    chat_id=chat_id,
                )
                continue
            try:
                out = ssh_exec(state["last"], arg, timeout=120)
                send_telegram(
                    cfg,
                    f"💻 <b> lệnh:</b> <code>{html.escape(arg)}</code>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<pre>{html.escape((out or '(Hoàn tất, không có output)')[-3500:])}</pre>",
                    chat_id=chat_id,
                )
            except Exception as exc:
                send_telegram(cfg, f"❌ <b>Lỗi thực thi:</b> <code>{html.escape(str(exc))}</code>", chat_id=chat_id)
        elif cmd == "/pkgs":
            try:
                out = ssh_exec(
                    state["last"],
                    "cat /home/marimo/.packages.json 2>/dev/null || echo '{\"apt\": [], \"pip\": []}'",
                )
                data = json.loads(out)
                apt_s = ", ".join(f"<code>{html.escape(x)}</code>" for x in data.get("apt") or []) or "<i>(trống)</i>"
                pip_s = ", ".join(f"<code>{html.escape(x)}</code>" for x in data.get("pip") or []) or "<i>(trống)</i>"
                send_telegram(
                    cfg,
                    "📦 <b>DANH SÁCH GÓI TỰ CÀI LẠI SAU 12H</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔹 <b>Gói APT:</b> {apt_s}\n"
                    f"🔸 <b>Gói PIP:</b> {pip_s}\n\n"
                    "➕ Thêm gói: <code>/addpkg apt tên_gói</code>\n"
                    "➖ Xoá gói: <code>/delpkg tên_gói</code>",
                    chat_id=chat_id,
                )
            except Exception as exc:
                send_telegram(cfg, f"❌ <b>Lỗi đọc gói:</b> <code>{html.escape(str(exc))}</code>", chat_id=chat_id)
        elif cmd == "/addpkg":
            sub = arg.split()
            if len(sub) < 2 or sub[0] not in ("apt", "pip"):
                send_telegram(
                    cfg,
                    "➕ <b>Thêm & cài gói tự động</b>\nCách dùng:\n• <code>/addpkg apt htop tmux</code>\n• <code>/addpkg pip requests</code>",
                    chat_id=chat_id,
                )
                continue
            kind, names = sub[0], [x for x in sub[1:] if re.match(r"^[A-Za-z0-9_.+-]+$", x)]
            if not names:
                send_telegram(cfg, "❌ <b>Tên gói không hợp lệ.</b>", chat_id=chat_id)
                continue
            send_telegram(
                cfg,
                f"⏳ <b>Đang cài & lưu ({kind}):</b> <code>{' '.join(names)}</code>...",
                chat_id=chat_id,
                menu=False,
            )
            install_cmd = (
                f"DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq {' '.join(names)} && save"
                if kind == "apt"
                else f"pip install {' '.join(names)} && save"
            )
            try:
                out = ssh_exec(state["last"], install_cmd, timeout=180)
                send_telegram(
                    cfg,
                    f"✅ <b>ĐÃ CÀI & LƯU ({kind.upper()}):</b> <code>{' '.join(names)}</code>\n"
                    f"<pre>{html.escape(out[-800:])}</pre>",
                    chat_id=chat_id,
                )
            except Exception as exc:
                send_telegram(cfg, f"❌ <b>Lỗi:</b> <code>{html.escape(str(exc))}</code>", chat_id=chat_id)
        elif cmd == "/delpkg":
            names = [x for x in arg.split() if re.match(r"^[A-Za-z0-9_.+-]+$", x)]
            if not names:
                send_telegram(cfg, "➖ <b>Xoá gói:</b> <code>/delpkg tên_gói</code>", chat_id=chat_id)
                continue
            py = (
                "import json; p='/home/marimo/.packages.json'; "
                "d=json.load(open(p)) if __import__('os').path.exists(p) else {'apt':[],'pip':[]}; "
                f"rem={names!r}; "
                "d['apt']=[x for x in d.get('apt',[]) if x not in rem]; "
                "d['pip']=[x for x in d.get('pip',[]) if x not in rem]; "
                "json.dump(d,open(p,'w'),indent=2); print(json.dumps(d,indent=2))"
            )
            try:
                out = ssh_exec(state["last"], f"python3 -c {json.dumps(py)} && save", timeout=120)
                send_telegram(
                    cfg,
                    f"✅ <b>Đã xoá gói:</b> <code>{', '.join(names)}</code>\n<pre>{html.escape(out)}</pre>",
                    chat_id=chat_id,
                )
            except Exception as exc:
                send_telegram(cfg, f"❌ <b>Lỗi:</b> <code>{html.escape(str(exc))}</code>", chat_id=chat_id)
        elif cmd == "/set":
            nums = [int(x) for x in arg.split() if x.isdigit()]
            if not nums:
                send_telegram(
                    cfg,
                    "⚙️ <b>CẤU HÌNH CHU KỲ TỰ ĐỘNG</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Đổi cổng SSH:</b> <code>{cfg['cell_rerun_minutes']} phút</code>\n"
                    f"• <b>Reset phiên:</b> <code>{cfg['session_restart_minutes']} phút</code>\n\n"
                    "Đổi chu kỳ: <code>/set &lt;phút_ssh&gt; [phút_phiên]</code>",
                    chat_id=chat_id,
                )
                continue
            cfg["cell_rerun_minutes"] = max(5, min(58, nums[0]))
            if len(nums) > 1:
                cfg["session_restart_minutes"] = max(30, min(725, nums[1]))
            save_config(cfg)
            now = time.monotonic()
            state["next_rerun"] = now + cfg["cell_rerun_minutes"] * 60
            state["next_restart"] = now + cfg["session_restart_minutes"] * 60
            send_telegram(
                cfg,
                f"✅ <b>Đã lưu chu kỳ mới:</b>\n• Đổi cổng SSH: <code>{cfg['cell_rerun_minutes']} phút</code>\n• Reset phiên: <code>{cfg['session_restart_minutes']} phút</code>",
                chat_id=chat_id,
            )
        elif cmd == "/ids":
            rows = "\n".join(
                f"• <code>{html.escape(i)}</code> {'👑 <i>(Admin gốc)</i>' if i == ADMIN_ID else '👤'}"
                for i in allowed
            )
            send_telegram(
                cfg,
                "👥 <b>DANH SÁCH ID ĐƯỢC CẤP QUYỀN</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{rows}\n\n"
                "➕ Thêm: <code>/addid &lt;id&gt;</code>  |  ➖ Xoá: <code>/delid &lt;id&gt;</code>",
                chat_id=chat_id,
            )
        elif cmd == "/addid":
            new_id = arg.strip()
            if not re.match(r"^-?\d+$", new_id):
                send_telegram(cfg, "👤 <b>Cấp quyền ID mới:</b> <code>/addid 123456789</code>", chat_id=chat_id)
                continue
            if new_id not in allowed:
                allowed.append(new_id)
                cfg["telegram_allowed_ids"] = allowed
                save_config(cfg)
            send_telegram(cfg, f"✅ <b>Đã cấp quyền cho ID:</b> <code>{html.escape(new_id)}</code>", chat_id=chat_id)
            if new_id != chat_id and state.get("last"):
                send_telegram(
                    cfg,
                    format_ssh_card(
                        cfg,
                        state["last"],
                        state,
                        title="🎉 <b>BẠN ĐÃ ĐƯỢC CẤP QUYỀN MOLAB VPS</b>",
                    ),
                    chat_id=new_id,
                )
        elif cmd == "/delid":
            rem_id = arg.strip()
            if rem_id == ADMIN_ID:
                send_telegram(cfg, f"⛔ <b>Không thể xoá ID Admin gốc (<code>{ADMIN_ID}</code>).</b>", chat_id=chat_id)
                continue
            if rem_id in allowed:
                allowed.remove(rem_id)
                cfg["telegram_allowed_ids"] = allowed
                save_config(cfg)
                send_telegram(cfg, f"✅ <b>Đã thu hồi quyền của ID:</b> <code>{html.escape(rem_id)}</code>", chat_id=chat_id)
            else:
                send_telegram(cfg, f"⚠️ Không tìm thấy ID <code>{html.escape(rem_id)}</code>.", chat_id=chat_id)


def session(cfg):
    with sync_playwright() as playwright:
        context = open_context(playwright, cfg, headless=cfg["headless"])
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(cfg["notebook_url"], wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(5000)

            result = ensure_running(page)
            if not result:
                page.screenshot(path=str(ROOT / "error.png"), full_page=True)
                raise RuntimeError("Không chạy được cell SSH (xem error.png)")
            now = time.monotonic()
            state = {
                "last": result,
                "next_rerun": now + cfg["cell_rerun_minutes"] * 60,
                "next_restart": now + cfg["session_restart_minutes"] * 60,
                "next_heartbeat": now + 30,
            }
            publish(cfg, result, state)

            while True:
                page.wait_for_timeout(2000)
                handle_telegram_commands(cfg, page, state)
                now = time.monotonic()
                if now >= state["next_heartbeat"]:
                    page.mouse.move(5, 5)  # giả lập hoạt động, tránh bị coi là idle
                    page.evaluate("1")  # heartbeat giữ trang sống
                    state["next_heartbeat"] = now + 30

                if now >= state["next_restart"]:
                    logging.info("Reload notebook (session 12h)")
                    sync_remote(cfg, state["last"], mode="--push")
                    page.reload(wait_until="domcontentloaded", timeout=120000)
                    page.wait_for_timeout(5000)
                    new = ensure_running(page)
                    state["next_restart"] = now + cfg["session_restart_minutes"] * 60
                    state["next_rerun"] = now + cfg["cell_rerun_minutes"] * 60
                    if new:
                        if new != state["last"]:
                            publish(cfg, new, state)
                        state["last"] = new
                    else:
                        page.screenshot(path=str(ROOT / "error.png"), full_page=True)
                        logging.error("Reload xong nhưng không thấy SSH")
                    continue

                if now >= state["next_rerun"]:
                    logging.info("Rerun cell (tunnel Pinggy 60 phút)")
                    new = ensure_running(page, force_rerun=True)
                    state["next_rerun"] = now + cfg["cell_rerun_minutes"] * 60
                    if new:
                        publish(cfg, new, state)
                        state["last"] = new
                    else:
                        page.screenshot(path=str(ROOT / "error.png"), full_page=True)
                        logging.error("Rerun không ra địa chỉ mới")
        finally:
            context.close()


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = argparse.ArgumentParser()
    parser.add_argument("--login", action="store_true", help="đăng nhập molab một lần")
    parser.add_argument("--once", action="store_true", help="mở, chạy cell, in địa chỉ rồi thoát")
    parser.add_argument("--headful", action="store_true", help="hiện cửa sổ browser")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config()
    init_telegram_bot(cfg)
    if args.headful:
        cfg["headless"] = False

    if args.login:
        login(cfg)
        return

    if args.once:
        with sync_playwright() as playwright:
            context = open_context(playwright, cfg, headless=cfg["headless"])
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(cfg["notebook_url"], wait_until="domcontentloaded", timeout=120000)
                page.wait_for_timeout(5000)
                result = ensure_running(page)
                if not result:
                    page.screenshot(path=str(ROOT / "error.png"), full_page=True)
                    raise RuntimeError("Không thấy dòng SSH (xem error.png)")
                publish(cfg, result)
            finally:
                context.close()
        return

    while True:
        try:
            session(cfg)
        except Exception as exc:
            logging.exception("Session lỗi: %s", exc)
            send_telegram(cfg, f"molab keepalive lỗi: {exc}")
            time.sleep(300)


if __name__ == "__main__":
    main()
