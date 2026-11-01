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
    cfg.setdefault("users", {})
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

ADMIN_KEYBOARD = {
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
            {"text": "🖥 Quản lý VPS User", "callback_data": "/vps"},
        ],
        [
            {"text": "🚀 Tạo VPS mới", "callback_data": "/newvps"},
            {"text": "👥 Quản lý ID", "callback_data": "/ids"},
        ],
        [
            {"text": "♻️ Reset phiên 12h", "callback_data": "/restart"},
            {"text": "📖 Bảng lệnh", "callback_data": "/help"},
        ],
    ]
}

USER_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "🔗 Liên kết Molab", "callback_data": "/login"},
            {"text": "🚀 Tạo / Bật VPS", "callback_data": "/newvps"},
        ],
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
            {"text": "♻️ Reset phiên 12h", "callback_data": "/restart"},
        ],
        [
            {"text": "🛑 Tắt VPS", "callback_data": "/stopvps"},
            {"text": "📖 Bảng lệnh", "callback_data": "/help"},
        ],
    ]
}

MAIN_KEYBOARD = ADMIN_KEYBOARD


def get_bot_help(is_admin=False):
    base = (
        "⚡ <b>MOLAB CLOUD VPS — BẢNG ĐIỀU KHIỂN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔗 <b>Tài khoản & Khởi tạo VPS riêng</b>\n"
        "• <code>/login email@gmail.com</code> — Liên kết tài khoản Molab (Google)\n"
        "• <code>/code 123456</code> — Nhập mã 6 số gửi về Gmail\n"
        "• <code>/cookie &lt;__client&gt;</code> — Liên kết nhanh bằng cookie phiên\n"
        "• <code>/newvps</code> — Tự động tạo & bật VPS GPU RTX Pro 6000 riêng\n"
        "• <code>/stopvps</code> — Tạm tắt VPS của bạn\n\n"
        "🖥 <b>Kết nối & Phiên</b>\n"
        "• <code>/ssh</code> — Lấy lệnh SSH, mật khẩu & trạng thái\n"
        "• <code>/status</code> — Xem GPU, VRAM, RAM, ổ cứng & Uptime\n"
        "• <code>/rotate</code> — Đổi cổng & mật khẩu SSH mới ngay\n"
        "• <code>/restart</code> — Lưu dữ liệu & tạo phiên 12h mới\n\n"
        "💾 <b>Lưu trữ & Gói cài đặt</b>\n"
        "• <code>/save</code> — Đồng bộ ngay lên Hugging Face\n"
        "• <code>/pkgs</code> — Xem các gói <code>apt</code> / <code>pip</code> tự cài\n"
        "• <code>/addpkg apt tên_gói</code> — Cài & nhớ gói mới\n"
        "• <code>/delpkg tên_gói</code> — Xoá gói khỏi danh sách nhớ\n"
        "• <code>/sh lệnh</code> — Chạy lệnh trực tiếp trên VPS\n"
    )
    if is_admin:
        base += (
            "\n👑 <b>Quyền Quản trị viên (Admin)</b>\n"
            "• <code>/vps</code> — Xem & điều khiển toàn bộ VPS của mọi ID\n"
            "• <code>/use &lt;id&gt;</code> — Chuyển quyền điều khiển sang VPS của ID\n"
            "• <code>/newvps &lt;id&gt;</code> | <code>/stopvps &lt;id&gt;</code> — Tạo/tắt VPS cho ID\n"
            "• <code>/ids</code> | <code>/addid &lt;id&gt;</code> | <code>/delid &lt;id&gt;</code> — Phân quyền ID\n"
            "• <code>/set 55 725</code> — Chỉnh chu kỳ xoay (phút)\n"
        )
    return (
        base
        + "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <i>Chạm trực tiếp vào lệnh SSH hoặc mật khẩu để copy 1 chạm, hoặc bấm các nút bên dưới:</i>"
    )


def save_config(cfg):
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def get_user_cfg(cfg, uid):
    uid = str(uid).strip()
    if uid == ADMIN_ID:
        return {
            "profile_dir": cfg["profile_dir"],
            "notebook_url": cfg["notebook_url"],
            "hf_repo": cfg.get("hf_repo", ""),
            "output_file": cfg["output_file"],
            "logged_in": True,
            "email": "Admin (mặc định)",
        }
    users = cfg.setdefault("users", {})
    base_hf = cfg.get("hf_repo") or "lesslosster/molab-backup"
    u = users.setdefault(uid, {})
    changed = False
    defaults = {
        "profile_dir": f"profiles/{uid}",
        "notebook_url": "",
        "hf_repo": f"{base_hf}-{uid}",
        "output_file": f"last_ssh_{uid}.txt",
        "logged_in": False,
        "email": "",
    }
    for k, v in defaults.items():
        if k not in u:
            u[k] = v
            changed = True
    if changed:
        save_config(cfg)
    return u


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


def send_telegram(cfg, text, chat_id=None, html_mode=True, menu=True, reply_markup=None):
    targets = [str(chat_id)] if chat_id else [ADMIN_ID]
    for target in targets:
        payload = {
            "chat_id": target,
            "text": text[:4000],
            "disable_web_page_preview": "true",
        }
        if html_mode:
            payload["parse_mode"] = "HTML"
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        elif menu:
            kb = ADMIN_KEYBOARD if str(target) == ADMIN_ID else USER_KEYBOARD
            payload["reply_markup"] = json.dumps(kb)
        tg_api(cfg, "sendMessage", payload)


