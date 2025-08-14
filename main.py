from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
import pathlib, time
import time
import os
from datetime import datetime

# ---- Config (ASCII-safe) ----
BASE_URL   = "https://phon.nytud.hu/beast2/"
FILES_DIR  = pathlib.Path("/home/szabol/podtest") #/home/datasets/raw-data/podcasts
OUTPUT_DIR = pathlib.Path("/home/szabol/leiratok")

# Global sleep between UI steps
sleep_t = 2

# Headless can be overridden by env HEADLESS=0/1/true/false
def _env_bool(name, default=True):
    val = os.environ.get(name)
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")

HEADLESS      = _env_bool("HEADLESS", True)
NAV_TIMEOUT   = 60_000
STEP_TIMEOUT  = 30_000

# Browser selection via env BROWSER=chromium|firefox|webkit
BROWSER_NAME  = os.environ.get("BROWSER", "chromium").strip().lower()

DEBUG_HTML = pathlib.Path("debug_page.html")
DEBUG_PNG  = pathlib.Path("debug.png")

# ---- Helpers ----
def say_time():
    now = datetime.now()
    print("Time now:", now.strftime("%H:%M:%S"))

def add_to_visited(text):
    """
    Append text to visited.txt if not present. If already present -> True, else append and return False/None.
    """
    filepath = os.path.join(".", "visited.txt")

    if not os.path.exists(filepath):
        with open(filepath, "w", encoding="utf-8"):
            pass

    with open(filepath, "r", encoding="utf-8") as f:
        visited = {line.strip() for line in f if line.strip()}

    if text in visited:
        return True

    with open(filepath, "a", encoding="utf-8") as f:
        f.write(text + "\n")

def check_and_add_visited(text):
    """
    Return True if text already in visited.txt, else False (does NOT add).
    """
    filepath = os.path.join(".", "visited.txt")

    if not os.path.exists(filepath):
        with open(filepath, "w", encoding="utf-8"):
            pass

    with open(filepath, "r", encoding="utf-8") as f:
        visited = {line.strip() for line in f if line.strip()}

    if text in visited:
        return True

    return False

_start_time = None
def timer(action="start"):
    global _start_time
    if action == "start":
        _start_time = time.time()
        print("Timer started...")
    elif action == "stop":
        if _start_time is None:
            print("Start the timer first!")
        else:
            elapsed = time.time() - _start_time
            print(f"Elapsed: {elapsed:.3f} sec")
            _start_time = None
    else:
        print("Unknown timer action. Use: timer('start') or timer('stop')")

def dump_debug(page, reason=""):
    try:
        DEBUG_HTML.write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(DEBUG_PNG), full_page=True)
        print(f"[DEBUG] Saved DOM+shot -> {DEBUG_HTML}, {DEBUG_PNG}. {reason}")
    except Exception as e:
        print("[DEBUG] dump_debug error:", e)

def list_all_buttons(page):
    try:
        btns = page.locator("button")
        n = btns.count()
        print(f"[DEBUG] Buttons on page: {n}")
        for i in range(min(n, 80)):
            b = btns.nth(i)
            try:
                label = b.get_attribute("aria-label")
            except Exception:
                label = None
            try:
                txt = b.inner_text().strip()
            except Exception:
                txt = ""
            print(f"  #{i:02d} aria-label={label!r} text={txt!r}")
    except Exception as e:
        print("[DEBUG] list_all_buttons error:", e)

def wait_for_file_selected(page, input_sel, seconds=10):
    """Wait until file input has files.length > 0."""
    deadline = time.time() + seconds
    li = page.locator(input_sel).first
    while time.time() < deadline:
        try:
            li.wait_for(state="attached", timeout=1000)
            has = li.evaluate("el => !!(el && el.files && el.files.length > 0)")
            if has:
                return True
        except Exception:
            pass
        time.sleep(sleep_t)
    return False

