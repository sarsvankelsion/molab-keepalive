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
import threading
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


def get_editor_code(editor):
    try:
        return (
            editor.evaluate(
                """el => {
                    const v = (el.cmTile && el.cmTile.view) || (el.cmView && el.cmView.view);
                    return v ? v.state.doc.toString() : (el.innerText || "");
                }"""
            )
            or ""
        )
    except Exception:
        return ""


def find_ssh_cell(page):
    fallback = None
    for frame in page.frames:
        editors = frame.locator(".cm-content")
        count = editors.count()
        if count and fallback is None:
            fallback = editors.first
        for index in range(count):
            try:
                txt = get_editor_code(editors.nth(index))
                if "_start_pinggy_subprocess" in txt or "import pinggy" in txt or "LOCAL_PORT" in txt:
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
            try:
                head = frame.inner_text("body", timeout=2000)[:120]
                if "Connecting" not in head:
                    return frame
            except Exception:
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
    before_alive = tunnel_alive(before)
    frame = editor_frame(page)
    run_btn = frame.get_by_test_id("run-button") if frame else None
    clicked_btn = False
    if run_btn and run_btn.count():
        try:
            run_btn.first.click(force=True, timeout=5000)
            clicked_btn = True
        except Exception:
            clicked_btn = False
    if not clicked_btn:
        editor.click(position={"x": 80, "y": 20})
        page.wait_for_timeout(300)
        page.keyboard.press("Shift+Enter")
    page.wait_for_timeout(6000)
    started = time.time()
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        result = scrape(page)
        if result:
            if result != before and tunnel_alive(result):
                return result
            if (
                result == before
                and "bore.pub" in result[0]
                and (not before_alive or time.time() - started >= 8)
                and tunnel_alive(result)
            ):
                return result
        if time.time() - started > 70:
            text = visible_text(page)
            if (
                "IndexError" in text
                or "Tunnel failed to start" in text
                or "RuntimeError:" in text
            ):
                logging.warning("Tunnel lỗi, chạy lại cell")
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

EFFECT_FIRE = "5104841245755180586"
EFFECT_PARTY = "5046509860389126442"
EFFECT_LIKE = "5107584321108051014"

ADMIN_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "🔑 Thông tin SSH", "callback_data": "/ssh"},
            {"text": "📊 Cấu hình & GPU", "callback_data": "/status"},
        ],
        [
            {"text": "🖥 Quản lý đa VPS", "callback_data": "/vps"},
            {"text": "➕ Tạo thêm VPS mới", "callback_data": "/newvps new"},
        ],
        [
            {"text": "🚀 App chạy ngầm", "callback_data": "/apps"},
            {"text": "📦 Gói tự cài", "callback_data": "/pkgs"},
        ],
        [
            {"text": "🔄 Làm mới SSH", "callback_data": "/rotate"},
            {"text": "💾 Backup HF ngay", "callback_data": "/save"},
        ],
        [
            {"text": "👥 Quản lý Member", "callback_data": "/ids"},
            {"text": "📘 Hướng dẫn A-Z", "callback_data": "/guide"},
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
            {"text": "🚀 Bật VPS hiện tại", "callback_data": "/newvps"},
        ],
        [
            {"text": "🖥 Danh sách VPS của tôi", "callback_data": "/vps"},
            {"text": "➕ Tạo thêm VPS mới", "callback_data": "/newvps new"},
        ],
        [
            {"text": "🔑 Thông tin SSH", "callback_data": "/ssh"},
            {"text": "📊 Cấu hình & GPU", "callback_data": "/status"},
        ],
        [
            {"text": "🚀 App chạy ngầm", "callback_data": "/apps"},
            {"text": "📦 Gói tự cài", "callback_data": "/pkgs"},
        ],
        [
            {"text": "📘 Hướng dẫn A-Z", "callback_data": "/guide"},
            {"text": "💾 Backup HF ngay", "callback_data": "/save"},
        ],
        [
            {"text": "♻️ Reset phiên 12h", "callback_data": "/restart"},
            {"text": "🛑 Tắt VPS", "callback_data": "/stopvps"},
        ],
    ]
}

MAIN_KEYBOARD = ADMIN_KEYBOARD
PENDING_ACCESS_REQS = {}


def get_member_guide(cfg, uid):
    nb = get_user_nb(cfg, uid)
    return (
        "📘 <b>CẨM NANG VẬN HÀNH MOLAB GPU VPS (A–Z CHO NGƯỜI MỚI)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Tài khoản Member ID:</b> <code>{html.escape(str(uid))}</code>\n"
        "🎮 <b>Cấu hình mỗi máy:</b> <code>NVIDIA RTX Pro 6000 (96GB VRAM) • 4 CPU • 32GB RAM</code>\n\n"
        "<b>1️⃣ BƯỚC 1: LIÊN KẾT TÀI KHOẢN MOLAB (CHỈ LÀM 1 LẦN)</b>\n"
        "• Truy cập <a href='https://molab.marimo.io/sign-in'>molab.marimo.io/sign-in</a> và bấm <b>Continue with Google</b>.\n"
        "• Quay lại bot gõ: <code>/login email_cua_ban@gmail.com</code>\n"
        "• Mở Gmail lấy mã 6 chữ số từ marimo rồi gõ: <code>/code 123456</code>\n\n"
        "<b>2️⃣ BƯỚC 2: BẬT VPS & TẠO NHIỀU VPS (NHIỀU NOTEBOOK)</b>\n"
        "• <b>Bật máy hiện tại:</b> Bấm <b>🚀 Bật VPS hiện tại</b> (<code>/newvps</code>).\n"
        "• <b>Tạo thêm VPS thứ 2, thứ 3...:</b> Bấm <b>➕ Tạo thêm VPS mới</b> (<code>/newvps new</code>) — mỗi notebook là 1 VPS độc lập có <b>cổng SSH cố định riêng</b>!\n"
        "• <b>Chuyển đổi giữa các VPS:</b> Bấm <b>🖥 Danh sách VPS của tôi</b> (<code>/vps</code>) hoặc gõ <code>/use 1</code>, <code>/use 2</code>...\n\n"
        "<b>3️⃣ BƯỚC 3: KẾT NỐI SSH CỐ ĐỊNH & ĐỔI MẬT KHẨU</b>\n"
        f"• <b>Lệnh SSH máy đang chọn (VPS #{nb['idx']}):</b>\n"
        f"  <code>ssh -o ServerAliveInterval=30 -p {nb['static_port']} molab@bore.pub</code>\n"
        f"• <b>Mật khẩu hiện tại:</b> <code>{html.escape(nb['static_password'])}</code>\n"
        "• <b>Đổi mật khẩu theo ý thích:</b> <code>/setpass &lt;mật_khẩu_mới&gt;</code>\n"
        "• <b>Đổi cổng SSH cố định:</b> <code>/setport &lt;10000-60000&gt;</code>\n\n"
        "<b>4️⃣ BƯỚC 4: TREO APP / BOT / WEB 24/7 (TỰ SỐNG LẠI SAU 12H)</b>\n"
        "• Đặt code của bạn trong thư mục <code>/home/marimo</code>.\n"
        "• Gõ trên bot: <code>/deploy python3 bot.py</code> (hoặc trong SSH gõ: <code>deploy python3 bot.py</code>).\n"
        "• Hệ thống tự tạo systemd service chạy ngầm, tự khởi động lại nếu crash và <b>tự chạy lại sau mỗi lần reset phiên 12h</b>!\n"
        "• Quản lý, xem log, xoá app: bấm <b>🚀 App chạy ngầm</b> (<code>/apps</code>).\n\n"
        "<b>5️⃣ BƯỚC 5: CÀI GÓI & LƯU DỮ LIỆU VĨNH VIỄN</b>\n"
        "• Cài gói tự động giữ lại sau 12h: <code>/addpkg apt tmux ffmpeg</code> hoặc <code>/addpkg pip torch</code>.\n"
        "• Lưu dữ liệu <code>/home/marimo</code> ngay lập tức: bấm <b>💾 Backup HF ngay</b> (<code>/save</code>) hoặc gõ <code>save</code> trong SSH."
    )


