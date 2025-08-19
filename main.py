import time
import json
import pathlib
import mimetypes
import traceback
import subprocess
import shlex
import shutil
import argparse
from datetime import datetime
from typing import Optional, Dict, Any, List

import requests

# ---- Config (ASCII-safe) ----
BASE_URL   = "https://phon.nytud.hu/beast2/"
FILES_DIR  = pathlib.Path("/mnt/c/Users/Levinwork/Documents/Nytud/1feladat/celanyag/audio")
OUTPUT_DIR = pathlib.Path("/mnt/c/Users/Levinwork/Documents/Nytud/1feladat/celanyag/javtest")

ALLOWED_EXTS      = {".mp3", ".m4a", ".wav", ".flac", ".ogg"}
EXTRA_OPTIONS     = ["Punctuation and Capitalization", "Diarization"]  # will try; falls back to []
HTTP_TIMEOUT_CONN = 3000      # connect timeout (seconds)
HTTP_TIMEOUT_READ = 6000      # read timeout for predict (seconds)
HTTP_TIMEOUT_UPLD = 3000      # read timeout for upload (seconds)
MAX_RETRIES       = 3         # retries for predict
BACKOFF_SEC       = 5         # sleep between retries
VISITED_FILE      = pathlib.Path("./visited.txt")

# --- Chunking config (defaults; can be overridden by CLI) ---
SIZE_SPLIT_MB              = 50     # if file size >= this, split into chunks
CHUNK_SEC                  = 600    # 10-minute chunks
REUPLOAD_EACH_TRY_FOR_BIG  = True   # for big files, re-upload before each retry
CHUNK_BASE_DIR             = pathlib.Path(__file__).parent / "chunks_tmp"

# will be set by CLI
KEEP_CHUNKS                = False


# -------------------------------
# Logging helpers
# -------------------------------
def tstamp() -> str:
    return datetime.now().strftime("%H:%M:%S")

def log(msg: str) -> None:
    print(f"[{tstamp()}] {msg}")

# Pretty step logging
def step(title: str) -> None:
    print("\n" + "=" * 70)
    print(f"== {title}")
    print("=" * 70)


# -------------------------------
# Small utils
# -------------------------------
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
    all_files = [p for p in root.rglob("*") if p.is_file()]
    files = [p for p in all_files if p.suffix.lower() in ALLOWED_EXTS and not is_visited(p.name)]
    skipped = [p for p in all_files if p.suffix.lower() not in ALLOWED_EXTS]
    if skipped:
        log("[INFO] Skipped (not audio): " + ", ".join(s.name for s in skipped))
    return files

def pick_best_text(texts: List[str]) -> str:
    return max(texts, key=len) if texts else ""


def is_big_file(path: pathlib.Path) -> bool:
    return (path.stat().st_size / (1024 * 1024)) >= SIZE_SPLIT_MB


def ffmpeg_split(input_path: pathlib.Path, chunk_sec: int = CHUNK_SEC) -> List[pathlib.Path]:
    """Split audio into chunks (lossless, -c copy). Output files live under chunks_tmp/<stem>/."""
    out_dir = CHUNK_BASE_DIR / input_path.stem

    # cleanup if exists and not empty (fresh start per your request)
    if out_dir.exists() and any(out_dir.iterdir()):
        shutil.rmtree(out_dir)
        log(f"[CLEANUP] Removed leftover chunk directory before splitting: {out_dir}")

    out_dir.mkdir(parents=True, exist_ok=True)
    out_pattern = out_dir / (input_path.stem + "__part%03d" + input_path.suffix)
    cmd = (
        f"ffmpeg -hide_banner -nostdin -y -i {shlex.quote(str(input_path))} "
        f"-c copy -f segment -segment_time {int(chunk_sec)} {shlex.quote(str(out_pattern))}"
    )
    log(f"[STEP] Local split with ffmpeg: {cmd}")
    res = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0:
        log("[ERROR] ffmpeg split failed")
        log(res.stderr.decode(errors="ignore")[:2000])
        raise RuntimeError("ffmpeg split failed")
    parts = sorted(out_dir.glob(input_path.stem + "__part*" + input_path.suffix))
    log(f"[OK] Created {len(parts)} chunk(s)")
    return parts


def cleanup_chunks(input_path: pathlib.Path):
    """Remove chunk directory after processing, unless --keep-chunks is set."""
    if KEEP_CHUNKS:
        log("[INFO] Keeping chunks (debug mode).")
        return
    out_dir = CHUNK_BASE_DIR / input_path.stem
    if out_dir.exists():
        shutil.rmtree(out_dir)
        log(f"[CLEANUP] Removed chunk directory: {out_dir}")