def wait_for_nonempty_textarea(page, locator_str):
    """
    Wait indefinitely until textarea has non-empty value.
    """
    loc = page.locator(locator_str)
    while True:
        try:
            loc.wait_for(state="attached", timeout=1000)
            try:
                val = (loc.input_value() or "").strip()
            except Exception:
                val = (loc.evaluate("el => el.value || ''") or "").strip()
            if val:
                print("[DEBUG] Output textarea non-empty, length:", len(val))
                return val
        except Exception:
            pass
        time.sleep(sleep_t)

def click_submit_with_retries(page):
    """Robust 'Submit' click with multiple attempts."""
    btn_css = "gradio-app #component-5"
    btn = page.locator(btn_css)
    btn.wait_for(state="attached", timeout=10_000)
    try:
        btn.scroll_into_view_if_needed(timeout=2_000)
    except Exception:
        pass
    time.sleep(sleep_t)

    for _ in range(10):
        try:
            dis = btn.get_attribute("disabled")
            if not dis:
                break
        except Exception:
            pass
        time.sleep(sleep_t)

    try:
        btn.click()
        print("[DEBUG] Submit: normal click.")
        return True
    except Exception as e:
        print("[DEBUG] Submit normal click error:", e)

    time.sleep(sleep_t)
    try:
        btn.click(force=True)
        print("[DEBUG] Submit: force click.")
        return True
    except Exception as e:
        print("[DEBUG] Submit force click error:", e)

    time.sleep(sleep_t)
    try:
        page.evaluate("""sel => { const el = document.querySelector(sel); if (el) el.click(); }""", btn_css)
        print("[DEBUG] Submit: JS click.")
        return True
    except Exception as e:
        print("[DEBUG] Submit JS click error:", e)

    return False

def tick_checkboxes(page):
    """Tick the two 'Extra Features' checkboxes (#component-6)."""
    cb1 = page.locator("gradio-app #component-6 input[type='checkbox'][name='Punctuation and Capitalization']").first
    cb2 = page.locator("gradio-app #component-6 input[type='checkbox'][name='Diarization']").first

    for name, cb in [("Punctuation and Capitalization", cb1), ("Diarization", cb2)]:
        try:
            cb.wait_for(state="attached", timeout=3000)
            if not cb.is_checked():
                cb.check(force=True)
                print(f"[DEBUG] Checkbox checked: {name}")
            else:
                print(f"[DEBUG] Checkbox already checked: {name}")
        except Exception as e:
            print(f"[WARN] Could not check: {name} -> {e}")

