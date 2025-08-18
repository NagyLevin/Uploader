#!/usr/bin/env python3
import os
import time
import json
import pathlib
import mimetypes
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

import requests

# ---- Config (ASCII-safe) ----
BASE_URL   = "https://phon.nytud.hu/beast2/"
FILES_DIR  = pathlib.Path("/mnt/c/Users/Levinwork/Documents/Nytud/1feladat/celanyag/audio")
OUTPUT_DIR = pathlib.Path("/mnt/c/Users/Levinwork/Documents/Nytud/1feladat/celanyag/javtest")

# Allowed audio extensions
ALLOWED_EXTS = {".mp3", ".m4a", ".wav", ".flac", ".ogg"}

# HTTP timeout (seconds)
HTTP_TIMEOUT = 120

# Polling sleep
SLEEP_T = 1.0

# Bookkeeping files
VISITED_FILE = pathlib.Path("./visited.txt")
DEBUG_DIR = pathlib.Path("./debug_api")
DEBUG_DIR.mkdir(parents=True, exist_ok=True)


# -------------------------------
# Utility helpers
# -------------------------------
def say_time():
    now = datetime.now()
    print("Time now:", now.strftime("%H:%M:%S"))

def add_to_visited(text: str) -> None:
    """Append text to visited.txt if not present."""
    VISITED_FILE.touch(exist_ok=True)
    with VISITED_FILE.open("r", encoding="utf-8") as f:
        visited = {line.strip() for line in f if line.strip()}
    if text not in visited:
        with VISITED_FILE.open("a", encoding="utf-8") as f:
            f.write(text + "\n")

def check_visited(text: str) -> bool:
    """Return True if text already in visited.txt."""
    VISITED_FILE.touch(exist_ok=True)
    with VISITED_FILE.open("r", encoding="utf-8") as f:
        visited = {line.strip() for line in f if line.strip()}
    return text in visited

def timer(action="start"):
    """Simple wall-clock timer."""
    if not hasattr(timer, "_t"):
        timer._t = None
    if action == "start":
        timer._t = time.time()
        print("Timer started...")
    elif action == "stop":
        if timer._t is None:
            print("Start the timer first!")
        else:
            elapsed = time.time() - timer._t
            print(f"Elapsed: {elapsed:.3f} sec")
            timer._t = None
    else:
        print("Unknown timer action. Use: timer('start') or timer('stop')")

def ensure_base(url: str) -> str:
    """Ensure BASE_URL ends with a single slash."""
    return url.rstrip("/") + "/"

