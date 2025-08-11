from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
import pathlib, time

# --- KONFIG ---
BASE_URL   = "https://phon.nytud.hu/beast2/"
FILES_DIR  = pathlib.Path("/mnt/c/Users/Levinwork/Documents/Nytud/1feladat/celanyag/audio")
OUT_TXT    = pathlib.Path("kimenet.txt")

HEADLESS   = False          # debughoz False; ha stabil, mehet True
NAV_TIMEOUT = 60_000
STEP_TIMEOUT = 30_000
OUTPUT_WAIT_SECS = 180

DEBUG_HTML = pathlib.Path("debug_page.html")
DEBUG_PNG  = pathlib.Path("debug.png")

# --- SEGÉDFÜGGVÉNYEK ---------------------------------------------------------

def dump_debug(page, reason=""):
    try:
        DEBUG_HTML.write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(DEBUG_PNG), full_page=True)
        print(f"[DEBUG] DOM+shot mentve -> {DEBUG_HTML}, {DEBUG_PNG}. {reason}")
    except Exception as e:
        print("[DEBUG] dump_debug hiba:", e)

def list_all_buttons(page):
    try:
        btns = page.locator("button")
        n = btns.count()
        print(f"[DEBUG] Összes gomb a lapon: {n}")
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
        print("[DEBUG] list_all_buttons hiba:", e)

def wait_for_file_selected(page, input_sel, seconds=10):
    """Megvárja, hogy input[type=file] tényleg kapjon fájlt (files.length>0)."""
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
        time.sleep(0.4)
    return False

def wait_for_nonempty_textarea(page, locator_str, seconds=OUTPUT_WAIT_SECS):
    loc = page.locator(locator_str)
    deadline = time.time() + seconds
    last = ""
    while time.time() < deadline:
        try:
            loc.wait_for(state="attached", timeout=1000)
            try:
                val = (loc.input_value() or "").strip()
            except Exception:
                val = (loc.evaluate("el => el.value || ''") or "").strip()
            if val:
                print("[DEBUG] Kimenet textarea nem üres, hossza:", len(val))
                return val
        except Exception:
            pass
        time.sleep(1)
        last = val if 'val' in locals() else last
    print("[DEBUG] Timeout a kimenet textarea-ra várva.")
    return last

# --- FŐPROGRAM ----------------------------------------------------------------

