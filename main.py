import time
import json
import pathlib
import mimetypes
import traceback
import subprocess
import shlex
import shutil
from datetime import datetime
from typing import Optional, Dict, Any, List
import os
import re

import requests

"""
Config part
"""
BASE_URL   = "https://phon.nytud.hu/beast2/"
#FILES_DIR  = pathlib.Path("/mnt/c/Users/Levinwork/Documents/Nytud/1feladat/celanyag/audio")
#OUTPUT_DIR = pathlib.Path("/mnt/c/Users/Levinwork/Documents/Nytud/1feladat/celanyag/javtest")
FILES_DIR  = pathlib.Path("/home/datasets/raw-data/podcasts") #this is the folder where it gets the files that will be uploaded
OUTPUT_DIR = pathlib.Path("/home/szabol/leiratok") #this is where it puts the txt

ALLOWED_EXTS      = {".mp3", ".m4a"} # allowed file formats for uploading
EXTRA_OPTIONS     = ["Punctuation and Capitalization", "Diarization"] #extra options that will be checked
HTTP_TIMEOUT_CONN = 3000      # connect timeout (seconds)
HTTP_TIMEOUT_READ = 6000      # read timeout for predict (seconds)
HTTP_TIMEOUT_UPLD = 3000      # read timeout for upload (seconds)
MAX_RETRIES       = 3         # retries for predict
BACKOFF_SEC       = 5         # sleep between retries
VISITED_FILE      = pathlib.Path("./visited.txt") #this is where it puts the names of the files that it worked on

# --- Chunking config ---
SIZE_SPLIT_MB              = 25     # if file size >= 25 MB, split into chunks
CHUNK_SEC                  = 600    # split into 10-minute chunks
REUPLOAD_EACH_TRY_FOR_BIG  = True   # for big files, re-upload before each retry
CHUNK_BASE_DIR             = pathlib.Path(__file__).parent / "chunks_tmp" #folder for the chunks

"""
LOGING SECTION AND DEBUGGING SECTION
"""


"""
Says the time at the beginning of te row
"""

def tstamp() -> str:
    return datetime.now().strftime("%H:%M:%S")
def log(msg: str) -> None:
    print(f"[{tstamp()}] {msg}")

# Pretty step logging
def step(title: str) -> None:
    print("\n" + "=" * 70)
    print(f"== {title}")
    print("=" * 70)
def ensure_base(url: str) -> str:
    return url.rstrip("/") + "/"