def jdump(obj: Any, path: pathlib.Path, note: str = ""):
    """Save JSON for debugging."""
    try:
        with path.open("w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        if note:
            print(f"[DEBUG] Saved JSON to {path} ({note})")
        else:
            print(f"[DEBUG] Saved JSON to {path}")
    except Exception as e:
        print("[DEBUG] JSON dump error:", e)


# -------------------------------
# Discovery helpers (optional but useful)
# -------------------------------
def discover_fn_index_and_flags(base: str) -> Tuple[Optional[int], bool]:
    """
    Try to read /config to find fn_index for api_name 'partial_2' and enable_queue flag.
    """
    base = ensure_base(base)
    cfg_url = base + "config"
    fn_index = None
    enable_queue = True
    try:
        r = requests.get(cfg_url, timeout=HTTP_TIMEOUT)
        if r.status_code == 200 and r.headers.get("content-type","").startswith("application/json"):
            cfg = r.json()
            jdump(cfg, DEBUG_DIR / "discover_config.json", note="GET /config")
            enable_queue = bool(cfg.get("enable_queue", True))
            deps = cfg.get("dependencies") or []
            for dep in deps:
                if isinstance(dep, dict) and dep.get("api_name") == "partial_2":
                    # In Gradio Blocks, 'id' works as fn_index for /api/predict fallback.
                    fn_index = dep.get("id")
                    break
    except Exception:
        pass
    return fn_index, enable_queue


# -------------------------------
# Gradio API client (named endpoint with fallbacks)
# -------------------------------
class GradioClient:
    """
    - Uploads via /upload (returns list[str] with one absolute path)
    - Predict tries:
        1) /api/route/partial_2
        2) /api/predict/partial_2
        3) /api/predict  with fn_index (from /config)
    """

    def __init__(self, base_url: str):
        self.base = ensure_base(base_url)
        self.session = requests.Session()
        self.upload_path = "upload"
        self.api_name = "partial_2"
        self.fn_index, self.enable_queue = discover_fn_index_and_flags(self.base)
        print(f"[INFO] Discovery: fn_index={self.fn_index}, enable_queue={self.enable_queue}")

    def upload_file(self, file_path: pathlib.Path) -> Dict[str, Any]:
        """
        Upload a file via /upload.
        Returns a FileData dict: {"path": "..."} suitable for Gradio JSON.
        """
        url = self.base + self.upload_path
        mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        with file_path.open("rb") as f:
            files = {"files": (file_path.name, f, mime)}
            r = self.session.post(url, files=files, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        # Newer Gradio returns e.g. ["/tmp/gradio/.../file.m4a"]
        file_path_str = None
        if isinstance(data, list) and data and isinstance(data[0], str):
            file_path_str = data[0]
        elif isinstance(data, dict) and "name" in data and isinstance(data["name"], str):
            v = data["name"]
            file_path_str = v.split("file=", 1)[-1]
        else:
            jdump(data, DEBUG_DIR / "unexpected_upload_response.json", note="upload_file")
            raise RuntimeError("Unexpected upload response; could not extract uploaded path.")

        print(f"[DEBUG] Uploaded path: {file_path_str}")
        return {"path": file_path_str}

    def _predict_try(self, endpoint: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        url = self.base + endpoint
        r = self.session.post(url, json=payload, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            jdump({"status": r.status_code, "text": r.text[:5000]}, DEBUG_DIR / "predict_http_error.json", note="predict")
            return None
        try:
            resp = r.json()
        except Exception:
            jdump({"raw": r.text[:5000]}, DEBUG_DIR / "predict_nonjson_response.json", note="predict")
            return None
        if isinstance(resp, dict) and resp.get("error"):
            jdump(resp, DEBUG_DIR / "predict_error_payload.json", note="predict_error")
            return None
        jdump(resp, DEBUG_DIR / f"last_predict_response.json", note=f"OK via {endpoint}")
        return resp

    def predict(self, filedata: Dict[str, Any]) -> Dict[str, Any]:
        """
        Try name-based routes first, then fallback to fn_index on /api/predict.
        """
        payload_named = {"data": [filedata, []]}  # [Audio(FileData), CheckboxGroup([])]
        # 1) /api/route/<api_name>
        resp = self._predict_try(f"api/route/{self.api_name}", payload_named)
        if resp is not None:
            return resp
        # 2) /api/predict/<api_name>
        resp = self._predict_try(f"api/predict/{self.api_name}", payload_named)
        if resp is not None:
            return resp
        # 3) /api/predict + fn_index
        if self.fn_index is not None:
            payload_fn = {"fn_index": self.fn_index, "data": [filedata, []]}
            resp = self._predict_try("api/predict", payload_fn)
            if resp is not None:
                return resp
        raise RuntimeError("All predict attempts failed (route and fn_index).")

    @staticmethod
    def extract_text_outputs(resp: Dict[str, Any]) -> List[str]:
        """
        Extract textual outputs from common Gradio response shapes.
        """
        texts = []
        if not isinstance(resp, dict):
            return texts
        for key in ("data", "result", "output"):
            if key in resp and isinstance(resp[key], list):
                for item in resp[key]:
                    if isinstance(item, str) and item.strip():
                        texts.append(item.strip())
                    elif isinstance(item, dict):
                        for tk in ("text", "label", "value"):
                            v = item.get(tk)
                            if isinstance(v, str) and v.strip():
                                texts.append(v.strip())
        for k, v in resp.items():
            if isinstance(v, str) and v.strip():
                texts.append(v.strip())
        # Deduplicate preserving order
        seen, out = set(), []
        for t in texts:
            if t not in seen:
                out.append(t); seen.add(t)
        return out

    @staticmethod
    def extract_links(resp: Dict[str, Any]) -> List[str]:
        """
        Extract any file URLs/paths (e.g., TextGrid).
        """
        links = []
        def walk(x):
            if isinstance(x, dict):
                for k in ("url", "href", "path"):
                    if k in x and isinstance(x[k], str):
                        links.append(x[k])
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(resp)
        return sorted(set(links), key=lambda s: (".TextGrid" not in s, s))


# -------------------------------
# File scanning & saving
# -------------------------------
def find_audio_files(root: pathlib.Path) -> List[pathlib.Path]:
    """Recursively find eligible audio files under root, excluding already-visited names."""
    all_files = [p for p in root.rglob("*") if p.is_file()]
    files = [p for p in all_files if p.suffix.lower() in ALLOWED_EXTS and not check_visited(p.name)]
    skipped = [p for p in all_files if p.suffix.lower() not in ALLOWED_EXTS]
    if skipped:
        print("[INFO] Skipped (not audio):", ", ".join(s.name for s in skipped))
    return files

def save_result(output_dir: pathlib.Path, file_in: pathlib.Path, text_value: str, res_id: Optional[str] = None):
    """Save result text mirroring input structure with .txt extension."""
    output_dir.mkdir(parents=True, exist_ok=True)
    rel_path = file_in.relative_to(FILES_DIR)
    out_path = (output_dir / rel_path).with_suffix(".txt")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        if res_id:
            f.write(f"ID: {res_id}\n")
        f.write(text_value if text_value else "[No output]\n")
    print(f"[INFO] Saved: {out_path}")

def pick_best_text(texts: List[str]) -> str:
    """Heuristic: pick the longest textual output."""
    return max(texts, key=len) if texts else ""


# -------------------------------
# Main
# -------------------------------
def main():
    base = ensure_base(BASE_URL)
    print("[DEBUG] Using BASE_URL:", base)

    files = find_audio_files(FILES_DIR)
    if not files:
        print(f"[ERROR] No eligible audio file in '{FILES_DIR}'.")
        return

    api = GradioClient(base)

    for f in files:
        print("\n[INFO] Uploading:", f.name)
        timer("start")
        say_time()

        # 1) Upload
        try:
            filedata = api.upload_file(f)   # {"path": "..."}
        except Exception as e:
            print("[ERROR] Upload failed:", e)
            jdump({"error": str(e)}, DEBUG_DIR / f"upload_error_{f.stem}.json")
            continue

        # 2) Predict (named endpoint with fallbacks)
        try:
            resp = api.predict(filedata)
        except Exception as e:
            print("[ERROR] Predict failed:", e)
            jdump({"error": str(e)}, DEBUG_DIR / f"predict_error_{f.stem}.json")
            continue

        # 3) Extract outputs
        texts = api.extract_text_outputs(resp)
        links = api.extract_links(resp)

        # Optional: glean a result ID
        res_id = None
        for k in ("id", "result_id", "run_id"):
            v = resp.get(k) if isinstance(resp, dict) else None
            if isinstance(v, str) and v.strip():
                res_id = v.strip()
                break

        best = pick_best_text(texts)
        if not best:
            print("[WARN] No text output detected; saving raw JSON pointer.")
            jdump(resp, DEBUG_DIR / f"no_text_{f.stem}.json", note="no text output")

        # 4) Save transcript
        save_result(OUTPUT_DIR, f, best, res_id=res_id)

        # 5) Show links (TextGrid etc.)
        if links:
            print("[INFO] Download links (first few):")
            for l in links[:5]:
                print("  -", l)

        timer("stop")
        add_to_visited(f.name)

    print("[INFO] Done. All files saved to OUTPUT_DIR.")


if __name__ == "__main__":
    main()