def main():
    files = [p for p in FILES_DIR.glob("*") if p.is_file()]
    if not files:
        print(f"[HIBA] Nincs fájl a '{FILES_DIR}' mappában.")
        return

    with sync_playwright() as p:
        browser  = p.chromium.launch(headless=HEADLESS)
        context  = browser.new_context()
        page     = context.new_page()
        page.set_default_timeout(STEP_TIMEOUT)

        print("[DEBUG] Navigálás:", BASE_URL)
        page.goto(BASE_URL, timeout=NAV_TIMEOUT)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)

        list_all_buttons(page)
        time.sleep(2)

        with OUT_TXT.open("w", encoding="utf-8") as out:
            for f in files:
                print("\n[INFO] Feltöltés:", f.name)

                # 0) Clear (ha van)
                try:
                    page.locator("gradio-app #component-1").click(timeout=1500)
                    print("[DEBUG] 'Clear' megnyomva.")
                except Exception:
                    print("[DEBUG] 'Clear' nem kattintható / nincs.")
                time.sleep(2)

                # --- 1) Feltöltés: előbb forrás: Upload file (ha kell) -----------
                try:
                    page.locator('gradio-app #component-2 .source-selection button[aria-label="Upload file"]').first.click(timeout=1500, force=True)
                    print("[DEBUG] Source: 'Upload file' kiválasztva.")
                except Exception:
                    print("[DEBUG] Source kiválasztás kihagyva (valszeg már ez az aktív).")
                time.sleep(2)

                # --- 2) File chooser-es beadás (első próbálkozás) ----------------
                upload_area = page.locator("gradio-app #component-2 .audio-container button").first
                file_input_sel = "gradio-app #component-2 input[data-testid='file-upload']"

                used_file_chooser = False
                try:
                    with page.expect_file_chooser(timeout=5000) as fc_info:
                        upload_area.click()
                    chooser = fc_info.value
                    chooser.set_files(str(f))
                    used_file_chooser = True
                    print("[DEBUG] Fájl beadva file chooserrel.")
                except Exception as e:
                    print("[DEBUG] File chooser nem jött fel (", e, ") -> B-terv: közvetlen input")
                time.sleep(2)

                # --- 2/B) Direkt input[type=file] feltöltés, ha kellett -----------
                if not used_file_chooser:
                    appeared = False
                    for i in range(8):
                        try:
                            page.locator(file_input_sel).wait_for(state="attached", timeout=1000)
                            appeared = True
                            break
                        except Exception:
                            try:
                                upload_area.click(timeout=1000)
                            except Exception:
                                pass
                    if not appeared:
                        dump_debug(page, reason="input[file] nem jelent meg (B-terv előtt)")
                        raise RuntimeError("Nem érhető el az input[type=file].")

                    page.locator(file_input_sel).set_input_files(str(f))
                    print("[DEBUG] Fájl beadva közvetlen a file inputnak.")
                time.sleep(2)

                # --- 2/C) Ellenőrzés: tényleg bent van a fájl? --------------------
                if not wait_for_file_selected(page, file_input_sel, seconds=10):
                    try:
                        page.locator(file_input_sel).set_input_files(str(f))
                        if not wait_for_file_selected(page, file_input_sel, seconds=8):
                            dump_debug(page, reason="file_input nem kapott fájlt")
                            raise RuntimeError("A fájl beadása nem sikerült (files.length==0).")
                        else:
                            print("[DEBUG] Második kísérletre az input felvette a fájlt.")
                    except Exception:
                        dump_debug(page, reason="file_input set_input_files kivétel")
                        raise
                time.sleep(2)

                # --- 3) Submit ----------------------------------------------------
                submit_btn = page.locator("gradio-app #component-5")
                try:
                    submit_btn.click()
                    print("[DEBUG] 'Submit' megnyomva.")
                except Exception as e:
                    dump_debug(page, reason="Submit katt hiba")
                    raise RuntimeError("Nem találtam a 'Submit' gombot (#component-5).") from e
                time.sleep(2)

                try:
                    has_processing = page.locator("gradio-app .progress-text").count() > 0
                    if not has_processing:
                        print("[DEBUG] Újrapróbálom a 'Submit'-et...")
                        submit_btn.click()
                except Exception:
                    pass
                time.sleep(2)

                # --- 4) Kimenet ---------------------------------------------------
                textarea_sel = "gradio-app #component-10 textarea[data-testid='textbox']"
                text_value = wait_for_nonempty_textarea(page, textarea_sel, seconds=OUTPUT_WAIT_SECS)
                time.sleep(2)

                # --- 5) TextGrid link --------------------------------------------
                tg_link = ""
                try:
                    a = page.locator("gradio-app #component-9 .file-preview a").first
                    a.wait_for(state="attached", timeout=2000)
                    tg_link = a.get_attribute("href") or ""
                    if tg_link:
                        print("[DEBUG] TextGrid:", tg_link)
                except Exception:
                    print("[DEBUG] Nincs TextGrid link (most).")
                time.sleep(2)

                # --- 6) Eredmény ID ----------------------------------------------
                res_id = ""
                try:
                    id_div = page.locator("gradio-app #component-12 .prose div").nth(1)
                    id_div.wait_for(state="attached", timeout=2000)
                    res_id = (id_div.inner_text() or "").strip()
                    if res_id:
                        print("[DEBUG] Eredmény ID:", res_id)
                except Exception:
                    print("[DEBUG] Nincs Eredmény ID (most).")
                time.sleep(2)

                # --- 7) Mentés ----------------------------------------------------
                with OUT_TXT.open("a", encoding="utf-8") as aout:
                    aout.write(f"=== {f.name} ===\n")
                    if res_id:
                        aout.write(f"ID: {res_id}\n")
                    if tg_link:
                        aout.write(f"TextGrid: {tg_link}\n")
                    aout.write("\n-- Kimenet --\n")
                    aout.write(text_value if text_value else "[Nincs kimenet vagy időtúllépés]\n")
                    aout.write("\n\n")
                time.sleep(2)

        context.close()
        browser.close()
        print("[INFO] Kész:", OUT_TXT)

if __name__ == "__main__":
    main()
