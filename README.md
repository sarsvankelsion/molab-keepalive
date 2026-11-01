# molab-keepalive

Biến notebook **molab (marimo cloud)** GPU (`RTX Pro 6000`) thành Cloud VPS chạy ngầm 24/7:
- **OpenSSH (`sshd`) thật + SFTP/SCP + VS Code Remote SSH** qua Pinggy TCP tunnel.
- **Giả lập `systemd` (`systemctl`)** ngay trong container (`systemctl start/stop/restart/status ...`).
- **Tự động giữ sống & xoay cổng**: xoay tunnel Pinggy mỗi **55 phút** và tự khởi tạo phiên mới mỗi **12 giờ**.
- **Lưu trữ bền vững trên Hugging Face Dataset (Private)**: tự động backup `/home/marimo` mỗi 5 phút, tự nhớ & cài lại các gói `apt` / `pip` khi sang phiên 12h mới.
- **Telegram Bot Control Panel**: điều khiển VPS từ xa qua bàn phím nút bấm (Inline Keyboard), xem trạng thái GPU/CPU/RAM/Disk, chạy lệnh bash, quản lý gói và phân quyền nhiều ID.

## Cài đặt

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
copy config.example.json config.json
```

Điền thông tin vào `config.json`:
- `notebook_url`: link notebook molab chứa cell `molab_ssh_pinggy.py`
- `telegram_bot_token` & `telegram_chat_id`: bot Telegram nhận thông báo và điều khiển
- `hf_token` & `hf_repo`: Hugging Face token (quyền Write) và tên dataset repo private (ví dụ `username/molab-backup`)

## Đăng nhập molab lần đầu

```powershell
python keepalive.py --login
```

Đăng nhập tài khoản molab trên cửa sổ Chromium hiện ra,script sẽ tự lưu cookie vào `profile/` và đóng trình duyệt.

## Chạy ngầm 24/7

Chạy trực tiếp hoặc dùng `pythonw.exe` (không hiện cửa sổ):

```powershell
Start-Process pythonw.exe -ArgumentList "keepalive.py" -WorkingDirectory "$PWD" -WindowStyle Hidden
```

## Lệnh trên VPS (qua SSH)

- `save` — Đẩy ngay `/home/marimo` và danh sách gói lên Hugging Face.
- `save <đường_dẫn_file>` — Gửi file trực tiếp về Telegram và đồng bộ Hugging Face.
- `systemctl status ssh` — Quản lý dịch vụ bằng `systemctl` giả lập.
