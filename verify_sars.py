import sys, os, json, time, html
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "controller_state"
PROFILE_DIR = Path(os.path.expanduser("~\\AppData\\Local\\Temp\\test_clerk_persist"))

def complete_login(code):
    from playwright.sync_api import sync_playwright
    import keepalive

    cfg = json.load(open(ROOT / "config.json", encoding="utf-8"))
    tok = cfg.get("hf_token")
    repo = "lesslosster/molab-controller-state"

    print(f"Applying OTP code [{code}] to pending session...")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(user_data_dir=str(PROFILE_DIR), headless=True)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://molab.marimo.io/sign-in/factor-one", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)

        otp = page.locator("input[data-input-otp='true'], input[autocomplete='one-time-code']").first
        if not otp.count():
            print("OTP input not found on page! Current URL:", page.url)
            ctx.close()
            return False

        otp.click()
        otp.fill(str(code).strip())
        page.wait_for_timeout(2500)

        btn = page.locator("button.cl-formButtonPrimary, button:has-text('Continue')").first
        if btn.count() and btn.is_visible():
            try:
                btn.click(timeout=3000)
            except Exception:
                pass

        for _ in range(12):
            page.wait_for_timeout(1500)
            if page.locator("input[name='firstName']").count():
                page.locator("input[name='firstName']").first.fill("Molab")
                if page.locator("input[name='lastName']").count():
                    page.locator("input[name='lastName']").first.fill("User")
                page.locator("button.cl-formButtonPrimary").first.click()
                page.wait_for_timeout(2500)
            if "/sign-in" not in page.url and "/sign-up" not in page.url:
                break

        print("Final URL:", page.url)
        if "/sign-in" in page.url:
            print("Login failed or code invalid. Body:", page.inner_text("body")[:300].replace("\n", " "))
            ctx.close()
            return False

        # Extract and save cookies
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        st_path = STATE_DIR / "storage_state_7521313803.json"
        ctx.storage_state(path=str(st_path))
        print("Saved storage_state to", st_path)

        # Update local config
        cfg_path = ROOT / "config.json"
        cfg_data = json.load(open(cfg_path, encoding="utf-8"))
        u = cfg_data.get("users", {}).get("7521313803", {})
        u["email"] = "sarsvankelsion@gmail.com"
        u["logged_in"] = True
        u["notebook_url"] = ""
        if u.get("notebooks"):
            u["notebooks"][0]["notebook_url"] = ""
        cfg_data["notebook_url"] = ""
        json.dump(cfg_data, open(cfg_path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        json.dump(cfg_data, open(STATE_DIR / "config.json", "w", encoding="utf-8"), indent=2, ensure_ascii=False)

        # Push to Hugging Face
        print("Pushing updated state to Hugging Face...")
        keepalive._do_push_controller_state(tok, repo)
        print("Successfully synced state to Hugging Face!")
        ctx.close()
        return True

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python verify_sars.py <6-digit-code>")
        sys.exit(1)
    ok = complete_login(sys.argv[1])
    sys.exit(0 if ok else 1)