# -------------------------------
# Gradio discovery
# -------------------------------
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
        """Upload file to /upload → return FileData: {'path': '...'}"""
        url = self.base + "upload"
        size_mb = path.stat().st_size / (1024 * 1024)
        log(f"[STEP] UPLOAD → {url}")
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
            log(f"[EXC] /api/predict ({label}) → {e}")
            log(traceback.format_exc(limit=2).strip())
            return None

    def predict_once(self, filedata: Dict[str, Any], options: List[str]) -> Optional[Dict[str, Any]]:
        if self.fn_index is None:
            raise RuntimeError("fn_index not discovered; cannot call /api/predict")
        payload_opts = {"fn_index": self.fn_index, "data": [filedata, options]}
        return self._post_json(payload_opts, "with options (fn_index)")

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
# Higher-level helpers (retry + chunking)
# -------------------------------
def process_file_with_retry(api: GradioClient, f: pathlib.Path) -> str:
    """
    Upload + predict with retries.
    For big files, optionally re-upload before each retry.
    Returns the best text found.
    """
    filedata = api.upload(f)
    best_resp: Optional[Dict[str, Any]] = None

    for attempt in range(1, MAX_RETRIES + 1):
        log(f"[DEBUG] /api/predict (with options) attempt {attempt}/{MAX_RETRIES}")
        timer("start")
        print("processing your file...")
        resp = api.predict_once(filedata, EXTRA_OPTIONS)
        if resp is not None:
            best_resp = resp
            break
        if attempt < MAX_RETRIES:
            log(f"[BACKOFF] sleeping {BACKOFF_SEC}s before next attempt…")
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


def process_maybe_chunked(api: GradioClient, f: pathlib.Path) -> str:
    """If file is large, split locally and merge transcripts, otherwise process normally."""
    if not is_big_file(f):
        return process_file_with_retry(api, f)

    log("[INFO] Big file detected → splitting locally to avoid 502 timeouts…")
    parts = ffmpeg_split(f, CHUNK_SEC)
    merged: List[str] = []
    for i, part in enumerate(parts, 1):
        step(f"PROCESS CHUNK {i}/{len(parts)}: {part.name}")
        say_time()
        try:
            text = process_file_with_retry(api, part)
        except Exception as e:
            log(f"[ERROR] Predict failed for chunk {i}: {e}")
            text = f"[Chunk {i} failed]\n"
        merged.append(f"=== [Chunk {i}] {part.name} ===\n{text}\n")

    final_text = "\n".join(merged)
    cleanup_chunks(f)
    return final_text


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
# CLI
# -------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="Batch upload & transcribe via Gradio /api/predict with local chunking.")
    p.add_argument("--keep-chunks", action="store_true", help="Do not delete chunk files after processing (debug).")
    p.add_argument("--size-split-mb", type=int, default=SIZE_SPLIT_MB, help="Split if file size >= MB (default: 50).")
    p.add_argument("--chunk-sec", type=int, default=CHUNK_SEC, help="Chunk length in seconds (default: 600).")
    return p.parse_args()


# -------------------------------
# Main
# -------------------------------
def main():
    global KEEP_CHUNKS, SIZE_SPLIT_MB, CHUNK_SEC

    args = parse_args()
    KEEP_CHUNKS   = bool(args.keep_chunks)
    SIZE_SPLIT_MB = int(args.size_split_mb)
    CHUNK_SEC     = int(args.chunk_sec)

    base = ensure_base(BASE_URL)
    log(f"[BOOT] Using BASE_URL: {base}")

    files = find_audio_files(FILES_DIR)
    if not files:
        log(f"[ERROR] No eligible audio file in '{FILES_DIR}'.")
        return

    api = GradioClient(base)

    for f in files:
        step(f"PROCESS FILE: {f.name}")
        log("Timer started…")
        say_time()

        try:
            best_text = process_maybe_chunked(api, f)
        except Exception as e:
            log(f"[ERROR] Predict failed: {e}")
            log(traceback.format_exc(limit=2).strip())
            continue

        log(f"[INFO] Picked best length={len(best_text)}")
        save_transcript(OUTPUT_DIR, f, best_text, res_id=None)

        log("[DONE] File processed.\n")
        add_to_visited(f.name)

    log("[ALL DONE] All files saved to OUTPUT_DIR.")


if __name__ == "__main__":
    main()
"""
python3 main.py --size-split-mb 80 --chunk-sec 900
finetune
"""