def get_bot_help(is_admin=False):
    base = (
        "⚡ <b>MOLAB CLOUD VPS — BẢNG ĐIỀU KHIỂN</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📘 <b>Người mới bắt đầu?</b> Gõ <code>/guide</code> (hoặc bấm <b>📘 Hướng dẫn A-Z</b>)\n\n"
        "🖥 <b>Quản lý Đa VPS (Nhiều Notebook độc lập)</b>\n"
        "• <code>/vps</code> — Xem danh sách các VPS, trạng thái & chuyển máy đang điều khiển\n"
        "• <code>/newvps</code> — Khởi động VPS đang chọn\n"
        "• <code>/newvps new</code> — Tạo thêm 1 VPS (Notebook GPU) mới với cổng SSH riêng\n"
        "• <code>/use &lt;số_máy&gt;</code> — Chuyển sang điều khiển VPS #1, #2... (VD: <code>/use 2</code>)\n"
        "• <code>/stopvps</code> — Lưu dữ liệu & tắt VPS đang chọn\n\n"
        "📌 <b>SSH Cố Định Vĩnh Viễn (bore.pub)</b>\n"
        "• <code>/ssh</code> — Xem lệnh SSH & mật khẩu cố định của máy đang chọn\n"
        "• <code>/setpass &lt;mật_khẩu&gt;</code> — Đổi mật khẩu SSH cho máy đang chọn\n"
        "• <code>/setport &lt;cổng&gt;</code> — Đổi cổng SSH cố định (10000–60000)\n\n"
        "🚀 <b>Deploy App chạy ngầm (Tự bật lại sau 12h)</b>\n"
        "• <code>/deploy python3 bot.py</code> — Chạy ngầm & tự sống lại sau 12h\n"
        "• <code>/apps</code> | <code>/applog 1</code> | <code>/undep 1</code> — Quản lý App ngầm\n\n"
        "🔗 <b>Tài khoản & Hệ thống</b>\n"
        "• <code>/login email@gmail.com</code> | <code>/code 123456</code> — Liên kết Molab\n"
        "• <code>/status</code> | <code>/rotate</code> | <code>/restart</code> | <code>/save</code>\n"
        "• <code>/pkgs</code> | <code>/addpkg apt tên</code> | <code>/sh lệnh</code>\n"
    )
    if is_admin:
        base += (
            "\n👑 <b>Quyền Quản trị viên (Admin)</b>\n"
            "• <code>/vps</code> — Xem & điều khiển toàn bộ các VPS của mọi Member\n"
            "• <code>/use &lt;id&gt;[:số_máy]</code> — Chọn VPS của Member (VD: <code>/use 123456:2</code>)\n"
            "• <code>/setpass &lt;id&gt;[:số] &lt;mk_mới&gt;</code> — Đổi mật khẩu VPS cho từng Member\n"
            "• <code>/ids</code> | <code>/addid &lt;id&gt;</code> | <code>/delid &lt;id&gt;</code> — Duyệt / xoá Member\n"
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


def _default_port_for_uid(uid):
    uid = str(uid).strip()
    if uid == ADMIN_ID:
        return 43803
    digits = re.sub(r"\D", "", uid)
    return 25000 + (int(digits) % 35000) if digits else 43803


def _used_static_ports(cfg, exclude_uid=None, exclude_idx=None):
    used = set()
    for u_id, u_data in (cfg.get("users") or {}).items():
        for nb in u_data.get("notebooks") or []:
            if str(u_id) == str(exclude_uid) and int(nb.get("idx", 1)) == int(exclude_idx or 0):
                continue
            if nb.get("static_port"):
                used.add(int(nb["static_port"]))
    return used


def _alloc_nb_port(cfg, uid, idx):
    base = _default_port_for_uid(uid)
    used = _used_static_ports(cfg, exclude_uid=uid, exclude_idx=idx)
    cand = base + (int(idx) - 1)
    while cand in used or not (10000 <= cand <= 60000):
        cand += 1
        if cand > 60000:
            cand = 20000
    return cand


def get_user_cfg(cfg, uid):
    uid = str(uid).strip()
    users = cfg.setdefault("users", {})
    base_hf = cfg.get("hf_repo") or "lesslosster/molab-backup"
    u = users.setdefault(uid, {})
    changed = False
    if uid == ADMIN_ID:
        defaults = {
            "profile_dir": cfg["profile_dir"],
            "notebook_url": cfg["notebook_url"],
            "hf_repo": base_hf,
            "output_file": cfg["output_file"],
            "logged_in": True,
            "email": "Admin (mặc định)",
            "static_port": 43803,
            "static_password": "molab" + ADMIN_ID[-4:],
            "active_idx": 1,
        }
    else:
        defaults = {
            "profile_dir": f"profiles/{uid}",
            "notebook_url": "",
            "hf_repo": f"{base_hf}-{uid}",
            "output_file": f"last_ssh_{uid}.txt",
            "logged_in": False,
            "email": "",
            "static_port": _default_port_for_uid(uid),
            "static_password": "molab" + (re.sub(r"\D", "", uid)[-4:] or "6000"),
            "active_idx": 1,
        }
    for k, v in defaults.items():
        if k not in u or u[k] == "":
            u[k] = v
            changed = True
    if uid == ADMIN_ID:
        u["profile_dir"] = cfg["profile_dir"]
        if not u.get("notebook_url"):
            u["notebook_url"] = cfg["notebook_url"]
        u["hf_repo"] = base_hf
        u["output_file"] = cfg["output_file"]
        u["logged_in"] = True
    if not u.get("notebooks"):
        u["notebooks"] = [
            {
                "idx": 1,
                "notebook_url": u["notebook_url"],
                "static_port": int(u["static_port"]),
                "static_password": str(u["static_password"]),
                "hf_repo": u["hf_repo"],
            }
        ]
        changed = True
    else:
        for i, nb in enumerate(u["notebooks"], 1):
            if nb.get("idx") != i:
                nb["idx"] = i
                changed = True
            if not nb.get("static_port"):
                nb["static_port"] = _alloc_nb_port(cfg, uid, i)
                changed = True
            if not nb.get("static_password"):
                nb["static_password"] = str(u["static_password"])
                changed = True
            if not nb.get("hf_repo"):
                nb["hf_repo"] = u["hf_repo"] if i == 1 else f"{u['hf_repo']}-vps{i}"
                changed = True
    if not (1 <= int(u.get("active_idx") or 1) <= len(u["notebooks"])):
        u["active_idx"] = 1
        changed = True
    if changed:
        save_config(cfg)
    return u


def get_user_nb(cfg, uid, idx=None, create_new=False):
    u = get_user_cfg(cfg, uid)
    nbs = u["notebooks"]
    if create_new:
        if len(nbs) == 1 and not nbs[0].get("notebook_url"):
            u["active_idx"] = 1
            save_config(cfg)
            return nbs[0]
        new_idx = len(nbs) + 1
        nb = {
            "idx": new_idx,
            "notebook_url": "",
            "static_port": _alloc_nb_port(cfg, uid, new_idx),
            "static_password": str(u["static_password"]),
            "hf_repo": f"{u['hf_repo']}-vps{new_idx}",
        }
        nbs.append(nb)
        u["active_idx"] = new_idx
        save_config(cfg)
        return nb
    pick = int(idx or u.get("active_idx") or 1)
    for nb in nbs:
        if int(nb["idx"]) == pick:
            return nb
    return nbs[0]


def session_key(uid, idx=1):
    return f"{str(uid).strip()}:{int(idx or 1)}"


def parse_target_spec(spec, default_uid, cfg):
    spec = (spec or "").strip()
    if ":" in spec:
        u_part, i_part = spec.split(":", 1)
        uid = u_part.strip() or str(default_uid)
        idx = int(i_part) if i_part.strip().isdigit() else get_user_nb(cfg, uid)["idx"]
        return uid, idx
    if spec.isdigit() and int(spec) <= 50:
        return str(default_uid), int(spec)
    if re.match(r"^-?\d+$", spec):
        uid = spec
        return uid, get_user_nb(cfg, uid)["idx"]
    return str(default_uid), get_user_nb(cfg, default_uid)["idx"]


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


def react_telegram(cfg, chat_id, message_id, emoji="⚡"):
    if not chat_id or not message_id:
        return
    tg_api(
        cfg,
        "setMessageReaction",
        {
            "chat_id": str(chat_id),
            "message_id": int(message_id),
            "reaction": json.dumps([{"type": "emoji", "emoji": emoji}]),
            "is_big": "true",
        },
        timeout=4,
    )
    tg_api(cfg, "sendChatAction", {"chat_id": str(chat_id), "action": "typing"}, timeout=4)


def send_telegram(cfg, text, chat_id=None, html_mode=True, menu=True, reply_markup=None, effect_id=None):
    targets = [str(chat_id)] if chat_id else [ADMIN_ID]
    for target in targets:
        payload = {
            "chat_id": target,
            "text": text[:4000],
            "disable_web_page_preview": "true",
        }
        if html_mode:
            payload["parse_mode"] = "HTML"
        if effect_id:
            payload["message_effect_id"] = str(effect_id)
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        elif menu:
            kb = ADMIN_KEYBOARD if str(target) == ADMIN_ID else USER_KEYBOARD
            payload["reply_markup"] = json.dumps(kb)
        tg_api(cfg, "sendMessage", payload)


def format_ssh_card(cfg, result, state=None, uid=ADMIN_ID, idx=None, title="🔑 <b>THÔNG TIN KẾT NỐI SSH CỐ ĐỊNH</b>"):
    u = get_user_cfg(cfg, uid)
    nb = get_user_nb(cfg, uid, idx=idx or (state.get("idx") if state else None))
    nb_idx = nb["idx"]
    if not result:
        return f"{title}\n━━━━━━━━━━━━━━━━━━━━━━\n⚠️ <b>VPS #{nb_idx} của ID <code>{html.escape(str(uid))}</code> chưa có kết nối SSH.</b>"
    ssh_line, password = result
    alive = "🟢 Đang hoạt động (Cố định 24/7)" if "bore.pub" in ssh_line and tunnel_alive(result) else ("🟢 Đang hoạt động" if tunnel_alive(result) else "🔴 Mất kết nối")
    timers = ""
    if state:
        rem_ses = max(0, int((state["next_restart"] - time.monotonic()) // 60))
        if "bore.pub" in ssh_line:
            timers = f"📌 <b>Địa chỉ & Mật khẩu:</b> Cố định vĩnh viễn  |  <b>Reset phiên:</b> {rem_ses} phút\n"
        else:
            rem_ssh = max(0, int((state["next_rerun"] - time.monotonic()) // 60))
            timers = f"⏱ <b>Đổi cổng sau:</b> {rem_ssh} phút  |  <b>Reset phiên:</b> {rem_ses} phút\n"
    repo = nb.get("hf_repo") or u.get("hf_repo") or "chưa cấu hình"
    owner_line = (
        f"👤 <b>Chủ VPS (ID):</b> <code>{html.escape(str(uid))}</code> ({html.escape(u.get('email') or 'Chưa rõ')})\n"
        f"🖥 <b>Máy VPS:</b> <b>#{nb_idx}</b> / {len(u['notebooks'])} máy  |  <b>Cổng riêng:</b> <code>bore.pub:{nb['static_port']}</code>\n"
    )
    return (
        f"{title}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{owner_line}"
        f"📡 <b>Trạng thái:</b> {alive}\n"
        f"🕒 <b>Cập nhật:</b> <code>{time.strftime('%H:%M:%S — %d/%m/%Y')}</code>\n"
        f"{timers}"
        f"☁️ <b>Backup HF:</b> <code>{html.escape(repo)}</code>\n\n"
        "🖥 <b>Lệnh SSH cố định</b> <i>(chạm để copy)</i>:\n"
        f"<code>{html.escape(ssh_line)}</code>\n\n"
        "🔐 <b>Mật khẩu cố định</b> <i>(chạm để copy)</i>:\n"
        f"<code>{html.escape(password)}</code>\n\n"
        "💡 <i>Đổi mật khẩu: <code>/setpass &lt;mật_khẩu&gt;</code> | Tạo thêm VPS mới: <code>/newvps new</code></i>"
    )


def init_telegram_bot(cfg):
    global TG_OFFSET
    if not cfg.get("telegram_bot_token"):
        return
    get_allowed_ids(cfg)
    commands = [
        {"command": "guide", "description": "📘 Hướng dẫn vận hành A–Z cho người mới"},
        {"command": "ssh", "description": "🔑 Lấy lệnh SSH & mật khẩu cố định (copy 1 chạm)"},
        {"command": "vps", "description": "🖥 Danh sách các VPS (nhiều notebook) & chuyển máy"},
        {"command": "newvps", "description": "🚀 Bật VPS hiện tại hoặc tạo thêm máy: /newvps [new]"},
        {"command": "use", "description": "🎯 Chuyển sang điều khiển VPS số #1, #2...: /use <số>"},
        {"command": "setpass", "description": "🔐 Đổi mật khẩu VPS: /setpass [id] <mật_khẩu>"},
        {"command": "setport", "description": "📌 Đổi cổng SSH cố định: /setport <10000-60000>"},
        {"command": "status", "description": "📊 Xem GPU, RAM, Disk & trạng thái VPS"},
        {"command": "deploy", "description": "🚀 Chạy ngầm & tự bật lại sau 12h: /deploy <lệnh>"},
        {"command": "apps", "description": "📋 Xem & quản lý các App đang chạy ngầm"},
        {"command": "undep", "description": "🗑 Dừng & xoá App chạy ngầm: /undep <số>"},
        {"command": "login", "description": "🔗 Liên kết acc Molab (Google): /login <email>"},
        {"command": "code", "description": "🔢 Nhập mã 6 số từ Gmail: /code <mã>"},
        {"command": "rotate", "description": "🔄 Làm mới kết nối tunnel SSH ngay"},
        {"command": "save", "description": "💾 Đồng bộ dữ liệu lên Hugging Face ngay"},
        {"command": "pkgs", "description": "📦 Danh sách gói apt/pip tự cài"},
        {"command": "addpkg", "description": "➕ Cài & lưu gói: /addpkg <apt|pip> <tên>"},
        {"command": "delpkg", "description": "➖ Xoá gói tự cài: /delpkg <tên>"},
        {"command": "sh", "description": "💻 Chạy lệnh bash: /sh <lệnh>"},
        {"command": "ids", "description": "👥 (Admin) Xem & duyệt danh sách Member"},
        {"command": "addid", "description": "👤 (Admin) Duyệt / cấp quyền Member: /addid <id>"},
        {"command": "delid", "description": "🚫 (Admin) Thu hồi quyền Member: /delid <id>"},
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


def sync_remote(cfg, result, mode="--start-daemon", uid=ADMIN_ID, idx=None):
    u = get_user_cfg(cfg, uid)
    nb = get_user_nb(cfg, uid, idx=idx)
    hf_repo = nb.get("hf_repo") or u.get("hf_repo") or cfg.get("hf_repo")
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
            logging.info("HF sync [%s#%s] (%s): %s", uid, nb["idx"], mode, out)
        return out
    except Exception as exc:
        logging.warning("Không cài/chạy được HF sync [%s#%s] qua SSH: %s", uid, nb["idx"], exc)
        return f"Lỗi: {exc}"


def publish(cfg, result, state=None, uid=ADMIN_ID, idx=None, notify_chat_id=None):
    u = get_user_cfg(cfg, uid)
    nb = get_user_nb(cfg, uid, idx=idx or (state.get("idx") if state else None))
    nb_idx = nb["idx"]
    ssh_line, password = result
    text = f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n{ssh_line}\npassword: {password}\n"
    out_name = u["output_file"] if nb_idx == 1 else f"last_ssh_{uid}_vps{nb_idx}.txt"
    path = ROOT / out_name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    logging.info("Địa chỉ mới [%s#%s]:\n%s", uid, nb_idx, text.strip())
    card = format_ssh_card(cfg, result, state, uid=uid, idx=nb_idx)
    send_telegram(cfg, card, chat_id=uid, effect_id=EFFECT_FIRE)
    if notify_chat_id and str(notify_chat_id) != str(uid):
        send_telegram(cfg, card, chat_id=notify_chat_id, effect_id=EFFECT_FIRE)
    sync_remote(cfg, result, mode="--start-daemon", uid=uid, idx=nb_idx)


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
    editor = find_ssh_cell(page) or frame.locator(".cm-content").first
    updated = editor.evaluate(
        """(el, code) => {
            const view = (el.cmTile && el.cmTile.view) || (el.cmView && el.cmView.view);
            if (!view) return false;
            view.dispatch({
                changes: { from: 0, to: view.state.doc.length, insert: code }
            });
            return true;
        }""",
        code_str,
    )
    if not updated:
        editor.click(position={"x": 80, "y": 20})
        page.keyboard.press("Control+a")
        page.keyboard.press("Backspace")
        page.keyboard.insert_text(code_str)
    page.wait_for_timeout(1000)
    save_btn = frame.get_by_test_id("save-button")
    if save_btn.count():
        try:
            save_btn.first.click(timeout=3000)
            page.wait_for_timeout(1500)
        except Exception:
            pass
    return True


def build_user_cell_code(cfg, uid, idx=None):
    nb = get_user_nb(cfg, uid, idx=idx)
    port = int(nb.get("static_port") or _default_port_for_uid(uid))
    pw = str(nb.get("static_password") or f"molab{str(uid)[-4:]}")
    code = (ROOT / "molab_ssh_pinggy.py").read_text(encoding="utf-8")
    code = re.sub(r"STATIC_PORT\s*=\s*\d+", f"STATIC_PORT = {port}", code)
    code = re.sub(r'STATIC_PASSWORD\s*=\s*"[^"]*"', f"STATIC_PASSWORD = {json.dumps(pw)}", code)
    if str(uid) != ADMIN_ID or int(nb["idx"]) != 1:
        code = re.sub(r'TOKEN\s*=\s*"[^"]*"', 'TOKEN = ""', code)
    return code


def ensure_static_cell_code(page, cfg, uid, idx=None):
    nb = get_user_nb(cfg, uid, idx=idx)
    port = int(nb.get("static_port") or _default_port_for_uid(uid))
    pw = str(nb.get("static_password") or f"molab{str(uid)[-4:]}")
    wait_editor_frame(page, timeout=60)
    take_over(page)
    editor = find_ssh_cell(page)
    cur_code = get_editor_code(editor) if editor is not None else ""
    if (
        "_start_pinggy_subprocess" in cur_code
        and "sys.path[:]" in cur_code
        and f"STATIC_PORT = {port}" in cur_code
        and f"STATIC_PASSWORD = {json.dumps(pw)}" in cur_code
    ):
        return False
    logging.info("Cập nhật cell SSH cố định (bore.pub:%s) cho ID %s#%s", port, uid, nb["idx"])
    replace_cell_code(page, build_user_cell_code(cfg, uid, idx=nb["idx"]))
    return True


BORE_HOST = "bore.pub"
BORE_CTRL_PORT = 7835


def _bore_send_json(sock, payload):
    sock.sendall(json.dumps(payload).encode("utf-8") + b"\x00")


def _bore_recv_json(sock, buf):
    while b"\x00" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            return None, buf
        buf += chunk
    raw, _, buf = buf.partition(b"\x00")
    return json.loads(raw.decode("utf-8")), buf


def _bore_pipe(src, dst):
    try:
        while True:
            data = src.recv(16384)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        for s in (dst, src):
            try:
                s.close()
            except Exception:
                pass


def _bore_handshake(req_port):
    ctrl = socket.create_connection((BORE_HOST, BORE_CTRL_PORT), timeout=8)
    ctrl.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    ctrl.settimeout(8)
    _bore_send_json(ctrl, {"Hello": int(req_port)})
    msg, buf = _bore_recv_json(ctrl, b"")
    if not msg or "Hello" not in msg:
        ctrl.close()
        raise RuntimeError(f"Bore handshake failed: {msg}")
    ctrl.settimeout(35)
    return ctrl, int(msg["Hello"]), buf


def stop_bore_bridge(st):
    if not st:
        return
    ev = st.get("bore_stop")
    if ev is not None:
        ev.set()
    sock = st.get("bore_sock")
    if sock is not None:
        try:
            sock.close()
        except Exception:
            pass
    st["bore_stop"] = None
    st["bore_sock"] = None
    st["bore_thread"] = None


def attach_bore_bridge(cfg, uid, raw_result, st, idx=None):
    if not raw_result:
        return None
    raw_line, password = raw_result
    m = re.search(r"-p\s+(\d+)\s+\S+@(\S+)", raw_line)
    if not m:
        return raw_result
    target_port, target_host = int(m.group(1)), m.group(2)
    st["raw_last"] = raw_result
    st["raw_target"] = (target_host, target_port)

    nb = get_user_nb(cfg, uid, idx=idx or st.get("idx"))
    desired_port = int(nb.get("static_port") or _default_port_for_uid(uid))
    thr = st.get("bore_thread")
    if thr and thr.is_alive() and st.get("bore_port") == desired_port:
        return (f"ssh -o ServerAliveInterval=30 -p {desired_port} molab@{BORE_HOST}", password)

    stop_bore_bridge(st)
    ctrl = None
    assigned_port = None
    buf = b""
    for attempt in range(4):
        try:
            req = desired_port if attempt < 3 else 0
            ctrl, assigned_port, buf = _bore_handshake(req)
            break
        except Exception:
            time.sleep(1.5)
    if ctrl is None or assigned_port is None:
        logging.warning("Không mở được bore.pub cho [%s#%s], dùng trực tiếp Pinggy", uid, nb["idx"])
        return raw_result

    stop_ev = threading.Event()
    st["bore_stop"] = stop_ev
    st["bore_sock"] = ctrl
    st["bore_port"] = assigned_port

    def _accept_conn(conn_id):
        try:
            rem = socket.create_connection((BORE_HOST, BORE_CTRL_PORT), timeout=10)
            rem.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            _bore_send_json(rem, {"Accept": conn_id})
            thost, tport = st.get("raw_target") or (target_host, target_port)
            loc = socket.create_connection((thost, tport), timeout=10)
            loc.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # Clear the 10s connect timeout so idle SSH sessions never get dropped
            for s in (rem, loc):
                s.settimeout(None)
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                except Exception:
                    pass
            threading.Thread(target=_bore_pipe, args=(rem, loc), daemon=True).start()
            threading.Thread(target=_bore_pipe, args=(loc, rem), daemon=True).start()
        except Exception:
            pass

    def _bridge_loop(init_ctrl, init_buf, port_to_keep):
        cur_ctrl = init_ctrl
        cur_buf = init_buf
        while not stop_ev.is_set():
            try:
                if cur_ctrl is None:
                    cur_ctrl, _, cur_buf = _bore_handshake(port_to_keep)
                    st["bore_sock"] = cur_ctrl
                msg, cur_buf = _bore_recv_json(cur_ctrl, cur_buf)
                if msg is None:
                    raise EOFError("bore EOF")
                if "Connection" in msg:
                    threading.Thread(target=_accept_conn, args=(msg["Connection"],), daemon=True).start()
                elif "Error" in msg:
                    raise RuntimeError(msg["Error"])
            except Exception:
                if cur_ctrl is not None:
                    try:
                        cur_ctrl.close()
                    except Exception:
                        pass
                    cur_ctrl = None
                if not stop_ev.is_set():
                    time.sleep(2.0)

    b_thr = threading.Thread(target=_bridge_loop, args=(ctrl, buf, assigned_port), daemon=True)
    b_thr.start()
    st["bore_thread"] = b_thr
    return (f"ssh -o ServerAliveInterval=30 -p {assigned_port} molab@{BORE_HOST}", password)


def ensure_gpu_compute(page):
    try:
        btn = page.locator("button[title='Configure compute']").first
        btn.wait_for(state="visible", timeout=30000)
        label = (btn.inner_text(timeout=3000) or "").strip()
        if "RTX Pro 6000" in label:
            return True
        logging.info("Notebook đang ở chế độ CPU (%s), tự động bật GPU RTX Pro 6000...", label)
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
            save_btn.last.click()
            deadline = time.time() + 45
            while time.time() < deadline:
                page.wait_for_timeout(2000)
                cur_label = (btn.inner_text(timeout=3000) or "").strip()
                if "RTX Pro 6000" in cur_label:
                    break
            page.wait_for_timeout(5000)
            return True
    except Exception as exc:
        logging.warning("Không đổi được cấu hình GPU: %s", exc)
    return False


def close_pending_login(uid, pending_logins):
    item = pending_logins.pop(str(uid), None)
    if item:
        try:
            item["context"].close()
        except Exception:
            pass


def start_molab_email_login(playwright, cfg, uid, email, pending_logins, active_sessions):
    uid = str(uid)
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
            return True, f"✅ Tài khoản Molab (<code>{html.escape(email)}</code>) đã được đăng nhập sẵn! Bấm <b>🚀 Bật VPS hiện tại</b> để khởi tạo máy."
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
                    "• Giờ bạn có thể bấm nút <b>🚀 Bật VPS hiện tại</b> (<code>/newvps</code>) hoặc <b>➕ Tạo thêm VPS mới</b> (<code>/newvps new</code>)!",
                )
        return False, "❌ <b>Mã xác thực không đúng hoặc đã hết hạn.</b> Vui lòng kiểm tra lại và gõ <code>/code &lt;6_số&gt;</code>."
    except Exception as exc:
        close_pending_login(uid, pending_logins)
        return False, f"❌ <b>Lỗi xác thực mã:</b> <code>{html.escape(str(exc))}</code>"


def link_molab_cookie(playwright, cfg, uid, cookie_val, pending_logins, active_sessions):
    uid = str(uid)
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
                "Bấm nút <b>🚀 Bật VPS hiện tại</b> (hoặc gõ <code>/newvps</code>) để tạo VPS GPU riêng của bạn.",
            )
        return False, "❌ <b>Cookie <code>__client</code> không hợp lệ hoặc đã hết hạn.</b>"
    finally:
        ctx.close()


def _find_user_context(uid, active_sessions):
    uid = str(uid)
    for st in active_sessions.values():
        if str(st.get("uid")) == uid and st.get("context"):
            return st["context"]
    return None


def stop_user_vps(uid, active_sessions, idx=None):
    uid = str(uid)
    if idx is None:
        keys = [k for k, st in active_sessions.items() if str(st.get("uid")) == uid or k == uid]
    else:
        keys = [k for k in (session_key(uid, idx), uid if int(idx) == 1 else "") if k in active_sessions]
    if not keys:
        return False
    ctx_to_check = None
    for k in keys:
        st = active_sessions.pop(k, None)
        if not st:
            continue
        stop_bore_bridge(st)
        ctx_to_check = st.get("context") or ctx_to_check
        try:
            st["page"].close()
        except Exception:
            pass
    if ctx_to_check and not _find_user_context(uid, active_sessions):
        try:
            ctx_to_check.close()
        except Exception:
            pass
    return True


def provision_or_start_user_vps(
    playwright, cfg, uid, active_sessions, pending_logins, idx=None, force_new_notebook=False, notify_chat_id=None
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
            "Sau khi liên kết xong, bấm lại <b>🚀 Bật VPS hiện tại</b>!",
        )

    close_pending_login(uid, pending_logins)
    nb = get_user_nb(cfg, uid, idx=idx, create_new=force_new_notebook)
    nb_idx = nb["idx"]
    u["active_idx"] = nb_idx
    save_config(cfg)

    skey = session_key(uid, nb_idx)
    if skey in active_sessions:
        stop_user_vps(uid, active_sessions, idx=nb_idx)

    shared_ctx = _find_user_context(uid, active_sessions)
    created_ctx = False
    if shared_ctx is None:
        ctx = open_context(playwright, cfg, headless=cfg["headless"], profile_dir=u["profile_dir"])
        created_ctx = True
    else:
        ctx = shared_ctx

    page = None
    try:
        page = (ctx.pages[0] if (created_ctx and ctx.pages) else ctx.new_page())
        need_provision = force_new_notebook or not nb.get("notebook_url")
        if need_provision:
            page.goto(cfg["notebook_url"], wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(8000)
            body = page.inner_text("body")
            if "/sign-in" in page.url or "Sign in to marimo" in body:
                u["logged_in"] = False
                save_config(cfg)
                if created_ctx:
                    ctx.close()
                else:
                    page.close()
                return (
                    False,
                    "⚠️ <b>Phiên đăng nhập Molab đã hết hạn.</b>\n"
                    "Hãy liên kết lại bằng lệnh: <code>/login email@gmail.com</code>",
                )
            dup_btn = page.locator(
                "button[title='Duplicate notebook'], button:has-text('Duplicate'), button:has-text('Fork')"
            ).first
            if not dup_btn.count():
                page.goto("https://molab.marimo.io/notebooks", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(5000)
                dup_btn = page.locator("button:has-text('Create a notebook')").first
            if not dup_btn.count():
                if created_ctx:
                    ctx.close()
                else:
                    page.close()
                return False, "❌ <b>Không tìm thấy nút Fork / Duplicate / Create notebook trên Molab.</b>"
            old_url = page.url.split("?")[0]
            dup_btn.click()
            deadline = time.time() + 45
            while time.time() < deadline:
                page.wait_for_timeout(2000)
                cur_url = page.url.split("?")[0]
                if "/notebooks/nb_" in cur_url and cur_url != old_url:
                    break
            else:
                if created_ctx:
                    ctx.close()
                else:
                    page.close()
                return False, "❌ <b>Không tạo được notebook mới (quá thời gian khởi tạo).</b>"

            nb["notebook_url"] = page.url.split("?")[0]
            if nb_idx == 1:
                u["notebook_url"] = nb["notebook_url"]
                if uid == ADMIN_ID:
                    cfg["notebook_url"] = nb["notebook_url"]
            save_config(cfg)

            page.wait_for_timeout(6000)
            take_over(page)
            ensure_gpu_compute(page)
            take_over(page)
            replace_cell_code(page, build_user_cell_code(cfg, uid, idx=nb_idx))
            code_updated = True
        else:
            page.goto(nb["notebook_url"], wait_until="domcontentloaded", timeout=120000)
            page.wait_for_timeout(5000)
            ensure_gpu_compute(page)
            code_updated = ensure_static_cell_code(page, cfg, uid, idx=nb_idx)

        raw_result = ensure_running(page, force_rerun=need_provision or code_updated)
        if not raw_result:
            page.screenshot(path=str(ROOT / "error.png"), full_page=True)
            if created_ctx:
                ctx.close()
            else:
                page.close()
            return False, f"❌ <b>Không khởi chạy được cell SSH trên VPS #{nb_idx}.</b>"

        now = time.monotonic()
        st = {
            "skey": skey,
            "uid": uid,
            "idx": nb_idx,
            "context": ctx,
            "page": page,
            "last": raw_result,
            "next_rerun": now + cfg["cell_rerun_minutes"] * 60,
            "next_restart": now + cfg["session_restart_minutes"] * 60,
            "next_heartbeat": now + 30,
            "next_healthcheck": now + 180,
        }
        result = attach_bore_bridge(cfg, uid, raw_result, st, idx=nb_idx)
        st["last"] = result
        active_sessions[skey] = st
        publish(cfg, result, st, uid=uid, idx=nb_idx, notify_chat_id=notify_chat_id)
        return True, f"✅ <b>VPS #{nb_idx} của ID <code>{html.escape(uid)}</code> đã sẵn sàng!</b>"
    except Exception as exc:
        try:
            if created_ctx:
                ctx.close()
            elif page:
                page.close()
        except Exception:
            pass
        return False, f"❌ <b>Lỗi khởi tạo VPS #{nb_idx}:</b> <code>{html.escape(str(exc))}</code>"


def get_vps_status_card(cfg, state, uid=ADMIN_ID, idx=None):
    u = get_user_cfg(cfg, uid)
    nb = get_user_nb(cfg, uid, idx=idx or (state.get("idx") if state else None))
    nb_idx = nb["idx"]
    if not state or not state.get("last"):
        return (
            "📊 <b>THÔNG SỐ HỆ THỐNG MOLAB VPS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Chủ VPS (ID):</b> <code>{html.escape(str(uid))}</code>\n"
            f"🖥 <b>Máy VPS:</b> <b>#{nb_idx}</b> / {len(u['notebooks'])} máy (<code>bore.pub:{nb['static_port']}</code>)\n"
            "📡 <b>Trạng thái:</b> ⚪ Chưa bật (Bấm <b>🚀 Bật VPS hiện tại</b> để chạy)"
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
        f"🖥 <b>Máy VPS:</b> <b>#{nb_idx}</b> / {len(u['notebooks'])} máy (<code>bore.pub:{nb['static_port']}</code>)\n"
        f"📓 <b>Notebook:</b> <code>{html.escape(nb.get('notebook_url') or 'Chưa tạo')}</code>\n"
        f"🎮 <b>GPU:</b> <code>{html.escape(info.get('GPU', 'N/A'))}</code>\n"
        f"🧠 <b>CPU:</b> <code>{html.escape(info.get('CPU', 'N/A'))}</code>\n"
        f"⚡ <b>RAM:</b> <code>{html.escape(info.get('RAM', 'N/A'))}</code>\n"
        f"💿 <b>Ổ cứng:</b> <code>{html.escape(info.get('DISK', 'N/A'))}</code>\n"
        f"⏳ <b>Uptime:</b> <code>{html.escape(info.get('UP', 'N/A'))}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔄 <b>Làm mới đường truyền ngầm sau:</b> {rem_ssh} phút <i>(giữ nguyên cổng cố định)</i>\n"
        f"♻️ <b>Reset phiên sau:</b> {rem_ses} phút <i>(chu kỳ {cfg['session_restart_minutes']}p)</i>\n"
        f"☁️ <b>Kho lưu trữ:</b> <code>{html.escape(nb.get('hf_repo', ''))}</code>"
    )


def format_vps_card(cfg, active_sessions, viewer_uid, target_uid, target_idx, is_admin=False):
    uids = get_allowed_ids(cfg) if is_admin else [str(viewer_uid)]
    blocks = []
    buttons = []
    for uid in uids:
        u = get_user_cfg(cfg, uid)
        role = "👑 Admin" if uid == ADMIN_ID else "👤 Member"
        linked = f"✅ <code>{html.escape(u.get('email') or 'Đã liên kết')}</code>" if u.get("logged_in") else "❌ Chưa liên kết Molab"
        nb_lines = []
        for nb in u["notebooks"]:
            n_idx = int(nb["idx"])
            skey = session_key(uid, n_idx)
            st = active_sessions.get(skey)
            running = "🟢 Đang chạy" if st and st.get("last") else "⚪ Đang tắt"
            is_sel = str(uid) == str(target_uid) and n_idx == int(target_idx)
            sel = " 🎯 <b>[ĐANG CHỌN]</b>" if is_sel else ""
            nb_url = nb.get("notebook_url") or "(Chưa tạo)"
            nb_lines.append(
                f"   🖥 <b>VPS #{n_idx}</b>{sel} — {running}\n"
                f"      • SSH: <code>ssh -o ServerAliveInterval=30 -p {nb['static_port']} molab@bore.pub</code>\n"
                f"      • Pass: <code>{html.escape(nb['static_password'])}</code>\n"
                f"      • Notebook: <code>{html.escape(nb_url)}</code>"
            )
            spec = f"{uid}:{n_idx}" if is_admin else f"{n_idx}"
            label_prefix = f"{uid}#{n_idx}" if is_admin else f"VPS #{n_idx}"
            row = [{"text": f"🎯 Chọn {label_prefix}", "callback_data": f"/use {spec}"}]
            if st:
                row.append({"text": f"🛑 Tắt {label_prefix}", "callback_data": f"/stopvps {spec}"})
            else:
                row.append({"text": f"🚀 Bật {label_prefix}", "callback_data": f"/newvps {spec}"})
            buttons.append(row)
        blocks.append(
            f"🔹 <b>ID:</b> <code>{html.escape(uid)}</code> ({role}) | Molab: {linked}\n"
            + "\n".join(nb_lines)
        )
    buttons.append(
        [
            {"text": "➕ Tạo thêm VPS mới (Notebook mới)", "callback_data": "/newvps new"},
            {"text": "🔙 Bảng chính", "callback_data": "/help"},
        ]
    )
    title = "🖥 <b>QUẢN LÝ ĐA VPS TOÀN HỆ THỐNG</b>" if is_admin else "🖥 <b>DANH SÁCH CÁC MÁY VPS CỦA BẠN</b>"
    text = (
        f"{title}\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        + "\n\n".join(blocks)
        + "\n━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 <b>Máy đang điều khiển:</b> <code>{html.escape(str(target_uid))}#{target_idx}</code>\n"
        "💡 <i>Mỗi VPS là 1 Notebook GPU độc lập có cổng SSH & mật khẩu riêng. Bấm nút bên dưới để chuyển máy, bật/tắt hoặc tạo thêm VPS mới:</i>"
    )
    return text, {"inline_keyboard": buttons}


def format_apps_card(state, target_banner=""):
    if not state or not state.get("last"):
        return target_banner + "⚠️ <b>VPS chưa bật. Hãy bấm 🚀 Bật VPS hiện tại trước.</b>", None
    probe_py = (
        "import json, os, subprocess\n"
        "p = '/home/marimo/.packages.json'\n"
        "d = json.load(open(p)) if os.path.exists(p) else {}\n"
        "apps = d.get('apps') or []\n"
        "out = []\n"
        "for i, c in enumerate(apps, 1):\n"
        "    rc = subprocess.call(['systemctl', 'is-active', '--quiet', f'molab-app-{i}.service'])\n"
        "    out.append({'idx': i, 'cmd': c, 'active': rc == 0})\n"
        "print(json.dumps(out))\n"
    )
    try:
        raw = ssh_exec(state["last"], f"python3 -c {json.dumps(probe_py)}", timeout=25)
        items = json.loads(raw.splitlines()[-1]) if raw else []
    except Exception as exc:
        return target_banner + f"❌ <b>Lỗi đọc danh sách App:</b> <code>{html.escape(str(exc))}</code>", None

    if not items:
        text = (
            target_banner
            + "🚀 <b>APP CHẠY NGẦM (TỰ ĐỘNG BẬT LẠI SAU 12H)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<i>Hiện chưa có app nào được deploy.</i>\n\n"
            "💡 <b>Cách dùng cực kỳ đơn giản:</b>\n"
            "• Trên Telegram: gõ <code>/deploy python3 bot.py</code>\n"
            "• Hoặc trong SSH: gõ <code>deploy python3 bot.py</code>\n\n"
            "👉 Hệ thống sẽ tự tạo service chạy ngầm trong <code>/home/marimo</code>, tự bật lại nếu crash, và <b>tự động chạy lại mỗi khi VPS reset 12h!</b>"
        )
        return text, None

    rows = []
    buttons = []
    for it in items:
        i = it["idx"]
        st_icon = "🟢 Đang chạy" if it["active"] else "🔴 Đã dừng"
        rows.append(
            f"<b>#{i}</b> {st_icon}\n"
            f"   💻 <code>{html.escape(it['cmd'])}</code>\n"
            f"   📄 Log: <code>/home/marimo/.logs/app-{i}.log</code>"
        )
        buttons.append(
            [
                {"text": f"📜 Log #{i}", "callback_data": f"/applog {i}"},
                {"text": f"🔄 Khởi động lại #{i}", "callback_data": f"/reapp {i}"},
                {"text": f"🗑 Xoá #{i}", "callback_data": f"/undep {i}"},
            ]
        )
    buttons.append([{"text": "🔄 Làm mới trạng thái", "callback_data": "/apps"}, {"text": "🔙 Bảng chính", "callback_data": "/help"}])
    text = (
        target_banner
        + "🚀 <b>DANH SÁCH APP CHẠY NGẦM (TỰ SỐNG LẠI SAU 12H)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        + "\n\n".join(rows)
        + "\n━━━━━━━━━━━━━━━━━━━━━━\n"
        "➕ <b>Thêm app mới:</b> <code>/deploy &lt;lệnh_chạy&gt;</code>\n"
        "💡 <i>Bấm các nút bên dưới để xem log, khởi động lại hoặc xoá nhanh:</i>"
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
            sender = cb.get("from") or {}
            chat_id = str((msg.get("chat") or {}).get("id") or "").strip()
            text = (cb.get("data") or "").strip()
        else:
            msg = item.get("message") or item.get("edited_message") or {}
            sender = msg.get("from") or {}
            chat_id = str((msg.get("chat") or {}).get("id") or "").strip()
            text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            continue
        allowed = get_allowed_ids(cfg)
        if chat_id not in allowed:
            full_name = " ".join(x for x in [sender.get("first_name"), sender.get("last_name")] if x).strip() or "Không rõ tên"
            uname = f"@{sender['username']}" if sender.get("username") else "Không có @username"
            if text.split()[0].lower() == "/req_access":
                PENDING_ACCESS_REQS[chat_id] = {"name": full_name, "username": uname, "ts": time.time()}
                req_kb = {
                    "inline_keyboard": [
                        [
                            {"text": f"✅ Duyệt Member ({chat_id})", "callback_data": f"/addid {chat_id}"},
                            {"text": "🚫 Từ chối", "callback_data": f"/rejectid {chat_id}"},
                        ]
                    ]
                }
                send_telegram(
                    cfg,
                    "🔔 <b>YÊU CẦU CẤP QUYỀN MEMBER MỚI</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>Người gửi:</b> {html.escape(full_name)} ({html.escape(uname)})\n"
                    f"🆔 <b>Telegram ID:</b> <code>{html.escape(chat_id)}</code>\n\n"
                    "👇 <i>Bấm nút bên dưới để duyệt cấp quyền Member 1 chạm:</i>",
                    chat_id=ADMIN_ID,
                    reply_markup=req_kb,
                )
                send_telegram(
                    cfg,
                    "✅ <b>ĐÃ GỬI YÊU CẦU TỚI ADMIN!</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>ID của bạn:</b> <code>{html.escape(chat_id)}</code>\n"
                    "• Vui lòng chờ Admin phê duyệt. Ngay khi được duyệt, bot sẽ tự động gửi thông báo kèm hướng dẫn vận hành A–Z cho bạn!",
                    chat_id=chat_id,
                    menu=False,
                )
            else:
                stranger_kb = {
                    "inline_keyboard": [
                        [{"text": "📨 Gửi yêu cầu cấp quyền Member cho Admin", "callback_data": "/req_access"}]
                    ]
                }
                send_telegram(
                    cfg,
                    "🔒 <b>TÀI KHOẢN CHƯA ĐƯỢC CẤP QUYỀN MEMBER</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👋 Xin chào <b>{html.escape(full_name)}</b>!\n"
                    f"🆔 <b>ID Telegram của bạn:</b> <code>{html.escape(chat_id)}</code>\n\n"
                    "Hệ thống <b>Molab Cloud GPU VPS (RTX Pro 6000)</b> chỉ dành cho thành viên được Admin phê duyệt.\n"
                    "👉 <b>Bấm nút bên dưới</b> để gửi yêu cầu cấp quyền trực tiếp tới Admin (hoặc gửi mã lệnh <code>/addid "
                    + html.escape(chat_id)
                    + "</code> cho Admin):",
                    chat_id=chat_id,
                    menu=False,
                    reply_markup=stranger_kb,
                )
            continue

        is_admin = chat_id == ADMIN_ID
        target_uid = admin_state.get("target", ADMIN_ID) if is_admin else chat_id
        if target_uid not in allowed:
            target_uid = ADMIN_ID
            admin_state["target"] = ADMIN_ID
        target_u = get_user_cfg(cfg, target_uid)
        target_nb = get_user_nb(cfg, target_uid)
        target_idx = target_nb["idx"]
        state = active_sessions.get(session_key(target_uid, target_idx))
        target_banner = (
            f"🎯 <i>[Đang điều khiển <b>VPS #{target_idx}</b> của ID <code>{html.escape(target_uid)}</code> — gõ <code>/use me</code> để về máy chính]</i>\n\n"
            if (is_admin and (target_uid != ADMIN_ID or target_idx != 1))
            else (f"🖥 <i>[Đang chọn <b>VPS #{target_idx}</b> / {len(target_u['notebooks'])} máy — gõ <code>/vps</code> để đổi máy]</i>\n\n" if len(target_u["notebooks"]) > 1 else "")
        )

        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        msg_id = msg.get("message_id")
        if msg_id and not cb:
            react_telegram(cfg, chat_id, msg_id, "🔥" if cmd in ("/deploy", "/newvps", "/restart") else "⚡")

        if cmd in ("/start", "/help"):
            send_telegram(cfg, target_banner + get_bot_help(is_admin=is_admin), chat_id=chat_id)
        elif cmd == "/guide":
            send_telegram(cfg, target_banner + get_member_guide(cfg, target_uid), chat_id=chat_id)
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
            raw_arg = arg.strip()
            force_new = raw_arg.lower() == "new"
            if raw_arg and not force_new:
                req_uid, req_idx = parse_target_spec(raw_arg, target_uid, cfg)
                if not is_admin and req_uid != chat_id:
                    req_uid = chat_id
            else:
                req_uid, req_idx = target_uid, (None if force_new else target_idx)
            nb_preview = get_user_nb(cfg, req_uid, idx=req_idx) if not force_new else None
            if not force_new and nb_preview and not nb_preview.get("notebook_url"):
                force_new_msg = True
            else:
                force_new_msg = force_new
            send_telegram(
                cfg,
                f"⏳ <b>Đang {'khởi tạo Notebook GPU mới & ' if force_new_msg else ''}bật VPS cho ID <code>{html.escape(req_uid)}</code>...</b>\n"
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
                idx=req_idx,
                force_new_notebook=force_new,
                notify_chat_id=chat_id,
            )
            if not ok:
                send_telegram(cfg, target_banner + msg_out, chat_id=chat_id)
        elif cmd == "/stopvps":
            if arg.strip():
                req_uid, req_idx = parse_target_spec(arg.strip(), target_uid, cfg)
                if not is_admin and req_uid != chat_id:
                    req_uid = chat_id
            else:
                req_uid, req_idx = target_uid, target_idx
            st = active_sessions.get(session_key(req_uid, req_idx))
            if st and st.get("last"):
                sync_remote(cfg, st["last"], mode="--push", uid=req_uid, idx=req_idx)
            if stop_user_vps(req_uid, active_sessions, idx=req_idx):
                send_telegram(cfg, f"🛑 <b>Đã lưu dữ liệu & tắt VPS #{req_idx} của ID <code>{html.escape(req_uid)}</code>.</b>", chat_id=chat_id)
            else:
                send_telegram(cfg, f"⚪ <b>VPS #{req_idx} của ID <code>{html.escape(req_uid)}</code> hiện đang tắt.</b>", chat_id=chat_id)
        elif cmd == "/vps":
            card_text, markup = format_vps_card(cfg, active_sessions, chat_id, target_uid, target_idx, is_admin=is_admin)
            send_telegram(cfg, card_text, chat_id=chat_id, reply_markup=markup)
        elif cmd == "/use":
            pick = arg.strip()
            if pick in ("", "me", "admin"):
                if is_admin:
                    admin_state["target"] = ADMIN_ID
                u = get_user_cfg(cfg, ADMIN_ID if is_admin else chat_id)
                u["active_idx"] = 1
                save_config(cfg)
                send_telegram(cfg, "🎯 <b>Đã chuyển điều khiển về VPS #1 của bạn.</b>", chat_id=chat_id)
                continue
            req_uid, req_idx = parse_target_spec(pick, target_uid, cfg)
            if not is_admin and req_uid != chat_id:
                send_telegram(cfg, "⛔ <b>Bạn chỉ có thể chuyển đổi giữa các VPS của chính mình (VD: <code>/use 1</code>, <code>/use 2</code>).</b>", chat_id=chat_id)
                continue
            if req_uid not in allowed:
                send_telegram(cfg, f"⚠️ ID <code>{html.escape(req_uid)}</code> chưa có trong danh sách Member.", chat_id=chat_id)
                continue
            u = get_user_cfg(cfg, req_uid)
            if not (1 <= int(req_idx) <= len(u["notebooks"])):
                send_telegram(
                    cfg,
                    f"⚠️ ID <code>{html.escape(req_uid)}</code> hiện chỉ có {len(u['notebooks'])} VPS. Gõ <code>/newvps new</code> để tạo thêm VPS mới!",
                    chat_id=chat_id,
                )
                continue
            u["active_idx"] = int(req_idx)
            save_config(cfg)
            if is_admin:
                admin_state["target"] = req_uid
            nb_sel = get_user_nb(cfg, req_uid, idx=req_idx)
            send_telegram(
                cfg,
                f"🎯 <b>Đã chuyển điều khiển sang VPS #{req_idx} của ID <code>{html.escape(req_uid)}</code></b>\n"
                f"• <b>Lệnh SSH:</b> <code>ssh -o ServerAliveInterval=30 -p {nb_sel['static_port']} molab@bore.pub</code>\n"
                f"• <b>Mật khẩu:</b> <code>{html.escape(nb_sel['static_password'])}</code>",
                chat_id=chat_id,
            )
        elif cmd == "/ssh":
            if not state:
                send_telegram(
                    cfg,
                    target_banner
                    + f"⚠️ <b>VPS #{target_idx} của ID <code>{html.escape(target_uid)}</code> chưa chạy.</b>\n"
                    f"📌 <b>Địa chỉ cố định của máy này:</b>\n<code>ssh -o ServerAliveInterval=30 -p {target_nb['static_port']} molab@bore.pub</code>\n"
                    f"🔐 <b>Mật khẩu cố định:</b> <code>{html.escape(target_nb['static_password'])}</code>\n\n"
                    "Bấm nút <b>🚀 Bật VPS hiện tại</b> (<code>/newvps</code>) để khởi động!",
                    chat_id=chat_id,
                )
                continue
            send_telegram(cfg, target_banner + format_ssh_card(cfg, state["last"], state, uid=target_uid, idx=target_idx), chat_id=chat_id)
        elif cmd == "/setpass":
            sub = arg.split()
            pw_uid, pw_idx = target_uid, target_idx
            if len(sub) >= 2:
                cand_uid, cand_idx = parse_target_spec(sub[0], target_uid, cfg)
                if (is_admin and cand_uid in allowed) or (cand_uid == chat_id and sub[0].isdigit()):
                    pw_uid, pw_idx = cand_uid, cand_idx
                    new_pw = sub[1].strip()
                else:
                    new_pw = arg.strip()
            else:
                new_pw = arg.strip()
            pw_u = get_user_cfg(cfg, pw_uid)
            pw_nb = get_user_nb(cfg, pw_uid, idx=pw_idx)
            pw_idx = pw_nb["idx"]
            if len(new_pw) < 4 or any(c.isspace() for c in new_pw):
                admin_hint = "\n• <b>Admin đổi cho Member:</b> <code>/setpass &lt;id&gt;[:số_máy] &lt;mật_khẩu_mới&gt;</code>" if is_admin else ""
                send_telegram(
                    cfg,
                    target_banner
                    + f"🔐 <b>ĐỔI MẬT KHẨU SSH CỐ ĐỊNH (VPS #{pw_idx} — ID {html.escape(pw_uid)})</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Mật khẩu hiện tại:</b> <code>{html.escape(pw_nb['static_password'])}</code>\n"
                    "• <b>Cách đổi:</b> <code>/setpass &lt;mật_khẩu_mới&gt;</code> (tối thiểu 4 ký tự, không khoảng trắng)"
                    + admin_hint,
                    chat_id=chat_id,
                )
                continue
            pw_nb["static_password"] = new_pw
            if pw_idx == 1:
                pw_u["static_password"] = new_pw
            save_config(cfg)
            pw_state = active_sessions.get(session_key(pw_uid, pw_idx))
            if pw_state and pw_state.get("last"):
                try:
                    import shlex

                    port = int(pw_nb["static_port"])
                    cfg_json = json.dumps({"port": port, "password": new_pw})
                    ssh_exec(
                        pw_state["last"],
                        f"printf '%s\\n' {shlex.quote(f'molab:{new_pw}')} {shlex.quote(f'root:{new_pw}')} | chpasswd && "
                        f"printf '%s\\n' {shlex.quote(cfg_json)} > /home/marimo/.ssh_static.json && chmod 600 /home/marimo/.ssh_static.json",
                        timeout=30,
                    )
                    ensure_static_cell_code(pw_state["page"], cfg, pw_uid, idx=pw_idx)
                    pw_state["last"] = (pw_state["last"][0], new_pw)
                    publish(cfg, pw_state["last"], pw_state, uid=pw_uid, idx=pw_idx, notify_chat_id=chat_id)
                except Exception as exc:
                    send_telegram(
                        cfg,
                        target_banner + f"⚠️ Đã lưu mật khẩu mới cho VPS #{pw_idx} (ID <code>{html.escape(pw_uid)}</code>) nhưng chưa cập nhật trực tiếp qua SSH: <code>{html.escape(str(exc))}</code>",
                        chat_id=chat_id,
                    )
            else:
                send_telegram(
                    cfg,
                    target_banner + f"✅ <b>Đã đặt mật khẩu SSH cố định mới cho VPS #{pw_idx} (ID <code>{html.escape(pw_uid)}</code>):</b> <code>{html.escape(new_pw)}</code>",
                    chat_id=chat_id,
                )
                if pw_uid != chat_id:
                    send_telegram(
                        cfg,
                        f"🔐 <b>Admin đã cập nhật mật khẩu SSH mới cho VPS #{pw_idx} của bạn:</b> <code>{html.escape(new_pw)}</code>",
                        chat_id=pw_uid,
                    )
        elif cmd == "/setport":
            if not arg.strip().isdigit() or not (10000 <= int(arg.strip()) <= 60000):
                send_telegram(
                    cfg,
                    target_banner
                    + f"📌 <b>ĐỔI CỔNG SSH CỐ ĐỊNH (VPS #{target_idx} — bore.pub)</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"• <b>Cổng hiện tại:</b> <code>{target_nb['static_port']}</code>\n"
                    "• <b>Cách đổi:</b> <code>/setport &lt;10000-60000&gt;</code> (ví dụ: <code>/setport 45678</code>)",
                    chat_id=chat_id,
                )
                continue
            new_port = int(arg.strip())
            if new_port in _used_static_ports(cfg, exclude_uid=target_uid, exclude_idx=target_idx):
                send_telegram(cfg, target_banner + f"⚠️ <b>Cổng <code>{new_port}</code> đang được dùng bởi 1 VPS khác trên hệ thống. Hãy chọn cổng khác!</b>", chat_id=chat_id)
                continue
            target_nb["static_port"] = new_port
            if target_idx == 1:
                target_u["static_port"] = new_port
            save_config(cfg)
            if state and state.get("page"):
                send_telegram(
                    cfg,
                    target_banner + f"⏳ <b>Đang chuyển cổng cố định VPS #{target_idx} sang <code>bore.pub:{new_port}</code>...</b>",
                    chat_id=chat_id,
                    menu=False,
                )
                try:
                    import shlex

                    cfg_json = json.dumps({"port": new_port, "password": target_nb["static_password"]})
                    ssh_exec(
                        state["last"],
                        f"printf '%s\\n' {shlex.quote(cfg_json)} > /home/marimo/.ssh_static.json && chmod 600 /home/marimo/.ssh_static.json",
                        timeout=20,
                    )
                except Exception:
                    pass
                ensure_static_cell_code(state["page"], cfg, target_uid, idx=target_idx)
                raw_cur = state.get("raw_last") or ensure_running(state["page"], force_rerun=False)
                if raw_cur:
                    new = attach_bore_bridge(cfg, target_uid, (raw_cur[0], target_nb["static_password"]), state, idx=target_idx)
                    state["last"] = new
                    publish(cfg, new, state, uid=target_uid, idx=target_idx, notify_chat_id=chat_id)
                else:
                    send_telegram(cfg, target_banner + "❌ <b>Không chuyển được sang cổng mới.</b>", chat_id=chat_id)
            else:
                send_telegram(
                    cfg,
                    target_banner + f"✅ <b>Đã lưu cổng SSH cố định mới cho VPS #{target_idx}:</b> <code>bore.pub:{new_port}</code>",
                    chat_id=chat_id,
                )
        elif cmd == "/status":
            send_telegram(cfg, target_banner + get_vps_status_card(cfg, state, uid=target_uid, idx=target_idx), chat_id=chat_id)
        elif cmd == "/rotate":
            if not state:
                send_telegram(cfg, target_banner + "⚠️ <b>VPS chưa bật. Hãy bấm 🚀 Bật VPS hiện tại trước.</b>", chat_id=chat_id)
                continue
            send_telegram(
                cfg,
                target_banner + "⏳ <b>Đang làm mới kết nối tunnel SSH cố định...</b>\n<i>Vui lòng đợi khoảng 10–40 giây.</i>",
                chat_id=chat_id,
                menu=False,
            )
            ensure_static_cell_code(state["page"], cfg, target_uid, idx=target_idx)
            raw_new = ensure_running(state["page"], force_rerun=True)
            if raw_new:
                stop_bore_bridge(state)
                new = attach_bore_bridge(cfg, target_uid, raw_new, state, idx=target_idx)
                state["last"] = new
                state["next_rerun"] = time.monotonic() + cfg["cell_rerun_minutes"] * 60
                publish(cfg, new, state, uid=target_uid, idx=target_idx, notify_chat_id=chat_id)
            else:
                send_telegram(cfg, target_banner + "❌ <b>Không làm mới được tunnel SSH.</b>", chat_id=chat_id)
        elif cmd == "/restart":
            if not state:
                send_telegram(cfg, target_banner + "⚠️ <b>VPS chưa bật. Hãy bấm 🚀 Bật VPS hiện tại trước.</b>", chat_id=chat_id)
                continue
            send_telegram(
                cfg,
                target_banner + "⏳ <b>Đang đồng bộ dữ liệu lên Hugging Face & khởi động lại phiên 12h...</b>",
                chat_id=chat_id,
                menu=False,
            )
            sync_remote(cfg, state["last"], mode="--push", uid=target_uid, idx=target_idx)
            state["page"].reload(wait_until="domcontentloaded", timeout=120000)
            state["page"].wait_for_timeout(5000)
            ensure_gpu_compute(state["page"])
            ensure_static_cell_code(state["page"], cfg, target_uid, idx=target_idx)
            raw_new = ensure_running(state["page"])
            if raw_new:
                now = time.monotonic()
                new = attach_bore_bridge(cfg, target_uid, raw_new, state, idx=target_idx)
                state["last"] = new
                state["next_restart"] = now + cfg["session_restart_minutes"] * 60
                state["next_rerun"] = now + cfg["cell_rerun_minutes"] * 60
                publish(cfg, new, state, uid=target_uid, idx=target_idx, notify_chat_id=chat_id)
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
            out = sync_remote(cfg, state["last"], mode="--push", uid=target_uid, idx=target_idx)
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
        elif cmd == "/apps":
            card_text, markup = format_apps_card(state, target_banner=target_banner)
            send_telegram(cfg, card_text, chat_id=chat_id, reply_markup=markup)
        elif cmd in ("/deploy", "/autostart"):
            if not state:
                send_telegram(cfg, target_banner + "⚠️ <b>VPS chưa bật.</b>", chat_id=chat_id)
                continue
            if not arg:
                card_text, markup = format_apps_card(state, target_banner=target_banner)
                send_telegram(cfg, card_text, chat_id=chat_id, reply_markup=markup)
                continue
            send_telegram(
                cfg,
                target_banner + f"⏳ <b>Đang deploy chạy ngầm & lưu tự động bật lại sau 12h:</b>\n<code>{html.escape(arg)}</code>...",
                chat_id=chat_id,
                menu=False,
            )
            try:
                import shlex

                out = ssh_exec(state["last"], f"/usr/local/bin/save --deploy {shlex.quote(arg)}", timeout=180)
                card_text, markup = format_apps_card(state, target_banner=target_banner)
                send_telegram(
                    cfg,
                    f"🎉 <b>DEPLOY THÀNH CÔNG!</b>\n<code>{html.escape(out or arg)}</code>\n\n" + card_text,
                    chat_id=chat_id,
                    reply_markup=markup,
                    effect_id=EFFECT_PARTY,
                )
            except Exception as exc:
                send_telegram(cfg, target_banner + f"❌ <b>Lỗi deploy:</b> <code>{html.escape(str(exc))}</code>", chat_id=chat_id)
        elif cmd == "/undep":
            if not state:
                send_telegram(cfg, target_banner + "⚠️ <b>VPS chưa bật.</b>", chat_id=chat_id)
                continue
            idx = arg.strip()
            if not idx.isdigit():
                send_telegram(cfg, target_banner + "🗑 <b>Cách dùng:</b> <code>/undep &lt;số_thứ_tự_app&gt;</code> (ví dụ: <code>/undep 1</code>)", chat_id=chat_id)
                continue
            try:
                out = ssh_exec(state["last"], f"/usr/local/bin/save --deploy --del {idx}", timeout=180)
                card_text, markup = format_apps_card(state, target_banner=target_banner)
                send_telegram(
                    cfg,
                    f"🗑 <b>{html.escape(out or f'Đã xoá app #{idx}')}</b>\n\n" + card_text,
                    chat_id=chat_id,
                    reply_markup=markup,
                )
            except Exception as exc:
                send_telegram(cfg, target_banner + f"❌ <b>Lỗi xoá app:</b> <code>{html.escape(str(exc))}</code>", chat_id=chat_id)
        elif cmd == "/reapp":
            if not state:
                send_telegram(cfg, target_banner + "⚠️ <b>VPS chưa bật.</b>", chat_id=chat_id)
                continue
            idx = arg.strip()
            if not idx.isdigit():
                send_telegram(cfg, target_banner + "🔄 <b>Cách dùng:</b> <code>/reapp &lt;số_thứ_tự_app&gt;</code>", chat_id=chat_id)
                continue
            try:
                out = ssh_exec(state["last"], f"/usr/local/bin/save --deploy --restart {idx}", timeout=60)
                card_text, markup = format_apps_card(state, target_banner=target_banner)
                send_telegram(
                    cfg,
                    f"🔄 <b>{html.escape(out or f'Đã khởi động lại app #{idx}')}</b>\n\n" + card_text,
                    chat_id=chat_id,
                    reply_markup=markup,
                )
            except Exception as exc:
                send_telegram(cfg, target_banner + f"❌ <b>Lỗi khởi động lại app:</b> <code>{html.escape(str(exc))}</code>", chat_id=chat_id)
        elif cmd == "/applog":
            if not state:
                send_telegram(cfg, target_banner + "⚠️ <b>VPS chưa bật.</b>", chat_id=chat_id)
                continue
            idx = arg.strip()
            if not idx.isdigit():
                send_telegram(cfg, target_banner + "📜 <b>Cách dùng:</b> <code>/applog &lt;số_thứ_tự_app&gt;</code>", chat_id=chat_id)
                continue
            try:
                out = ssh_exec(state["last"], f"tail -n 35 /home/marimo/.logs/app-{idx}.log 2>/dev/null || echo '(Chưa có log)'", timeout=30)
                markup = {
                    "inline_keyboard": [
                        [
                            {"text": f"🔄 Làm mới Log #{idx}", "callback_data": f"/applog {idx}"},
                            {"text": f"🔄 Restart #{idx}", "callback_data": f"/reapp {idx}"},
                        ],
                        [{"text": "🔙 Danh sách App", "callback_data": "/apps"}],
                    ]
                }
                send_telegram(
                    cfg,
                    target_banner
                    + f"📜 <b>LOG APP #{idx}</b> (<code>/home/marimo/.logs/app-{idx}.log</code>)\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"<pre>{html.escape((out or '(Trống)')[-3200:])}</pre>",
                    chat_id=chat_id,
                    reply_markup=markup,
                )
            except Exception as exc:
                send_telegram(cfg, target_banner + f"❌ <b>Lỗi đọc log:</b> <code>{html.escape(str(exc))}</code>", chat_id=chat_id)
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
                    f"• <b>Làm mới đường truyền ngầm:</b> <code>{cfg['cell_rerun_minutes']} phút</code>\n"
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
                f"✅ <b>Đã lưu chu kỳ mới:</b>\n• Làm mới đường truyền: <code>{cfg['cell_rerun_minutes']} phút</code>\n• Reset phiên: <code>{cfg['session_restart_minutes']} phút</code>",
                chat_id=chat_id,
            )
        elif cmd == "/ids":
            if not is_admin:
                send_telegram(cfg, "⛔ <b>Chỉ Admin mới có quyền quản lý danh sách Member.</b>", chat_id=chat_id)
                continue
            rows = []
            buttons = []
            for i in allowed:
                u = get_user_cfg(cfg, i)
                running_cnt = sum(1 for st in active_sessions.values() if str(st.get("uid")) == str(i))
                st_str = f"🟢 {running_cnt}/{len(u['notebooks'])} VPS đang chạy" if running_cnt else f"⚪ {len(u['notebooks'])} VPS (đang tắt)"
                mail_str = u.get("email") or ("Chưa liên kết" if not u.get("logged_in") else "Đã liên kết")
                rows.append(
                    f"• <code>{html.escape(i)}</code> {'👑 <i>(Admin)</i>' if i == ADMIN_ID else '👤 Member'} — {st_str} ({html.escape(mail_str)})"
                )
            if PENDING_ACCESS_REQS:
                rows.append("\n🔔 <b>Đang chờ duyệt Member:</b>")
                for req_id, req_info in list(PENDING_ACCESS_REQS.items()):
                    rows.append(f"• <code>{html.escape(req_id)}</code> — {html.escape(req_info.get('name', ''))} ({html.escape(req_info.get('username', ''))})")
                    buttons.append(
                        [
                            {"text": f"✅ Duyệt {req_id}", "callback_data": f"/addid {req_id}"},
                            {"text": f"🚫 Từ chối {req_id}", "callback_data": f"/rejectid {req_id}"},
                        ]
                    )
            send_telegram(
                cfg,
                "👥 <b>DANH SÁCH MEMBER ĐƯỢC CẤP QUYỀN</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                + "\n".join(rows)
                + "\n\n➕ Duyệt Member: <code>/addid &lt;id&gt;</code>  |  ➖ Thu hồi: <code>/delid &lt;id&gt;</code>\n"
                "🔐 Đổi mật khẩu Member: <code>/setpass &lt;id&gt; &lt;mk_mới&gt;</code>\n"
                "🖥 Quản lý toàn bộ VPS: <code>/vps</code>",
                chat_id=chat_id,
                reply_markup={"inline_keyboard": buttons} if buttons else None,
            )
        elif cmd == "/addid":
            if not is_admin:
                send_telegram(cfg, "⛔ <b>Chỉ Admin mới có quyền duyệt Member mới.</b>", chat_id=chat_id)
                continue
            new_id = arg.strip()
            if not re.match(r"^-?\d+$", new_id):
                send_telegram(cfg, "👤 <b>Duyệt Member mới:</b> <code>/addid 123456789</code>", chat_id=chat_id)
                continue
            PENDING_ACCESS_REQS.pop(new_id, None)
            if new_id not in allowed:
                allowed.append(new_id)
                cfg["telegram_allowed_ids"] = allowed
                save_config(cfg)
            u_new = get_user_cfg(cfg, new_id)
            nb_new = get_user_nb(cfg, new_id, idx=1)
            send_telegram(
                cfg,
                f"✅ <b>Đã cấp quyền Member cho ID:</b> <code>{html.escape(new_id)}</code>\n"
                f"• <b>Cổng SSH mặc định VPS #1:</b> <code>bore.pub:{nb_new['static_port']}</code>\n"
                f"• <b>Mật khẩu mặc định:</b> <code>{html.escape(nb_new['static_password'])}</code>\n"
                f"• <b>Đổi mật khẩu cho Member này:</b> <code>/setpass {html.escape(new_id)} &lt;mk_mới&gt;</code>",
                chat_id=chat_id,
            )
            if new_id != chat_id:
                send_telegram(
                    cfg,
                    "🎉 <b>CHÚC MỪNG! BẠN ĐÃ ĐƯỢC ADMIN DUYỆT QUYỀN MEMBER!</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    + get_member_guide(cfg, new_id),
                    chat_id=new_id,
                    effect_id=EFFECT_PARTY,
                )
        elif cmd == "/rejectid":
            if not is_admin:
                continue
            rej_id = arg.strip()
            PENDING_ACCESS_REQS.pop(rej_id, None)
            send_telegram(cfg, f"🚫 <b>Đã từ chối yêu cầu cấp quyền của ID <code>{html.escape(rej_id)}</code>.</b>", chat_id=chat_id)
            if re.match(r"^-?\d+$", rej_id):
                send_telegram(
                    cfg,
                    "🚫 <b>Yêu cầu cấp quyền Member của bạn chưa được Admin phê duyệt.</b>",
                    chat_id=rej_id,
                    menu=False,
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
                send_telegram(cfg, f"✅ <b>Đã tắt toàn bộ VPS & thu hồi quyền Member của ID:</b> <code>{html.escape(rem_id)}</code>", chat_id=chat_id)
            else:
                send_telegram(cfg, f"⚠️ Không tìm thấy ID <code>{html.escape(rem_id)}</code>.", chat_id=chat_id)


def session(cfg):
    with sync_playwright() as playwright:
        active_sessions = {}
        pending_logins = {}
        admin_state = {"target": ADMIN_ID}
        try:
            ok, err = provision_or_start_user_vps(
                playwright, cfg, ADMIN_ID, active_sessions, pending_logins, idx=1, force_new_notebook=False
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
                for skey, st in list(active_sessions.items()):
                    uid = st.get("uid", ADMIN_ID)
                    nb_idx = st.get("idx", 1)
                    page = st["page"]
                    try:
                        if now >= st["next_heartbeat"]:
                            page.mouse.move(5, 5)
                            page.evaluate("1")
                            st["next_heartbeat"] = now + 30

                        if now >= st["next_restart"]:
                            logging.info("Reload notebook [%s#%s] (session 12h)", uid, nb_idx)
                            sync_remote(cfg, st["last"], mode="--push", uid=uid, idx=nb_idx)
                            page.reload(wait_until="domcontentloaded", timeout=120000)
                            page.wait_for_timeout(5000)
                            ensure_gpu_compute(page)
                            ensure_static_cell_code(page, cfg, uid, idx=nb_idx)
                            raw_new = ensure_running(page)
                            st["next_restart"] = now + cfg["session_restart_minutes"] * 60
                            st["next_rerun"] = now + cfg["cell_rerun_minutes"] * 60
                            st["next_healthcheck"] = now + 180
                            if raw_new:
                                new = attach_bore_bridge(cfg, uid, raw_new, st, idx=nb_idx)
                                st["last"] = new
                                publish(cfg, new, st, uid=uid, idx=nb_idx)
                            else:
                                page.screenshot(path=str(ROOT / "error.png"), full_page=True)
                                logging.error("Reload [%s#%s] xong nhưng không thấy SSH", uid, nb_idx)
                            continue

                        if now >= st.get("next_healthcheck", 0):
                            st["next_healthcheck"] = now + 180
                            if st.get("last") and not tunnel_alive(st["last"]):
                                logging.warning("Phát hiện tunnel [%s#%s] mất kết nối sớm, tự động khôi phục...", uid, nb_idx)
                                ensure_static_cell_code(page, cfg, uid, idx=nb_idx)
                                raw_new = ensure_running(page, force_rerun=True)
                                if raw_new:
                                    stop_bore_bridge(st)
                                    new = attach_bore_bridge(cfg, uid, raw_new, st, idx=nb_idx)
                                    st["last"] = new
                                    st["next_rerun"] = now + cfg["cell_rerun_minutes"] * 60

                        if now >= st["next_rerun"]:
                            logging.info("Làm mới ngầm đường truyền nền [%s#%s] (giữ nguyên cổng cố định bore.pub)", uid, nb_idx)
                            ensure_static_cell_code(page, cfg, uid, idx=nb_idx)
                            raw_new = ensure_running(page, force_rerun=True)
                            st["next_rerun"] = now + cfg["cell_rerun_minutes"] * 60
                            if raw_new:
                                new = attach_bore_bridge(cfg, uid, raw_new, st, idx=nb_idx)
                                if new != st["last"]:
                                    publish(cfg, new, st, uid=uid, idx=nb_idx)
                                st["last"] = new
                            else:
                                page.screenshot(path=str(ROOT / "error.png"), full_page=True)
                                logging.error("Rerun [%s#%s] không ra địa chỉ mới", uid, nb_idx)
                    except Exception as exc:
                        logging.warning("Lỗi vòng lặp VPS [%s#%s]: %s", uid, nb_idx, exc)
        finally:
            for st in list(active_sessions.values()):
                stop_user_vps(st.get("uid", ADMIN_ID), active_sessions)
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
