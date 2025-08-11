from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError
import pathlib, time, sys

# --- KONFIG ---
BASE_URL  = "https://phon.nytud.hu/beast2/"
FILES_DIR = pathlib.Path("/mnt/c/Users/Levinwork/Documents/Nytud/1feladat/celanyag/audio")
OUT_TXT   = pathlib.Path("kimenet.txt")
HEADLESS  = False           # Debughoz legyen False; ha stabil, teheted True-ra
NAV_TIMEOUT = 60_000
STEP_TIMEOUT = 30_000
OUTPUT_WAIT_SECS = 180

DEBUG_HTML = pathlib.Path("debug_page.html")
DEBUG_PNG  = pathlib.Path("debug.png")

def dump_debug(page, reason=""):
    try:
        html = page.content()
        DEBUG_HTML.write_text(html, encoding="utf-8")
        page.screenshot(path=str(DEBUG_PNG), full_page=True)
        print(f"[DEBUG] Mentettem a DOM-ot ({DEBUG_HTML}) és screenshotot ({DEBUG_PNG}). {reason}")
    except Exception as e:
        print("[DEBUG] dump_debug hiba:", e)

def list_all_buttons(page):
    try:
        btns = page.locator("button")
        n = btns.count()
        print(f"[DEBUG] Összes gomb a lapon: {n}")
        for i in range(min(n, 80)):  # ne spammeljünk túl
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

def wait_for_nonempty_textarea(page, locator_str, seconds=OUTPUT_WAIT_SECS):
    loc = page.locator(locator_str)
    deadline = time.time() + seconds
    last = ""
    while time.time() < deadline:
        try:
            loc.wait_for(state="attached", timeout=2000)
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

def main():
    files = [p for p in FILES_DIR.glob("*") if p.is_file()]
    if not files:
        print(f"[HIBA] Nincs fájl a '{FILES_DIR}' mappában.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(STEP_TIMEOUT)

        print("[DEBUG] Navigálás:", BASE_URL)
        page.goto(BASE_URL, timeout=NAV_TIMEOUT)
        page.wait_for_load_state("domcontentloaded")

        # Alap debug: listázzuk a gombokat
        list_all_buttons(page)

        with OUT_TXT.open("w", encoding="utf-8") as out:
            for f in files:
                print("\n[INFO] Feltöltés:", f.name)

                # 0) Törlés (ha látható)
                try:
                    clear_btn = page.locator("gradio-app #component-4")
                    clear_btn.click(timeout=1500)
                    print("[DEBUG] 'Törlés' gomb megnyomva.")
                except Exception:
                    print("[DEBUG] 'Törlés' gomb nem kattintható / nincs.")

                # 1) Feltöltés mód kiválasztása: az általad küldött HTML szerint
                # ez a #component-2 blokkban, a .source-selection span alatt van
                upload_btn = page.locator('gradio-app #component-2 .source-selection button[aria-label="Upload file"]')

                # ha ez nem található, próbáljuk általánosan
                if upload_btn.count() == 0:
                    print("[DEBUG] Célzott Upload szelektor nem talált. Próbálok általánosabbat…")
                    upload_btn = page.locator('gradio-app button[aria-label="Upload file"]')

                # próbáljunk többször kattintani
                clicked = False
                for i in range(8):
                    try:
                        upload_btn.first.click(timeout=3000, force=True)
                        print("[DEBUG] 'Upload file' ikon megnyomva. (próbálkozás:", i+1, ")")
                        clicked = True
                        break
                    except Exception as e:
                        print(f"[DEBUG] Upload click sikertelen (#{i+1}): {e}")
                        time.sleep(1)

                if not clicked:
                    dump_debug(page, reason="Upload ikon nem kattintható")
                    list_all_buttons(page)
                    raise RuntimeError("Nem találtam/kattintható az 'Upload file' ikon (aria-label='Upload file').")

                # 1.5) input[type=file] megvárása
                file_input = page.locator("gradio-app input[type='file']")
                appeared = False
                for i in range(12):  # ~12s
                    try:
                        file_input.wait_for(state="attached", timeout=1000)
                        appeared = True
                        print("[DEBUG] input[type=file] megjelent.")
                        break
                    except Exception:
                        # Néha kell még egy kattintás a forrás választóra, ezért rámegyünk még egyszer
                        try:
                            upload_btn.first.click(timeout=1000, force=True)
                        except Exception:
                            pass
                        time.sleep(1)
                if not appeared:
                    dump_debug(page, reason="input[type=file] nem jelent meg")
                    raise RuntimeError("Nem jelent meg az <input type='file'> 12s-en belül.")

                # 2) Fájl beadása
                print("[DEBUG] Fájl beadása:", f)
                file_input.set_input_files(str(f))

                # 3) „Beküldés” (#component-5)
                submit_btn = page.locator("gradio-app #component-5")
                try:
                    submit_btn.click()
                    print("[DEBUG] 'Beküldés' gomb kattintva.")
                except Exception as e:
                    dump_debug(page, reason="Beküldés katt hiba")
                    raise RuntimeError("Nem találtam a 'Beküldés' gombot (#component-5).") from e

                # 4) Kimenet textarea (disabled)
                textarea_sel = "gradio-app #component-10 textarea[data-testid='textbox']"
                text_value = wait_for_nonempty_textarea(page, textarea_sel, seconds=OUTPUT_WAIT_SECS)

                # 5) TextGrid link (ha van)
                tg_link = ""
                try:
                    a = page.locator("gradio-app #component-9 .file-preview a").first
                    a.wait_for(state="attached", timeout=2000)
                    tg_link = a.get_attribute("href") or ""
                    if tg_link:
                        print("[DEBUG] TextGrid:", tg_link)
                except Exception:
                    print("[DEBUG] Nincs TextGrid link (most).")

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

                # 7) Mentés TXT-be
                out.write(f"=== {f.name} ===\n")
                if res_id:
                    out.write(f"ID: {res_id}\n")
                if tg_link:
                    out.write(f"TextGrid: {tg_link}\n")
                out.write("\n-- Kimenet --\n")
                out.write(text_value if text_value else "[Nincs kimenet vagy időtúllépés]\n")
                out.write("\n\n")
                out.flush()

        context.close()
        browser.close()
        print("[INFO] Kész:", OUT_TXT)

if __name__ == "__main__":
    main()