def format_ssh_card(cfg, result, state=None, uid=ADMIN_ID, title="🔑 <b>THÔNG TIN KẾT NỐI SSH</b>"):
    u = get_user_cfg(cfg, uid)
    if not result:
        return f"{title}\n━━━━━━━━━━━━━━━━━━━━━━\n⚠️ <b>VPS của ID <code>{html.escape(str(uid))}</code> chưa có kết nối SSH.</b>"
    ssh_line, password = result
    alive = "🟢 Đang hoạt động" if tunnel_alive(result) else "🔴 Mất kết nối"
    timers = ""
    if state:
        rem_ssh = max(0, int((state["next_rerun"] - time.monotonic()) // 60))
        rem_ses = max(0, int((state["next_restart"] - time.monotonic()) // 60))
        timers = f"⏱ <b>Đổi cổng sau:</b> {rem_ssh} phút  |  <b>Reset phiên:</b> {rem_ses} phút\n"
    repo = u.get("hf_repo") or "chưa cấu hình"
    owner_line = f"👤 <b>Chủ VPS (ID):</b> <code>{html.escape(str(uid))}</code> ({html.escape(u.get('email') or 'Chưa rõ')})\n"
    return (
        f"{title}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{owner_line}"
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
        {"command": "newvps", "description": "🚀 Tạo / khởi động VPS GPU riêng"},
        {"command": "login", "description": "🔗 Liên kết acc Molab (Google): /login <email>"},
        {"command": "code", "description": "🔢 Nhập mã 6 số từ Gmail: /code <mã>"},
        {"command": "rotate", "description": "🔄 Đổi cổng & mật khẩu SSH mới ngay"},
        {"command": "save", "description": "💾 Đồng bộ dữ liệu lên Hugging Face ngay"},
        {"command": "pkgs", "description": "📦 Danh sách gói apt/pip tự cài"},
        {"command": "addpkg", "description": "➕ Cài & lưu gói: /addpkg <apt|pip> <tên>"},
        {"command": "delpkg", "description": "➖ Xoá gói tự cài: /delpkg <tên>"},
        {"command": "sh", "description": "💻 Chạy lệnh bash: /sh <lệnh>"},
        {"command": "vps", "description": "🖥 (Admin) Quản lý toàn bộ VPS các User"},
        {"command": "use", "description": "🎯 (Admin) Chọn VPS cần điều khiển: /use <id>"},
        {"command": "ids", "description": "👥 (Admin) Xem danh sách ID có quyền"},
        {"command": "addid", "description": "👤 (Admin) Cấp quyền ID mới: /addid <id>"},
        {"command": "delid", "description": "🚫 (Admin) Xoá quyền ID: /delid <id>"},
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


def sync_remote(cfg, result, mode="--start-daemon", uid=ADMIN_ID):
    u = get_user_cfg(cfg, uid)
    hf_repo = u.get("hf_repo") or cfg.get("hf_repo")
    if not result or not cfg.get("hf_token") or not hf_repo:
        return
    remote_cfg = json.dumps(
        {
            "hf_token": cfg["hf_token"],
            "hf_repo": hf_repo,
            "telegram_bot_token": cfg.get("telegram_bot_token", ""),
            "telegram_chat_id": str(uid),
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
            logging.info("HF sync [%s] (%s): %s", uid, mode, out)
        return out
    except Exception as exc:
        logging.warning("Không cài/chạy được HF sync [%s] qua SSH: %s", uid, exc)
        return f"Lỗi: {exc}"


def publish(cfg, result, state=None, uid=ADMIN_ID, notify_chat_id=None):
    u = get_user_cfg(cfg, uid)
    ssh_line, password = result
    text = f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n{ssh_line}\npassword: {password}\n"
    path = ROOT / u["output_file"]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    logging.info("Địa chỉ mới [%s]:\n%s", uid, text.strip())
    card = format_ssh_card(cfg, result, state, uid=uid)
    send_telegram(cfg, card, chat_id=uid)
    if notify_chat_id and str(notify_chat_id) != str(uid):
        send_telegram(cfg, card, chat_id=notify_chat_id)
    sync_remote(cfg, result, mode="--start-daemon", uid=uid)


def open_context(playwright, cfg, headless, profile_dir=None):
    profile = ROOT / (profile_dir or cfg["profile_dir"])
    profile.mkdir(parents=True, exist_ok=True)
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


def replace_cell_code(page, code_str):
    frame = wait_editor_frame(page, timeout=60)
    if frame is None:
        return False
    editor = frame.locator(".cm-content").first
    has_cmview = editor.evaluate("el => !!(el.cmView && el.cmView.view)")
    if has_cmview:
        editor.evaluate(
            """(el, code) => {
                const view = el.cmView.view;
                view.dispatch({
                    changes: { from: 0, to: view.state.doc.length, insert: code }
                });
            }""",
            code_str,
        )
    else:
        editor.click(position={"x": 80, "y": 20})
        page.keyboard.press("Control+a")
        page.keyboard.press("Backspace")
        page.keyboard.insert_text(code_str)
    page.wait_for_timeout(1000)
    save_btn = frame.get_by_test_id("save-button")
    if save_btn.count():
        save_btn.first.click()
        page.wait_for_timeout(2000)
    return True


def ensure_gpu_compute(page):
    try:
        btn = page.locator("button[title='Configure compute']").first
        if not btn.count():
            return
        label = (btn.inner_text(timeout=3000) or "").strip()
        if "RTX Pro 6000" in label:
            return
        btn.click()
        page.wait_for_timeout(1500)
        combos = page.locator("[role='dialog'] button[role='combobox']")
        if combos.count():
            combos.last.click()
            page.wait_for_timeout(1000)
            opt = page.locator("[role='option']", has_text=re.compile("RTX Pro 6000", re.I))
            if opt.count():
                opt.first.click()
            else:
                page.keyboard.press("ArrowDown")
                page.keyboard.press("Enter")
            page.wait_for_timeout(1000)
        save_btn = page.locator("[role='dialog'] button", has_text=re.compile("Save and restart", re.I))
        if save_btn.count():
            save_btn.first.click()
            page.wait_for_timeout(8000)
    except Exception as exc:
        logging.warning("Không đổi được cấu hình GPU: %s", exc)


def close_pending_login(uid, pending_logins):
    item = pending_logins.pop(str(uid), None)
    if item:
        try:
            item["context"].close()
        except Exception:
            pass


def start_molab_email_login(playwright, cfg, uid, email, pending_logins, active_sessions):
    uid = str(uid)
    if uid in active_sessions:
        stop_user_vps(uid, active_sessions)
    close_pending_login(uid, pending_logins)
    u = get_user_cfg(cfg, uid)
    ctx = open_context(playwright, cfg, headless=cfg["headless"], profile_dir=u["profile_dir"])
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        page.goto("https://molab.marimo.io/sign-in", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        if "/sign-in" not in page.url:
            u["logged_in"] = True
            u["email"] = email
            save_config(cfg)
            ctx.close()
            return True, f"✅ Tài khoản Molab (<code>{html.escape(email)}</code>) đã được đăng nhập sẵn! Bấm <b>🚀 Tạo / Bật VPS</b> để khởi tạo máy."
        inp = page.locator("input[name='identifier']").first
        inp.wait_for(state="visible", timeout=15000)
        inp.fill(email)
        page.locator("button.cl-formButtonPrimary").first.click()
        page.wait_for_timeout(3500)
        body = page.inner_text("body")
        if page.locator("input[autocomplete='one-time-code']").count() or "Check your email" in body:
            pending_logins[uid] = {"context": ctx, "page": page, "email": email, "ts": time.time()}
            return (
                True,
                "📧 <b>ĐÃ GỬI MÃ XÁC THỰC 6 SỐ VỀ GMAIL</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"• <b>Email:</b> <code>{html.escape(email)}</code>\n"
                "• Hãy mở hộp thư Gmail nhận mã 6 số từ <b>marimo</b> rồi gõ:\n"
                "<code>/code 123456</code>",
            )
        ctx.close()
        return (
            False,
            "⚠️ <b>Không gửi được mã OTP cho email này.</b>\n"
            "Hãy đảm bảo bạn đã từng bấm <b>Continue with Google</b> trên <a href='https://molab.marimo.io/sign-in'>molab.marimo.io</a> bằng Gmail này trước đó, hoặc dùng lệnh <code>/cookie &lt;__client&gt;</code>.",
        )
    except Exception as exc:
        try:
            ctx.close()
        except Exception:
            pass
        return False, f"❌ <b>Lỗi khởi tạo đăng nhập:</b> <code>{html.escape(str(exc))}</code>"


def verify_molab_email_code(cfg, uid, code, pending_logins):
    uid = str(uid)
    item = pending_logins.get(uid)
    if not item:
        return False, "⚠️ Chưa có phiên đăng nhập nào đang chờ mã. Hãy gõ <code>/login email@gmail.com</code> trước."
    page = item["page"]
    email = item["email"]
    try:
        otp = page.locator("input[autocomplete='one-time-code']").first
        otp.click()
        otp.fill("")
        page.keyboard.type(code, delay=60)
        page.wait_for_timeout(2000)
        btn = page.locator("button.cl-formButtonPrimary")
        if btn.count() and btn.first.is_visible():
            try:
                btn.first.click(timeout=3000)
            except Exception:
                pass
        for _ in range(10):
            page.wait_for_timeout(1500)
            if page.locator("input[name='firstName']").count():
                page.locator("input[name='firstName']").first.fill("Molab")
                if page.locator("input[name='lastName']").count():
                    page.locator("input[name='lastName']").first.fill("User")
                page.locator("button.cl-formButtonPrimary").first.click()
                page.wait_for_timeout(2500)
            if "/sign-in" not in page.url and "/sign-up" not in page.url:
                u = get_user_cfg(cfg, uid)
                u["logged_in"] = True
                u["email"] = email
                save_config(cfg)
                close_pending_login(uid, pending_logins)
                return (
                    True,
                    "✅ <b>LIÊN KẾT TÀI KHOẢN MOLAB THÀNH CÔNG!</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Tài khoản:</b> <code>{html.escape(email)}</code>\n"
                    "• Giờ bạn có thể bấm nút <b>🚀 Tạo / Bật VPS</b> (hoặc gõ <code>/newvps</code>) để khởi tạo VPS GPU RTX Pro 6000 riêng!",
                )
        return False, "❌ <b>Mã xác thực không đúng hoặc đã hết hạn.</b> Vui lòng kiểm tra lại và gõ <code>/code &lt;6_số&gt;</code>."
    except Exception as exc:
        close_pending_login(uid, pending_logins)
        return False, f"❌ <b>Lỗi xác thực mã:</b> <code>{html.escape(str(exc))}</code>"


def link_molab_cookie(playwright, cfg, uid, cookie_val, pending_logins, active_sessions):
    uid = str(uid)
    if uid in active_sessions:
        stop_user_vps(uid, active_sessions)
    close_pending_login(uid, pending_logins)
    u = get_user_cfg(cfg, uid)
    ctx = open_context(playwright, cfg, headless=cfg["headless"], profile_dir=u["profile_dir"])
    try:
        ctx.add_cookies(
            [
                {
                    "name": "__client",
                    "value": cookie_val.strip(),
                    "domain": ".clerk.marimo.io",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ]
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://molab.marimo.io/notebooks", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        body = page.inner_text("body")
        if "/sign-in" not in page.url and "Sign in to marimo" not in body:
            u["logged_in"] = True
            if not u.get("email"):
                u["email"] = "Google/Cookie Linked"
            save_config(cfg)
            return (
                True,
                "✅ <b>LIÊN KẾT COOKIE MOLAB THÀNH CÔNG!</b>\n"
                "Bấm nút <b>🚀 Tạo / Bật VPS</b> (hoặc gõ <code>/newvps</code>) để tạo VPS GPU riêng của bạn.",
            )
        return False, "❌ <b>Cookie <code>__client</code> không hợp lệ hoặc đã hết hạn.</b>"
    finally:
        ctx.close()


def stop_user_vps(uid, active_sessions):
    uid = str(uid)
    st = active_sessions.pop(uid, None)
    if not st:
        return False
    try:
        st["context"].close()
    except Exception:
        pass
    return True


def provision_or_start_user_vps(
    playwright, cfg, uid, active_sessions, pending_logins, force_new_notebook=False, notify_chat_id=None
):
    uid = str(uid)
    u = get_user_cfg(cfg, uid)
    if uid != ADMIN_ID and not u.get("logged_in"):
        return (
            False,
            "🔒 <b>CHƯA LIÊN KẾT TÀI KHOẢN MOLAB (GOOGLE)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "Để có VPS GPU RTX Pro 6000 riêng, bạn cần liên kết tài khoản Molab (đăng ký bằng Google) của mình:\n\n"
            "1️⃣ Đăng nhập Google một lần trên <a href='https://molab.marimo.io/sign-in'>molab.marimo.io/sign-in</a>\n"
            "2️⃣ Gõ tại đây: <code>/login email_cua_ban@gmail.com</code>\n"
            "3️⃣ Nhập mã 6 số gửi về Gmail: <code>/code 123456</code>\n\n"
            "Sau khi liên kết xong, bấm lại <b>🚀 Tạo / Bật VPS</b>!",
        )

    close_pending_login(uid, pending_logins)
    stop_user_vps(uid, active_sessions)

    ctx = open_context(playwright, cfg, headless=cfg["headless"], profile_dir=u["profile_dir"])
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        need_provision = force_new_notebook or not u.get("notebook_url")
        if need_provision:
            page.goto(cfg["notebook_url"], wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(8000)
            body = page.inner_text("body")
            if "/sign-in" in page.url or "Sign in to marimo" in body:
                u["logged_in"] = False
                save_config(cfg)
                ctx.close()
                return (
                    False,
                    "⚠️ <b>Phiên đăng nhập Molab đã hết hạn.</b>\n"
                    "Hãy liên kết lại bằng lệnh: <code>/login email@gmail.com</code>",
                )
            dup_btn = page.locator("button[title='Duplicate notebook'], button:has-text('Duplicate')").first
            if not dup_btn.count():
                ctx.close()
                return False, "❌ <b>Không tìm thấy nút Duplicate notebook trên Molab.</b>"
            old_url = page.url.split("?")[0]
            dup_btn.click()
            deadline = time.time() + 45
            while time.time() < deadline:
                page.wait_for_timeout(2000)
                cur_url = page.url.split("?")[0]
                if "/notebooks/nb_" in cur_url and cur_url != old_url:
                    break
            else:
                ctx.close()
                return False, "❌ <b>Không tạo được notebook mới (quá thời gian Duplicate).</b>"

            u["notebook_url"] = page.url.split("?")[0]
            if uid == ADMIN_ID:
                cfg["notebook_url"] = u["notebook_url"]
            save_config(cfg)

            page.wait_for_timeout(6000)
            take_over(page)
            ensure_gpu_compute(page)
            if uid != ADMIN_ID:
                cell_code = (ROOT / "molab_ssh_pinggy.py").read_text(encoding="utf-8")
                cell_code = re.sub(r'TOKEN\s*=\s*"[^"]*"', 'TOKEN = ""', cell_code)
                replace_cell_code(page, cell_code)
        else:
            page.goto(u["notebook_url"], wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(5000)

        result = ensure_running(page, force_rerun=need_provision)
        if not result:
            page.screenshot(path=str(ROOT / "error.png"), full_page=True)
            ctx.close()
            return False, "❌ <b>Không khởi chạy được cell SSH trên notebook.</b>"

        now = time.monotonic()
        st = {
            "uid": uid,
            "context": ctx,
            "page": page,
            "last": result,
            "next_rerun": now + cfg["cell_rerun_minutes"] * 60,
            "next_restart": now + cfg["session_restart_minutes"] * 60,
            "next_heartbeat": now + 30,
        }
        active_sessions[uid] = st
        publish(cfg, result, st, uid=uid, notify_chat_id=notify_chat_id)
        return True, f"✅ <b>VPS của ID <code>{html.escape(uid)}</code> đã sẵn sàng!</b>"
    except Exception as exc:
        try:
            ctx.close()
        except Exception:
            pass
        return False, f"❌ <b>Lỗi khởi tạo VPS:</b> <code>{html.escape(str(exc))}</code>"


def get_vps_status_card(cfg, state, uid=ADMIN_ID):
    u = get_user_cfg(cfg, uid)
    if not state or not state.get("last"):
        return (
            "📊 <b>THÔNG SỐ HỆ THỐNG MOLAB VPS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Chủ VPS (ID):</b> <code>{html.escape(str(uid))}</code>\n"
            "📡 <b>Trạng thái:</b> ⚪ Chưa bật (Bấm <b>🚀 Tạo / Bật VPS</b> để chạy)"
        )
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
        f"👤 <b>Chủ VPS (ID):</b> <code>{html.escape(str(uid))}</code> ({html.escape(u.get('email') or 'Chưa rõ')})\n"
        f"📓 <b>Notebook:</b> <code>{html.escape(u.get('notebook_url') or 'Chưa tạo')}</code>\n"
        f"🎮 <b>GPU:</b> <code>{html.escape(info.get('GPU', 'N/A'))}</code>\n"
        f"🧠 <b>CPU:</b> <code>{html.escape(info.get('CPU', 'N/A'))}</code>\n"
        f"⚡ <b>RAM:</b> <code>{html.escape(info.get('RAM', 'N/A'))}</code>\n"
        f"💿 <b>Ổ cứng:</b> <code>{html.escape(info.get('DISK', 'N/A'))}</code>\n"
        f"⏳ <b>Uptime:</b> <code>{html.escape(info.get('UP', 'N/A'))}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔄 <b>Đổi cổng SSH sau:</b> {rem_ssh} phút <i>(chu kỳ {cfg['cell_rerun_minutes']}p)</i>\n"
        f"♻️ <b>Reset phiên sau:</b> {rem_ses} phút <i>(chu kỳ {cfg['session_restart_minutes']}p)</i>\n"
        f"☁️ <b>Kho lưu trữ:</b> <code>{html.escape(u.get('hf_repo', ''))}</code>"
    )


def format_all_vps_card(cfg, active_sessions, admin_target):
    allowed = get_allowed_ids(cfg)
    blocks = []
    buttons = []
    for uid in allowed:
        u = get_user_cfg(cfg, uid)
        st = active_sessions.get(uid)
        running = "🟢 Đang chạy" if st and st.get("last") else "⚪ Đang tắt"
        linked = f"✅ <code>{html.escape(u.get('email') or 'Đã liên kết')}</code>" if u.get("logged_in") else "❌ Chưa liên kết Molab"
        nb = u.get("notebook_url") or "(Chưa tạo)"
        role = "👑 Admin" if uid == ADMIN_ID else "👤 User"
        sel = " 🎯 <b>[ĐANG CHỌN]</b>" if uid == admin_target else ""
        ssh_str = f"\n   • SSH: <code>{html.escape(st['last'][0])}</code> (pass: <code>{html.escape(st['last'][1])}</code>)" if st and st.get("last") else ""
        blocks.append(
            f"🔹 <b>ID:</b> <code>{html.escape(uid)}</code> ({role}){sel}\n"
            f"   • Trạng thái: {running} | Molab: {linked}\n"
            f"   • Notebook: <code>{html.escape(nb)}</code>"
            f"{ssh_str}"
        )
        row = [{"text": f"🎯 Chọn {uid}", "callback_data": f"/use {uid}"}]
        if st:
            row.append({"text": f"🛑 Tắt {uid}", "callback_data": f"/stopvps {uid}"})
        else:
            row.append({"text": f"🚀 Bật {uid}", "callback_data": f"/newvps {uid}"})
        buttons.append(row)
    buttons.append([{"text": "🔙 Về bảng điều khiển chính", "callback_data": "/help"}])
    text = (
        "🖥 <b>QUẢN LÝ TOÀN BỘ VPS NGƯỜI DÙNG</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        + "\n\n".join(blocks)
        + "\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>VPS đang điều khiển:</b> <code>{html.escape(admin_target)}</code>\n"
        "💡 <i>Bấm nút bên dưới để chuyển điều khiển, bật hoặc tắt VPS của từng ID:</i>"
    )
    return text, {"inline_keyboard": buttons}


def handle_telegram_commands(playwright, cfg, active_sessions, pending_logins, admin_state):
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

        is_admin = chat_id == ADMIN_ID
        target_uid = admin_state.get("target", ADMIN_ID) if is_admin else chat_id
        if target_uid not in allowed:
            target_uid = ADMIN_ID
            admin_state["target"] = ADMIN_ID
        state = active_sessions.get(target_uid)
        target_banner = (
            f"🎯 <i>[Đang điều khiển VPS của ID: <code>{html.escape(target_uid)}</code> — gõ <code>/use me</code> để về VPS của bạn]</i>\n\n"
            if is_admin and target_uid != ADMIN_ID
            else ""
        )

        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/start", "/help"):
            send_telegram(cfg, target_banner + get_bot_help(is_admin=is_admin), chat_id=chat_id)
        elif cmd == "/login":
            login_uid = target_uid if is_admin else chat_id
            if not arg or "@" not in arg:
                u = get_user_cfg(cfg, login_uid)
                status_line = (
                    f"✅ Đã liên kết: <code>{html.escape(u.get('email') or 'Đã đăng nhập')}</code>"
                    if u.get("logged_in")
                    else "❌ Chưa liên kết"
                )
                send_telegram(
                    cfg,
                    target_banner
                    + "🔗 <b>LIÊN KẾT TÀI KHOẢN MOLAB (GOOGLE)</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Trạng thái (ID <code>{html.escape(login_uid)}</code>):</b> {status_line}\n\n"
                    "Để liên kết tài khoản Molab (đăng ký bằng Google) của bạn:\n"
                    "1️⃣ Gõ: <code>/login email_cua_ban@gmail.com</code>\n"
                    "2️⃣ Mở Gmail lấy mã 6 số từ marimo và gõ: <code>/code 123456</code>\n\n"
                    "<i>(Hoặc nâng cao: dán cookie bằng <code>/cookie &lt;giá_trị___client&gt;</code>)</i>",
                    chat_id=chat_id,
                )
                continue
            send_telegram(
                cfg,
                f"⏳ <b>Đang gửi yêu cầu mã xác thực tới <code>{html.escape(arg)}</code>...</b>",
                chat_id=chat_id,
                menu=False,
            )
            _, msg_out = start_molab_email_login(playwright, cfg, login_uid, arg, pending_logins, active_sessions)
            send_telegram(cfg, target_banner + msg_out, chat_id=chat_id)
        elif cmd == "/code":
            login_uid = target_uid if is_admin else chat_id
            code = re.sub(r"\D", "", arg)
            if len(code) != 6:
                send_telegram(cfg, "🔢 <b>Cách dùng:</b> <code>/code 123456</code> (6 chữ số trong Gmail)", chat_id=chat_id)
                continue
            send_telegram(cfg, "⏳ <b>Đang kiểm tra mã xác thực 6 số...</b>", chat_id=chat_id, menu=False)
            _, msg_out = verify_molab_email_code(cfg, login_uid, code, pending_logins)
            send_telegram(cfg, target_banner + msg_out, chat_id=chat_id)
        elif cmd == "/cookie":
            login_uid = target_uid if is_admin else chat_id
            if not arg:
                send_telegram(cfg, "🍪 <b>Cách dùng:</b> <code>/cookie &lt;giá_trị_cookie___client&gt;</code>", chat_id=chat_id)
                continue
            send_telegram(cfg, "⏳ <b>Đang kiểm tra cookie Molab...</b>", chat_id=chat_id, menu=False)
            _, msg_out = link_molab_cookie(playwright, cfg, login_uid, arg, pending_logins, active_sessions)
            send_telegram(cfg, target_banner + msg_out, chat_id=chat_id)
        elif cmd == "/newvps":
            req_uid = arg.strip() if (is_admin and re.match(r"^-?\d+$", arg.strip())) else target_uid
            u = get_user_cfg(cfg, req_uid)
            force_new = arg.strip().lower() == "new" or not u.get("notebook_url")
            send_telegram(
                cfg,
                f"⏳ <b>Đang {'khởi tạo notebook GPU mới & ' if force_new else ''}bật VPS cho ID <code>{html.escape(req_uid)}</code>...</b>\n"
                "<i>Vui lòng đợi khoảng 20–90 giây.</i>",
                chat_id=chat_id,
                menu=False,
            )
            ok, msg_out = provision_or_start_user_vps(
                playwright,
                cfg,
                req_uid,
                active_sessions,
                pending_logins,
                force_new_notebook=force_new,
                notify_chat_id=chat_id,
            )
            if not ok:
                send_telegram(cfg, target_banner + msg_out, chat_id=chat_id)
        elif cmd == "/stopvps":
            req_uid = arg.strip() if (is_admin and re.match(r"^-?\d+$", arg.strip())) else target_uid
            st = active_sessions.get(req_uid)
            if st and st.get("last"):
                sync_remote(cfg, st["last"], mode="--push", uid=req_uid)
            if stop_user_vps(req_uid, active_sessions):
                send_telegram(cfg, f"🛑 <b>Đã lưu dữ liệu & tắt VPS của ID <code>{html.escape(req_uid)}</code>.</b>", chat_id=chat_id)
            else:
                send_telegram(cfg, f"⚪ <b>VPS của ID <code>{html.escape(req_uid)}</code> hiện đang tắt.</b>", chat_id=chat_id)
        elif cmd == "/vps":
            if not is_admin:
                send_telegram(cfg, "⛔ <b>Chỉ Admin mới có quyền quản lý toàn bộ VPS.</b>", chat_id=chat_id)
                continue
            card_text, markup = format_all_vps_card(cfg, active_sessions, target_uid)
            send_telegram(cfg, card_text, chat_id=chat_id, reply_markup=markup)
        elif cmd == "/use":
            if not is_admin:
                send_telegram(cfg, "⛔ <b>Chỉ Admin mới có quyền chuyển VPS điều khiển.</b>", chat_id=chat_id)
                continue
            pick = arg.strip()
            if pick in ("", "me", "admin", ADMIN_ID):
                admin_state["target"] = ADMIN_ID
                send_telegram(cfg, f"👑 <b>Đã chuyển điều khiển về VPS Admin gốc (<code>{ADMIN_ID}</code>).</b>", chat_id=chat_id)
            elif pick in allowed:
                admin_state["target"] = pick
                send_telegram(
                    cfg,
                    f"🎯 <b>Đã chuyển quyền điều khiển sang VPS của ID: <code>{html.escape(pick)}</code></b>\n"
                    "Mọi lệnh <code>/ssh</code>, <code>/status</code>, <code>/rotate</code>, <code>/sh</code>, <code>/pkgs</code>... từ giờ sẽ tác động lên VPS này.\n"
                    "Gõ <code>/use me</code> để quay về VPS của bạn.",
                    chat_id=chat_id,
                )
            else:
                send_telegram(cfg, f"⚠️ ID <code>{html.escape(pick)}</code> chưa có trong danh sách <code>/ids</code>.", chat_id=chat_id)
        elif cmd == "/ssh":
            if not state:
                send_telegram(
                    cfg,
                    target_banner
                    + f"⚠️ <b>VPS của ID <code>{html.escape(target_uid)}</code> chưa chạy.</b>\nBấm nút <b>🚀 Tạo / Bật VPS</b> (<code>/newvps</code>) để khởi động!",
                    chat_id=chat_id,
                )
                continue
            send_telegram(cfg, target_banner + format_ssh_card(cfg, state["last"], state, uid=target_uid), chat_id=chat_id)
        elif cmd == "/status":
            send_telegram(cfg, target_banner + get_vps_status_card(cfg, state, uid=target_uid), chat_id=chat_id)
        elif cmd == "/rotate":
            if not state:
                send_telegram(cfg, target_banner + "⚠️ <b>VPS chưa bật. Hãy bấm 🚀 Tạo / Bật VPS trước.</b>", chat_id=chat_id)
                continue
            send_telegram(
                cfg,
                target_banner + "⏳ <b>Đang khởi tạo cổng & mật khẩu SSH mới...</b>\n<i>Vui lòng đợi khoảng 10–70 giây.</i>",
                chat_id=chat_id,
                menu=False,
            )
            new = ensure_running(state["page"], force_rerun=True)
            if new:
                state["last"] = new
                state["next_rerun"] = time.monotonic() + cfg["cell_rerun_minutes"] * 60
                publish(cfg, new, state, uid=target_uid, notify_chat_id=chat_id)
            else:
                send_telegram(cfg, target_banner + "❌ <b>Không tạo được cổng SSH mới.</b>", chat_id=chat_id)
        elif cmd == "/restart":
            if not state:
                send_telegram(cfg, target_banner + "⚠️ <b>VPS chưa bật. Hãy bấm 🚀 Tạo / Bật VPS trước.</b>", chat_id=chat_id)
                continue
            send_telegram(
                cfg,
                target_banner + "⏳ <b>Đang đồng bộ dữ liệu lên Hugging Face & khởi động lại phiên 12h...</b>",
                chat_id=chat_id,
                menu=False,
            )
            sync_remote(cfg, state["last"], mode="--push", uid=target_uid)
            state["page"].reload(wait_until="domcontentloaded", timeout=120000)
            state["page"].wait_for_timeout(5000)
            new = ensure_running(state["page"])
            if new:
                now = time.monotonic()
                state["last"] = new
                state["next_restart"] = now + cfg["session_restart_minutes"] * 60
                state["next_rerun"] = now + cfg["cell_rerun_minutes"] * 60
                publish(cfg, new, state, uid=target_uid, notify_chat_id=chat_id)
            else:
                send_telegram(cfg, target_banner + "❌ <b>Reload xong nhưng chưa lấy được SSH.</b>", chat_id=chat_id)
        elif cmd == "/save":
            if not state:
                send_telegram(cfg, target_banner + "⚠️ <b>VPS chưa bật.</b>", chat_id=chat_id)
                continue
            send_telegram(
                cfg,
                target_banner + "⏳ <b>Đang đồng bộ <code>/home/marimo</code> lên Hugging Face...</b>",
                chat_id=chat_id,
                menu=False,
            )
            out = sync_remote(cfg, state["last"], mode="--push", uid=target_uid)
            send_telegram(
                cfg,
                target_banner
                + "✅ <b>ĐỒNG BỘ HUGGING FACE HOÀN TẤT</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"<pre>{html.escape(out or 'Đã lưu thành công.')}</pre>",
                chat_id=chat_id,
            )
        elif cmd in ("/sh", "/exec"):
            if not state:
                send_telegram(cfg, target_banner + "⚠️ <b>VPS chưa bật.</b>", chat_id=chat_id)
                continue
            if not arg:
                send_telegram(
                    cfg,
                    target_banner + "💻 <b>Chạy lệnh từ xa trên VPS</b>\nCách dùng: <code>/sh &lt;lệnh&gt;</code>\nVí dụ: <code>/sh fastfetch --pipe</code>",
                    chat_id=chat_id,
                )
                continue
            try:
                out = ssh_exec(state["last"], arg, timeout=120)
                send_telegram(
                    cfg,
                    target_banner
                    + f"💻 <b>Lệnh:</b> <code>{html.escape(arg)}</code>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<pre>{html.escape((out or '(Hoàn tất, không có output)')[-3500:])}</pre>",
                    chat_id=chat_id,
                )
            except Exception as exc:
                send_telegram(cfg, target_banner + f"❌ <b>Lỗi thực thi:</b> <code>{html.escape(str(exc))}</code>", chat_id=chat_id)
        elif cmd == "/pkgs":
            if not state:
                send_telegram(cfg, target_banner + "⚠️ <b>VPS chưa bật.</b>", chat_id=chat_id)
                continue
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
                    target_banner
                    + "📦 <b>DANH SÁCH GÓI TỰ CÀI LẠI SAU 12H</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔹 <b>Gói APT:</b> {apt_s}\n"
                    f"🔸 <b>Gói PIP:</b> {pip_s}\n\n"
                    "➕ Thêm gói: <code>/addpkg apt tên_gói</code>\n"
                    "➖ Xoá gói: <code>/delpkg tên_gói</code>",
                    chat_id=chat_id,
                )
            except Exception as exc:
                send_telegram(cfg, target_banner + f"❌ <b>Lỗi đọc gói:</b> <code>{html.escape(str(exc))}</code>", chat_id=chat_id)
        elif cmd == "/addpkg":
            if not state:
                send_telegram(cfg, target_banner + "⚠️ <b>VPS chưa bật.</b>", chat_id=chat_id)
                continue
            sub = arg.split()
            if len(sub) < 2 or sub[0] not in ("apt", "pip"):
                send_telegram(
                    cfg,
                    target_banner
                    + "➕ <b>Thêm & cài gói tự động</b>\nCách dùng:\n• <code>/addpkg apt htop tmux</code>\n• <code>/addpkg pip requests</code>",
                    chat_id=chat_id,
                )
                continue
            kind, names = sub[0], [x for x in sub[1:] if re.match(r"^[A-Za-z0-9_.+-]+$", x)]
            if not names:
                send_telegram(cfg, target_banner + "❌ <b>Tên gói không hợp lệ.</b>", chat_id=chat_id)
                continue
            send_telegram(
                cfg,
                target_banner + f"⏳ <b>Đang cài & lưu ({kind}):</b> <code>{' '.join(names)}</code>...",
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
                    target_banner
                    + f"✅ <b>ĐÃ CÀI & LƯU ({kind.upper()}):</b> <code>{' '.join(names)}</code>\n"
                    f"<pre>{html.escape(out[-800:])}</pre>",
                    chat_id=chat_id,
                )
            except Exception as exc:
                send_telegram(cfg, target_banner + f"❌ <b>Lỗi:</b> <code>{html.escape(str(exc))}</code>", chat_id=chat_id)
        elif cmd == "/delpkg":
            if not state:
                send_telegram(cfg, target_banner + "⚠️ <b>VPS chưa bật.</b>", chat_id=chat_id)
                continue
            names = [x for x in arg.split() if re.match(r"^[A-Za-z0-9_.+-]+$", x)]
            if not names:
                send_telegram(cfg, target_banner + "➖ <b>Xoá gói:</b> <code>/delpkg tên_gói</code>", chat_id=chat_id)
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
                    target_banner + f"✅ <b>Đã xoá gói:</b> <code>{', '.join(names)}</code>\n<pre>{html.escape(out)}</pre>",
                    chat_id=chat_id,
                )
            except Exception as exc:
                send_telegram(cfg, target_banner + f"❌ <b>Lỗi:</b> <code>{html.escape(str(exc))}</code>", chat_id=chat_id)
        elif cmd == "/set":
            if not is_admin:
                send_telegram(cfg, "⛔ <b>Chỉ Admin mới có quyền chỉnh chu kỳ hệ thống.</b>", chat_id=chat_id)
                continue
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
            for st in active_sessions.values():
                st["next_rerun"] = now + cfg["cell_rerun_minutes"] * 60
                st["next_restart"] = now + cfg["session_restart_minutes"] * 60
            send_telegram(
                cfg,
                f"✅ <b>Đã lưu chu kỳ mới:</b>\n• Đổi cổng SSH: <code>{cfg['cell_rerun_minutes']} phút</code>\n• Reset phiên: <code>{cfg['session_restart_minutes']} phút</code>",
                chat_id=chat_id,
            )
        elif cmd == "/ids":
            if not is_admin:
                send_telegram(cfg, "⛔ <b>Chỉ Admin mới có quyền quản lý danh sách ID.</b>", chat_id=chat_id)
                continue
            rows = []
            for i in allowed:
                u = get_user_cfg(cfg, i)
                st_str = "🟢 VPS đang chạy" if i in active_sessions else "⚪ VPS tắt"
                mail_str = u.get("email") or ("Chưa liên kết" if not u.get("logged_in") else "Đã liên kết")
                rows.append(
                    f"• <code>{html.escape(i)}</code> {'👑 <i>(Admin)</i>' if i == ADMIN_ID else '👤'} — {st_str} ({html.escape(mail_str)})"
                )
            send_telegram(
                cfg,
                "👥 <b>DANH SÁCH ID ĐƯỢC CẤP QUYỀN</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                + "\n".join(rows)
                + "\n\n➕ Thêm: <code>/addid &lt;id&gt;</code>  |  ➖ Xoá: <code>/delid &lt;id&gt;</code>\n"
                "🖥 Quản lý VPS: <code>/vps</code>",
                chat_id=chat_id,
            )
        elif cmd == "/addid":
            if not is_admin:
                send_telegram(cfg, "⛔ <b>Chỉ Admin mới có quyền cấp ID mới.</b>", chat_id=chat_id)
                continue
            new_id = arg.strip()
            if not re.match(r"^-?\d+$", new_id):
                send_telegram(cfg, "👤 <b>Cấp quyền ID mới:</b> <code>/addid 123456789</code>", chat_id=chat_id)
                continue
            if new_id not in allowed:
                allowed.append(new_id)
                cfg["telegram_allowed_ids"] = allowed
                save_config(cfg)
            get_user_cfg(cfg, new_id)
            send_telegram(
                cfg,
                f"✅ <b>Đã cấp quyền cho ID:</b> <code>{html.escape(new_id)}</code>\n"
                f"Người dùng <code>{html.escape(new_id)}</code> giờ có thể tự liên kết tài khoản Molab (Google) bằng <code>/login email@gmail.com</code> và bấm <b>🚀 Tạo / Bật VPS</b>!",
                chat_id=chat_id,
            )
            if new_id != chat_id:
                send_telegram(
                    cfg,
                    "🎉 <b>BẠN ĐÃ ĐƯỢC CẤP QUYỀN TẠO MOLAB GPU VPS RIÊNG!</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "Để nhận máy VPS GPU <b>RTX Pro 6000</b> riêng (cách ly hoàn toàn):\n"
                    "1️⃣ Đăng nhập Google một lần trên <a href='https://molab.marimo.io/sign-in'>molab.marimo.io/sign-in</a>\n"
                    "2️⃣ Gõ tại đây: <code>/login email_cua_ban@gmail.com</code>\n"
                    "3️⃣ Nhập mã 6 số gửi về Gmail: <code>/code 123456</code>\n"
                    "4️⃣ Bấm nút <b>🚀 Tạo / Bật VPS</b> bên dưới!",
                    chat_id=new_id,
                )
        elif cmd == "/delid":
            if not is_admin:
                send_telegram(cfg, "⛔ <b>Chỉ Admin mới có quyền xoá ID.</b>", chat_id=chat_id)
                continue
            rem_id = arg.strip()
            if rem_id == ADMIN_ID:
                send_telegram(cfg, f"⛔ <b>Không thể xoá ID Admin gốc (<code>{ADMIN_ID}</code>).</b>", chat_id=chat_id)
                continue
            if rem_id in allowed:
                stop_user_vps(rem_id, active_sessions)
                close_pending_login(rem_id, pending_logins)
                allowed.remove(rem_id)
                cfg["telegram_allowed_ids"] = allowed
                save_config(cfg)
                if admin_state.get("target") == rem_id:
                    admin_state["target"] = ADMIN_ID
                send_telegram(cfg, f"✅ <b>Đã tắt VPS & thu hồi quyền của ID:</b> <code>{html.escape(rem_id)}</code>", chat_id=chat_id)
            else:
                send_telegram(cfg, f"⚠️ Không tìm thấy ID <code>{html.escape(rem_id)}</code>.", chat_id=chat_id)


def session(cfg):
    with sync_playwright() as playwright:
        active_sessions = {}
        pending_logins = {}
        admin_state = {"target": ADMIN_ID}
        try:
            ok, err = provision_or_start_user_vps(
                playwright, cfg, ADMIN_ID, active_sessions, pending_logins, force_new_notebook=False
            )
            if not ok:
                raise RuntimeError(err)

            while True:
                time.sleep(2)
                handle_telegram_commands(playwright, cfg, active_sessions, pending_logins, admin_state)

                for p_uid, p_item in list(pending_logins.items()):
                    if time.time() - p_item["ts"] > 600:
                        close_pending_login(p_uid, pending_logins)

                now = time.monotonic()
                for uid, st in list(active_sessions.items()):
                    page = st["page"]
                    try:
                        if now >= st["next_heartbeat"]:
                            page.mouse.move(5, 5)
                            page.evaluate("1")
                            st["next_heartbeat"] = now + 30

                        if now >= st["next_restart"]:
                            logging.info("Reload notebook [%s] (session 12h)", uid)
                            sync_remote(cfg, st["last"], mode="--push", uid=uid)
                            page.reload(wait_until="domcontentloaded", timeout=120000)
                            page.wait_for_timeout(5000)
                            new = ensure_running(page)
                            st["next_restart"] = now + cfg["session_restart_minutes"] * 60
                            st["next_rerun"] = now + cfg["cell_rerun_minutes"] * 60
                            if new:
                                if new != st["last"]:
                                    publish(cfg, new, st, uid=uid)
                                st["last"] = new
                            else:
                                page.screenshot(path=str(ROOT / "error.png"), full_page=True)
                                logging.error("Reload [%s] xong nhưng không thấy SSH", uid)
                            continue

                        if now >= st["next_rerun"]:
                            logging.info("Rerun cell [%s] (tunnel Pinggy 60 phút)", uid)
                            new = ensure_running(page, force_rerun=True)
                            st["next_rerun"] = now + cfg["cell_rerun_minutes"] * 60
                            if new:
                                publish(cfg, new, st, uid=uid)
                                st["last"] = new
                            else:
                                page.screenshot(path=str(ROOT / "error.png"), full_page=True)
                                logging.error("Rerun [%s] không ra địa chỉ mới", uid)
                    except Exception as exc:
                        logging.warning("Lỗi vòng lặp VPS [%s]: %s", uid, exc)
        finally:
            for uid in list(active_sessions.keys()):
                stop_user_vps(uid, active_sessions)
            for uid in list(pending_logins.keys()):
                close_pending_login(uid, pending_logins)


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
