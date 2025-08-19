import time
import json
import pathlib
import mimetypes
import traceback
from datetime import datetime
from typing import Optional, Dict, Any, List

import requests

# ---- Config (ASCII-safe) ----
BASE_URL   = "https://phon.nytud.hu/beast2/"
#FILES_DIR  = pathlib.Path("/mnt/c/Users/Levinwork/Documents/Nytud/1feladat/celanyag/audio")
#OUTPUT_DIR = pathlib.Path("/mnt/c/Users/Levinwork/Documents/Nytud/1feladat/celanyag/javtest")
FILES_DIR  = pathlib.Path("/home/datasets/raw-data/podcasts")
OUTPUT_DIR = pathlib.Path("/home/szabol/leiratok")

ALLOWED_EXTS      = {".mp3", ".m4a", ".wav", ".flac", ".ogg"}
EXTRA_OPTIONS     = ["Punctuation and Capitalization", "Diarization"]  # will try; falls back to []
HTTP_TIMEOUT_CONN = 3000      # connect timeout (seconds)
HTTP_TIMEOUT_READ = 6000     # read timeout for predict (seconds)
HTTP_TIMEOUT_UPLD = 3000     # read timeout for upload (seconds)
MAX_RETRIES       = 3       # retries for predict
BACKOFF_SEC       = 5       # sleep between retries
VISITED_FILE      = pathlib.Path("./visited.txt")


# -------------------------------
# Logging helpers
# -------------------------------
def tstamp() -> str:
    return datetime.now().strftime("%H:%M:%S")

def log(msg: str) -> None:
    print(f"[{tstamp()}] {msg}")

"""
Fancy file name show
"""
def step(title: str) -> None:
    print("\n" + "=" * 70)
    print(f"== {title}")
    print("=" * 70)


# -------------------------------
# URL normalise, removes to many slashes
# -------------------------------
def ensure_base(url: str) -> str:
    return url.rstrip("/") + "/"