def say_time():
    log("Time now: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
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


"""
Adds the name of the file to the visited files
"""

def add_to_visited(name: str) -> None:
    VISITED_FILE.touch(exist_ok=True)
    cur = set(x.strip() for x in VISITED_FILE.read_text(encoding="utf-8").splitlines() if x.strip())
    if name not in cur:
        with VISITED_FILE.open("a", encoding="utf-8") as f:
            f.write(name + "\n")

"""
Checks if the file name is in the visited section, so that it only works on every file once
"""

def is_visited(name: str) -> bool:
    VISITED_FILE.touch(exist_ok=True)
    return name in {x.strip() for x in VISITED_FILE.read_text(encoding="utf-8").splitlines() if x.strip()}

"""
Finds all files with the allowed extensions
"""

def find_audio_files(root: pathlib.Path) -> List[pathlib.Path]:
    all_files = [p for p in root.rglob("*") if p.is_file()]
    files = [p for p in all_files if p.suffix.lower() in ALLOWED_EXTS and not is_visited(p.name)]
    skipped = [p for p in all_files if p.suffix.lower() not in ALLOWED_EXTS]
    if skipped:
        log("[INFO] Skipped (not audio): " + ", ".join(s.name for s in skipped))
    return files


def pick_best_text(texts: List[str]) -> str:
    return max(texts, key=len) if texts else ""

"""
Checks if the size of the file,(if its to big, than the upload to the API might fail)
"""

def is_big_file(path: pathlib.Path) -> bool:
    return (path.stat().st_size / (1024 * 1024)) >= SIZE_SPLIT_MB

"""
Makes the filenames safe, so that it can run on a non UTF8 linux szerver
"""
def _safe_stem(stem: str) -> str:
    # keep letters, digits, dot, underscore, dash; replace others with underscore
    return re.sub(r'[^A-Za-z0-9._-]+', '_', stem)


"""
Split audio into chunks under chunks_tmp/, using sanitized names.
Detect ffmpeg from PATH, $FFMPEG_BIN, or imageio-ffmpeg. Falls back to re-encode if stream-copy fails.
"""
def ffmpeg_split(input_path: pathlib.Path, chunk_sec: int = CHUNK_SEC) -> List[pathlib.Path]:

    # sanitize output folder/name
    safe = _safe_stem(input_path.stem)
    out_dir = CHUNK_BASE_DIR / safe

    # start clean if leftovers exist
    if out_dir.exists() and any(out_dir.iterdir()):
        shutil.rmtree(out_dir)
        log(f"[CLEANUP] Removed leftover chunk directory before splitting: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)  # ffmpeg won't create it

    # resolve ffmpeg executable: PATH -> $FFMPEG_BIN -> imageio-ffmpeg
    ff = shutil.which("ffmpeg")
    if not ff:
        env_ff = os.environ.get("FFMPEG_BIN")
        if env_ff:
            ff = shutil.which(env_ff) or env_ff
    if not ff:
        try:
            import imageio_ffmpeg
            ff = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ff = None
    if not ff:
        raise RuntimeError(
            "ffmpeg not found. Install it, export FFMPEG_BIN, or install imageio-ffmpeg in your venv."
        )

    # --- Attempt 1: fast stream-copy split (no re-encode) ---
    out_pattern1 = out_dir / (safe + "__part%03d" + input_path.suffix)
    cmd1 = (
        f"{shlex.quote(ff)} -hide_banner -nostdin -y -i {shlex.quote(str(input_path))} "
        f"-c copy -f segment -segment_time {int(chunk_sec)} {shlex.quote(str(out_pattern1))}"
    )
    log(f"[STEP] Local split with ffmpeg (stream copy): {cmd1}")
    res1 = subprocess.run(cmd1, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    if res1.returncode != 0:
        log("[WARN] ffmpeg stream-copy split failed; stderr (head):")
        log(res1.stderr.decode(errors="ignore")[:1000])

        # clean partial outputs (if any)
        for p in out_dir.glob("*"):
            try:
                p.unlink()
            except Exception:
                pass

        # --- Attempt 2: robust fallback with re-encode (mp3) ---
        out_pattern2 = out_dir / (safe + "__part%03d.mp3")
        cmd2 = (
            f"{shlex.quote(ff)} -hide_banner -nostdin -y -i {shlex.quote(str(input_path))} "
            f"-vn -sn -map 0:a:0? -c:a libmp3lame -q:a 4 "
            f"-f segment -segment_time {int(chunk_sec)} -reset_timestamps 1 "
            f"{shlex.quote(str(out_pattern2))}"
        )
        log(f"[STEP] Local split with ffmpeg (re-encode fallback): {cmd2}")
        res2 = subprocess.run(cmd2, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res2.returncode != 0:
            log("[ERROR] ffmpeg split failed even after fallback; stderr (head):")
            log(res2.stderr.decode(errors="ignore")[:1000])
            raise RuntimeError("ffmpeg split failed")

    # collect parts regardless of extension used
    parts = sorted(out_dir.glob(safe + "__part*"))
    log(f"[OK] Created {len(parts)} chunk(s)")
    return parts

"""Remove chunk directory after processing (uses sanitized name)."""
def cleanup_chunks(input_path: pathlib.Path):

    safe = _safe_stem(input_path.stem)
    out_dir = CHUNK_BASE_DIR / safe
    if out_dir.exists():
        shutil.rmtree(out_dir)
        log(f"[CLEANUP] Removed chunk directory: {out_dir}")

"""
Gradio discovery
"""
def discover_fn_index(base: str) -> Optional[int]:
    """Read /config to locate fn_index (dependency id) for api_name 'partial_2'."""
    url = ensure_base(base) + "config"
    log(f"[STEP] Discover fn_index via GET {url}")
    try:
        r = requests.get(url, timeout=(HTTP_TIMEOUT_CONN, 60))
        if r.ok and r.headers.get("content-type", "").startswith("application/json"):
            cfg = r.json()
            deps = cfg.get("dependencies", [])
            for dep in deps:
                if isinstance(dep, dict) and dep.get("api_name") == "partial_2":
                    fn_index = dep.get("id")
                    log(f"[OK] fn_index discovered: {fn_index}")
                    return fn_index
        log("[WARN] Could not discover fn_index from /config.")
    except Exception as e:
        log(f"[WARN] Discovery failed: {e}")
    return None

"""
Gradio API client (fn_index only)
"""
class GradioClient:
    """Upload via /upload, predict only via /api/predict + fn_index, with retries."""

    def __init__(self, base_url: str):
        self.base = ensure_base(base_url)
        self.sess = requests.Session()
        self.fn_index = discover_fn_index(self.base)  # required for /api/predict

    def upload(self, path: pathlib.Path) -> Dict[str, Any]:
        """Upload file to /upload -> return FileData: {'path': '...'}"""
        url = self.base + "upload"
        size_mb = path.stat().st_size / (1024 * 1024)
        log(f"[STEP] UPLOAD -> {url}")
        log(f"      file: {path.name} ({size_mb:.2f} MB)")
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        with path.open("rb") as f:
            r = self.sess.post(url, files={"files": (path.name, f, mime)}, timeout=(HTTP_TIMEOUT_CONN, HTTP_TIMEOUT_UPLD))
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data and isinstance(data[0], str):
            log(f"[OK] Upload response (list path): {data[0]}")
            return {"path": data[0]}
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            p = data["name"].split("file=", 1)[-1]
            log(f"[OK] Upload response (dict name): {p}")
            return {"path": p}
        raise RuntimeError("Unexpected /upload response")

    def _post_json(self, payload: Dict[str, Any], label: str) -> Optional[Dict[str, Any]]:
        url = self.base + "api/predict"
        log(f"[TRY] POST {url}  ({label})  timeout={HTTP_TIMEOUT_READ}s")

        try:
            r = self.sess.post(url, json=payload, timeout=(HTTP_TIMEOUT_CONN, HTTP_TIMEOUT_READ))
            if not r.ok:
                log(f"[FAIL] HTTP {r.status_code} on /api/predict ({label})")
                return None
            try:
                resp = r.json()
            except Exception:
                log(f"[FAIL] Non-JSON response on /api/predict ({label})")
                return None
            if isinstance(resp, dict) and resp.get("error"):
                log(f"[FAIL] Backend error on /api/predict ({label}): {resp.get('error')}")
                return None
            log(f"[OK] /api/predict ({label})")
            return resp
        except requests.exceptions.ReadTimeout:
            log(f"[TIMEOUT] Read timeout on /api/predict ({label}) after {HTTP_TIMEOUT_READ}s")
            return None
        except Exception as e:
            log(f"[EXC] /api/predict ({label}) -> {e}")
            log(traceback.format_exc(limit=2).strip())
            return None

    def predict_once(self, filedata: Dict[str, Any], options: List[str]) -> Optional[Dict[str, Any]]:
        if self.fn_index is None:
            raise RuntimeError("fn_index not discovered; cannot call /api/predict")
        payload_opts = {"fn_index": self.fn_index, "data": [filedata, options]}
        return self._post_json(payload_opts, "with options (fn_index)")

    @staticmethod
    def extract_texts(resp: Dict[str, Any]) -> List[str]:
        out: List[str] = []
        if not isinstance(resp, dict):
            return out
        for key in ("data", "result", "output"):
            v = resp.get(key)
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, str) and item.strip():
                        out.append(item.strip())
                    elif isinstance(item, dict):
                        for tk in ("text", "label", "value"):
                            s = item.get(tk)
                            if isinstance(s, str) and s.strip():
                                out.append(s.strip())
        for k, v in resp.items():
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        seen, res = set(), []
        for s in out:
            if s not in seen:
                res.append(s); seen.add(s)
        return res

"""
Helperes for communication
"""
def process_file_with_retry(api: GradioClient, f: pathlib.Path) -> str:
    filedata = api.upload(f)
    best_resp: Optional[Dict[str, Any]] = None

    for attempt in range(1, MAX_RETRIES + 1):
        log(f"[DEBUG] /api/predict (with options) attempt {attempt}/{MAX_RETRIES}")

        resp = api.predict_once(filedata, EXTRA_OPTIONS)

        if resp is not None:
            best_resp = resp
            break
        if attempt < MAX_RETRIES:
            log(f"[BACKOFF] sleeping {BACKOFF_SEC}s before next attempt...")
            time.sleep(BACKOFF_SEC)
            if REUPLOAD_EACH_TRY_FOR_BIG and is_big_file(f):
                try:
                    filedata = api.upload(f)
                except Exception as e:
                    log(f"[WARN] Re-upload failed before next attempt: {e}")

    if best_resp is None:
        raise RuntimeError("All /api/predict attempts failed")

    texts = GradioClient.extract_texts(best_resp)
    return pick_best_text(texts)

"""
Nagy bigger files are getting split into chunks
"""

def process_maybe_chunked(api: GradioClient, f: pathlib.Path) -> str:
    """
    All-or-nothing: ha nagy fájl és bármelyik chunk hibázik vagy üres,
    az egész fájl feldolgozását megszakítjuk és kivételt dobunk.
    Kis fájlnál a régi retry-logika él a process_file_with_retry-ben.
    """
    if not is_big_file(f):
        # Kis fájl: ha ez hibázik, felmegy a kivétel a main()-ig (és az ott kezeli).
        return process_file_with_retry(api, f)

    log("[INFO] Big file detected -> splitting locally...")
    parts = ffmpeg_split(f, CHUNK_SEC)
    if not parts:
        cleanup_chunks(f)
        raise RuntimeError("No chunks created by ffmpeg_split")

    merged: List[str] = []
    total = len(parts)

    for i, part in enumerate(parts, 1):
        step(f"PROCESS CHUNK {i}/{total}: {part.name}")
        say_time()
        try:
            text = process_file_with_retry(api, part)
        except Exception as e:
            cleanup_chunks(f)
            raise RuntimeError(f"Chunk {i}/{total} failed: {e}") from e

        # Üres/whitespace kimenet is hibának számít
        if not text or not text.strip():
            cleanup_chunks(f)
            raise RuntimeError(f"Chunk {i}/{total} returned empty output")

        merged.append(text.strip())

    # Minden chunk sikerült; normál összeillesztés és takarítás
    final_text = "\n\n".join(merged).strip() + ("\n" if merged else "")
    cleanup_chunks(f)
    return final_text


"""
If the server is down all not processsed files are saved just in chase
"""

TIMEOUTS_FILE = pathlib.Path("./timeouts.txt")

def add_to_timeouts(name: str) -> None:
    """
    Ha egy fájl feldolgozása hibával megszakad,
    a nevét kiírjuk a timeouts.txt fájlba.
    Nem duplikál, csak egyszer szerepel minden fájl.
    """
    TIMEOUTS_FILE.touch(exist_ok=True)
    cur = set(
        x.strip() for x in TIMEOUTS_FILE.read_text(encoding="utf-8").splitlines() if x.strip()
    )
    if name not in cur:
        with TIMEOUTS_FILE.open("a", encoding="utf-8") as f:
            f.write(name + "\n")


"""
Save helpers
"""

def save_transcript(base_out: pathlib.Path, src_file: pathlib.Path, text: str, res_id: Optional[str] = None) -> pathlib.Path:
    base_out.mkdir(parents=True, exist_ok=True)
    rel = src_file.relative_to(FILES_DIR)
    out_path = (base_out / rel).with_suffix(".txt")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        if res_id:
            f.write(f"ID: {res_id}\n")
        f.write(text if text else "[No output]\n")
    log(f"[OK] Saved transcript: {out_path}")
    return out_path

# -------------------------------
# Main
# -------------------------------
def main():
    base = ensure_base(BASE_URL)
    log(f"[BOOT] Using BASE_URL: {base}")

    files = find_audio_files(FILES_DIR) #finds audio files that have the right extension
    if not files:
        log(f"[ERROR] No eligible audio file in '{FILES_DIR}'.")
        return

    api = GradioClient(base) #api connections

    for f in files:     #works on every file found
        step(f"PROCESS FILE: {f.name}")
        timer("start")
        print("processing your file...")

        try:
            best_text = process_maybe_chunked(api, f) #probáld meg chunckokra bontani
        except Exception as e:
            log(f"[ABORT FILE] {f.name} aborted due to chunk failure: {e}")
            log(traceback.format_exc(limit=2).strip())
            add_to_timeouts(f.name)   # <-- ide került be
            continue

        log(f"[INFO] Picked best length={len(best_text)}")
        save_transcript(OUTPUT_DIR, f, best_text, res_id=None)

        log("[DONE] File processed.\n")
        timer("stop")
        add_to_visited(f.name)      #last step after saving the file it gets added to the visited files

    log("[ALL DONE] All files saved to OUTPUT_DIR.")

if __name__ == "__main__":
    main()
"""
If you run it like this you can finetune the arguments for default usage run:[python3 main.py]
python3 main.py --size-split-mb 80 --chunk-sec 900
finetune
"""