# ---- Main ----
def main():
    global sleep_t
    sleep_t = 1  # faster steps

    allowed_exts = {".mp3", ".m4a"}
    all_files = []

    for p in FILES_DIR.rglob("*"):  # recursive search
        if p.is_file():
            all_files.append(p)

    files = []
    for p in all_files:
        ext = p.suffix.lower()
        if ext in allowed_exts and check_and_add_visited(p.name) == False:
            files.append(p)

    if not files:
        print(f"[ERROR] No .mp3/.m4a file to process in '{FILES_DIR}'.")
        return

    skipped = [p for p in all_files if p.suffix.lower() not in allowed_exts]
    if skipped:
        print("[INFO] Skipped (not mp3/m4a):", ", ".join(s.name for s in skipped))

    with sync_playwright() as p:
        # Launch per selected browser
        if BROWSER_NAME == "chromium":
            launch_args = [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
            browser = p.chromium.launch(headless=HEADLESS, args=launch_args)
        elif BROWSER_NAME == "firefox":
            browser = p.firefox.launch(headless=HEADLESS)
        elif BROWSER_NAME == "webkit":
            browser = p.webkit.launch(headless=HEADLESS)
        else:
            print(f"[WARN] Unknown BROWSER='{BROWSER_NAME}', falling back to chromium.")
            browser = p.chromium.launch(headless=HEADLESS)

        context  = browser.new_context()
        page     = context.new_page()
        page.set_default_timeout(STEP_TIMEOUT)

        print("[DEBUG] Navigating:", BASE_URL)
        page.goto(BASE_URL, timeout=NAV_TIMEOUT)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(4)

        list_all_buttons(page)
        time.sleep(sleep_t)

        for f in files:

            print("\n[INFO] Uploading:", f.name)

            # 0) Refresh
            try:
                page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                print("[DEBUG] Page reloaded.")
            except PWTimeoutError:
                print("[WARN] Reload timeout using goto fallback")
                page.goto(BASE_URL, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")
            except Exception as e:
                print("[WARN] Reload error:", e, " trying goto")
                page.goto(BASE_URL, timeout=NAV_TIMEOUT, wait_until="domcontentloaded")

            time.sleep(sleep_t)

            # 1) Source: Upload file (just in case)
            try:
                page.locator('gradio-app #component-2 .source-selection button[aria-label="Upload file"]').first.click(timeout=1500, force=True)
                print("[DEBUG] Source set to 'Upload file'.")
            except Exception:
                print("[DEBUG] Source selection skipped (probably already active).")
            time.sleep(sleep_t)

            # 2) File input
            upload_area    = page.locator("gradio-app #component-2 .audio-container button").first
            file_input_sel = "gradio-app #component-2 input[data-testid='file-upload']"

            try:
                with page.expect_file_chooser(timeout=5000) as fc_info:
                    upload_area.click()
                chooser = fc_info.value
                chooser.set_files(str(f))
                print("[DEBUG] File sent via file chooser.")
            except Exception as e:
                print("[DEBUG] File chooser not shown (", e, ") -> direct input attempt")
                try:
                    page.set_input_files(file_input_sel, str(f))
                    print("[DEBUG] File sent via set_input_files.")
                except Exception as e2:
                    print("[ERROR] Could not provide file:", e2)
                    dump_debug(page, reason="file upload failed")
                    continue

            time.sleep(sleep_t)

            # 2/D) Extra checkboxes before submit
            tick_checkboxes(page)
            time.sleep(sleep_t)

            # 3) Submit
            ok = click_submit_with_retries(page)
            time.sleep(sleep_t)
            if not ok:
                dump_debug(page, reason="Submit not clickable")
                raise RuntimeError("Could not click 'Submit' button.")

            try:
                has_processing = page.locator("gradio-app .progress-text").count() > 0
                if not has_processing:
                    print("[DEBUG] No 'processing' visible -> uploading again")
                    click_submit_with_retries(page)
                    time.sleep(sleep_t)

            except Exception:
                pass

            timer("start")
            say_time()

            # 4) Output
            textarea_sel = "gradio-app #component-10 textarea[data-testid='textbox']"
            text_value = wait_for_nonempty_textarea(page, textarea_sel)
            time.sleep(sleep_t)

            # 5) TextGrid link (optional)
            tg_link = ""
            try:
                a = page.locator("gradio-app #component-9 .file-preview a").first
                a.wait_for(state="attached", timeout=2000)
                tg_link = a.get_attribute("href") or ""
                if tg_link:
                    print("[DEBUG] TextGrid:", tg_link)
            except Exception:
                print("[DEBUG] No TextGrid link.")
            time.sleep(sleep_t)

            # 6) Result ID (optional)
            res_id = ""
            try:
                id_div = page.locator("gradio-app #component-12 .prose div").nth(1)
                id_div.wait_for(state="attached", timeout=2000)
                res_id = (id_div.inner_text() or "").strip()
                if res_id:
                    print("[DEBUG] Result ID:", res_id)
            except Exception:
                print("[DEBUG] No Result ID.")
            time.sleep(sleep_t)

            # 7) Save result (mirror input folder structure under OUTPUT_DIR)
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            rel_path = f.relative_to(FILES_DIR)          # keep subfolders
            out_path = (OUTPUT_DIR / rel_path).with_suffix(".txt")  # change ext to .txt
            out_path.parent.mkdir(parents=True, exist_ok=True)      # ensure subdirs exist
            with out_path.open("w", encoding="utf-8") as aout:
                if res_id:
                    aout.write(f"ID: {res_id}\n")
                aout.write(text_value if text_value else "[No output or timeout]\n")
                text_value = ""
            print(f"[INFO] Saved: {out_path}")
            time.sleep(sleep_t)
            timer("stop")
            add_to_visited(f.name)

        context.close()
        browser.close()
        print("[INFO] Done. All files saved to OUTPUT_DIR.")

if __name__ == "__main__":
    main()