def say_time():
    log("Time now: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

def add_to_visited(name: str) -> None:
    VISITED_FILE.touch(exist_ok=True)
    cur = set(x.strip() for x in VISITED_FILE.read_text(encoding="utf-8").splitlines() if x.strip())
    if name not in cur:
        with VISITED_FILE.open("a", encoding="utf-8") as f:
            f.write(name + "\n")

def is_visited(name: str) -> bool:
    VISITED_FILE.touch(exist_ok=True)
    return name in {x.strip() for x in VISITED_FILE.read_text(encoding="utf-8").splitlines() if x.strip()}

def find_audio_files(root: pathlib.Path) -> List[pathlib.Path]:
    # Collect all files (recursively) under the root directory
    all_files = []
    for p in root.rglob("*"):
        if p.is_file():
            all_files.append(p)

    # Filter only allowed extensions (.mp3, .m4a, etc.)
    files = []
    for p in all_files:
        ext = p.suffix.lower()
        if ext in ALLOWED_EXTS and not is_visited(p.name):
            files.append(p)

    # Track files that are skipped (not in allowed extensions)
    skipped = []
    for p in all_files:
        ext = p.suffix.lower()
        if ext not in ALLOWED_EXTS:
            skipped.append(p)

    # If any files were skipped, log them
    if skipped:
        skipped_names = ", ".join(s.name for s in skipped)
        log("[INFO] Skipped (not audio): " + skipped_names)

    # Return the list of valid audio files
    return files

def pick_best_text(texts: List[str]) -> str:
    return max(texts, key=len) if texts else ""

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


# -------------------------------
# Gradio discovery
# -------------------------------
def discover_fn_index(base: str) -> Optional[int]:
    """Read /config to locate fn_index (dependency id) for api_name 'partial_2'."""
    url = ensure_base(base) + "config"
    log(f"[STEP] Discover fn_index via GET {url}")
    try:
        r = requests.get(url, timeout=(HTTP_TIMEOUT_CONN, 60))
        if r.ok and r.headers.get("content-type","").startswith("application/json"):
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


# -------------------------------
# Gradio API client (fn_index only)
# -------------------------------
class GradioClient:
    """Upload via /upload, predict only via /api/predict + fn_index, with retries."""

    def __init__(self, base_url: str):
        self.base = ensure_base(base_url)
        self.sess = requests.Session()
        self.fn_index = discover_fn_index(self.base)  # required for /api/predict

    def upload(self, path: pathlib.Path) -> Dict[str, Any]:
        """Upload file to /upload return FileData: {'path': '...'}"""
        url = self.base + "upload"
        size_mb = path.stat().st_size / (1024 * 1024)
        log(f"[STEP] UPLOAD  {url}")
        log(f"      file: {path.name} ({size_mb:.2f} MB)")
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        with path.open("rb") as f:
            r = self.sess.post(url, files={"files": (path.name, f, mime)}, timeout=(HTTP_TIMEOUT_CONN, HTTP_TIMEOUT_UPLD))
        r.raise_for_status()
        data = r.json()
        # Accept either list[str] or dict{"name": "file=..."}
        if isinstance(data, list) and data and isinstance(data[0], str):
            log(f"[OK] Upload response (list path): {data[0]}")
            return {"path": data[0]}
        if isinstance(data, dict) and isinstance(data.get("name"), str):
            p = data["name"].split("file=", 1)[-1]
            log(f"[OK] Upload response (dict name): {p}")
            return {"path": p}
        raise RuntimeError("Unexpected /upload response")

    def _post_json(self, payload: Dict[str, Any], label: str) -> Optional[Dict[str, Any]]:
        """Single POST to /api/predict with JSON payload; returns JSON dict or None."""
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

    def predict(self, filedata: Dict[str, Any], options: List[str]) -> Dict[str, Any]:
        """
        Predict using only /api/predict with fn_index:
          1) try with options like dia and capital
        Both with retry/backoff.
        """
        if self.fn_index is None:
            raise RuntimeError("fn_index not discovered; cannot call /api/predict")

        # (1) with options
        payload_opts = {"fn_index": self.fn_index, "data": [filedata, options]}
        for attempt in range(1, MAX_RETRIES + 1):
            log(f"[DEBUG] /api/predict (with options) attempt {attempt}/{MAX_RETRIES}")
            resp = self._post_json(payload_opts, "with options (fn_index)")
            if resp is not None:
                return resp
            if attempt < MAX_RETRIES:
                log(f"[BACKOFF] sleeping {BACKOFF_SEC}s before next attempt")
                time.sleep(BACKOFF_SEC)

        raise RuntimeError("All /api/predict attempts failed")


    """
    {
      "data": [
        "Hello world",
        {"text": "Subtitle A"},
        {"label": "Speaker 1"}
      ],
      "error": null,
      "extra": "Some note"
    }
    expected structure
    """
    @staticmethod
    def extract_texts(resp: Dict[str, Any]) -> List[str]:
        """Collect strings from common Gradio response shapes."""
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
        # Deduplicate preserving order
        seen, res = set(), []
        for s in out:
            if s not in seen:
                res.append(s); seen.add(s)
        return res


# -------------------------------
# Save helpers
# -------------------------------
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

    files = find_audio_files(FILES_DIR)
    if not files:
        log(f"[ERROR] No eligible audio file in '{FILES_DIR}'.")
        return

    api = GradioClient(base)

    for f in files:
        step(f"PROCESS FILE: {f.name}")
        timer("start")
        say_time()

        # 1) Upload
        try:
            filedata = api.upload(f)  # {'path': '...'}
        except Exception as e:
            log(f"[ERROR] Upload failed: {e}")
            log(traceback.format_exc(limit=2).strip())
            continue

        # 2) Predict (fn_index only; with options)
        try:
            resp = api.predict(filedata, EXTRA_OPTIONS)
        except Exception as e:
            log(f"[ERROR] Predict failed: {e}")
            log(traceback.format_exc(limit=2).strip())
            continue

        # 3) Extract best text
        texts = api.extract_texts(resp)
        best = pick_best_text(texts)
        log(f"[INFO] Extracted {len(texts)} text segment(s), picked best length={len(best)}")

        # Optional result id (if the backend adds one)
        res_id = None
        if isinstance(resp, dict):
            for k in ("id", "result_id", "run_id"):
                v = resp.get(k)
                if isinstance(v, str) and v.strip():
                    res_id = v.strip()
                    break

        # 4) Save transcript
        save_transcript(OUTPUT_DIR, f, best, res_id=res_id)

        log("[DONE] File processed.\n")
        timer("stop")
        add_to_visited(f.name)

    log("[ALL DONE] All files saved to OUTPUT_DIR.")


if __name__ == "__main__":
    main()
