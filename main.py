from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
import pathlib, time

# --- KONFIG ---
BASE_URL    = "https://phon.nytud.hu/beast2/"
FILES_DIR   = pathlib.Path("/mnt/c/Users/Levinwork/Documents/Nytud/1feladat/celanyag/audio")
OUTPUT_DIR  = pathlib.Path("/mnt/c/Users/Levinwork/Documents/Nytud/1feladat/celanyag/audio")  # <-- ide mentünk
#OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

HEADLESS    = False          # debughoz False; ha stabil, mehet True megmutaja a weboldalt, és az automatizállt folyamatot
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

def click_submit_with_retries(page):
    """Megbízható 'Submit' kattintás több próbával."""
    btn_css = "gradio-app #component-5"
    btn = page.locator(btn_css)
    btn.wait_for(state="attached", timeout=10_000)
    try:
        btn.scroll_into_view_if_needed(timeout=2_000)
    except Exception:
        pass
    time.sleep(0.3)

    # ha disabled, várjunk kicsit
    for _ in range(10):
        try:
            dis = btn.get_attribute("disabled")
            if not dis:
                break
        except Exception:
            pass
        time.sleep(0.5)

    try:
        btn.click()
        print("[DEBUG] Submit: normál click.")
        return True
    except Exception as e:
        print("[DEBUG] Submit normál click hiba:", e)

    time.sleep(0.5)
    try:
        btn.click(force=True)
        print("[DEBUG] Submit: force click.")
        return True
    except Exception as e:
        print("[DEBUG] Submit force click hiba:", e)

    time.sleep(0.5)
    try:
        page.evaluate("""sel => { const el = document.querySelector(sel); if (el) el.click(); }""", btn_css)
        print("[DEBUG] Submit: JS click.")
        return True
    except Exception as e:
        print("[DEBUG] Submit JS click hiba:", e)

    return False

def tick_checkboxes(page):
    """Bepipálja az 'Extra Features' két checkboxát (#component-6)."""
    # Angol DOM szerint: "Punctuation and Capitalization" és "Diarization"
    cb1 = page.locator("gradio-app #component-6 input[type='checkbox'][name='Punctuation and Capitalization']").first
    cb2 = page.locator("gradio-app #component-6 input[type='checkbox'][name='Diarization']").first

    for name, cb in [("Punctuation and Capitalization", cb1), ("Diarization", cb2)]:
        try:
            cb.wait_for(state="attached", timeout=3000)
            if not cb.is_checked():
                cb.check(force=True)
                print(f"[DEBUG] Checkbox bepipálva: {name}")
            else:
                print(f"[DEBUG] Checkbox már pipálva: {name}")
        except Exception as e:
            print(f"[WARN] Nem sikerült pipálni: {name} -> {e}")

# --- FŐPROGRAM ----------------------------------------------------------------

def main():
    # minden lépés után ennyit vár; gyorsításhoz állítsd 0-ra
    sleep_t = 0

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
        time.sleep(sleep_t)

        list_all_buttons(page)
        time.sleep(sleep_t)

        for f in files:
            print("\n[INFO] Feltöltés:", f.name)

            # 0) Clear (ha van)
            try:
                page.locator("gradio-app #component-4").click(timeout=1500)
                print("[DEBUG] 'Clear' megnyomva.")
            except Exception:
                print("[DEBUG] 'Clear' nem kattintható / nincs.")
            time.sleep(sleep_t)

            # 1) forrás: Upload file (biztos ami biztos)
            try:
                page.locator('gradio-app #component-2 .source-selection button[aria-label="Upload file"]').first.click(timeout=1500, force=True)
                print("[DEBUG] Source: 'Upload file' kiválasztva.")
            except Exception:
                print("[DEBUG] Source kiválasztás kihagyva (valszeg már ez az aktív).")
            time.sleep(sleep_t)

            # 2) File beadása
            upload_area    = page.locator("gradio-app #component-2 .audio-container button").first
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
            time.sleep(sleep_t)



            # 2/D) Pipa az extrákra (feltöltés után, submit előtt!)
            tick_checkboxes(page)
            time.sleep(sleep_t)

            # 3) Submit – megbízható kattintás
            ok = click_submit_with_retries(page)
            time.sleep(sleep_t)
            if not ok:
                dump_debug(page, reason="Submit nem kattintható")
                raise RuntimeError("A 'Submit' gombot nem sikerült megnyomni.")

            # ha nem látszik processing, még egy próbálkozás
            try:
                has_processing = page.locator("gradio-app .progress-text").count() > 0
                if not has_processing:
                    print("[DEBUG] Nem látszik 'processing' -> még egy submit katt.")
                    click_submit_with_retries(page)
                    time.sleep(sleep_t)
            except Exception:
                pass

            # 4) Kimenet
            textarea_sel = "gradio-app #component-10 textarea[data-testid='textbox']"
            text_value = wait_for_nonempty_textarea(page, textarea_sel, seconds=OUTPUT_WAIT_SECS)
            time.sleep(sleep_t)

            # 5) TextGrid link
            tg_link = ""
            try:
                a = page.locator("gradio-app #component-9 .file-preview a").first
                a.wait_for(state="attached", timeout=2000)
                tg_link = a.get_attribute("href") or ""
                if tg_link:
                    print("[DEBUG] TextGrid:", tg_link)
            except Exception:
                print("[DEBUG] Nincs TextGrid link (most).")
            time.sleep(sleep_t)

            # 6) Eredmény ID
            res_id = ""
            try:
                id_div = page.locator("gradio-app #component-12 .prose div").nth(1)
                id_div.wait_for(state="attached", timeout=2000)
                res_id = (id_div.inner_text() or "").strip()
                if res_id:
                    print("[DEBUG] Eredmény ID:", res_id)
            except Exception:
                print("[DEBUG] Nincs Eredmény ID (most).")
            time.sleep(sleep_t)

            # 7) Mentés – fájlonként külön TXT az OUTPUT_DIR-be
            out_file = OUTPUT_DIR / f"{f.stem}.txt"
            with out_file.open("w", encoding="utf-8") as aout:
                if res_id:
                    aout.write(f"ID: {res_id}\n")
                #if tg_link:
                    #aout.write(f"TextGrid: {tg_link}\n")
                #aout.write("\n-- Kimenet --\n")
                aout.write(text_value if text_value else "[Nincs kimenet vagy időtúllépés]\n")
            print(f"[INFO] Mentve: {out_file}")
            time.sleep(sleep_t)

        context.close()
        browser.close()
        print("[INFO] Kész. Minden fájl az OUTPUT_DIR-be mentve.")

if __name__ == "__main__":
    main()
