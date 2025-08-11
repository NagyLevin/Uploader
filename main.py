import requests, time, random, string, mimetypes, json

BASE = "https://phon.nytud.hu"
APP  = "/beast2"
REFERER = f"{BASE}{APP}/"
FNAME = "LL5p.m4a"

def rid(n=11): return ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))
def guess_mime(p):
    return mimetypes.guess_type(p)[0] or "application/octet-stream"

s = requests.Session()

session_hash = rid()
event_id     = rid()
upload_id    = rid()
print("session_hash:", session_hash, "event_id:", event_id, "upload_id:", upload_id)

# 1) JOIN (a te szervered listát kér a 'data' mezőn)
join_payload = {"data": [], "session_hash": session_hash, "event_id": event_id, "fn_index": 0}
jh = {"Origin": BASE, "Referer": REFERER, "Content-Type": "application/json"}
jr = s.post(f"{BASE}{APP}/queue/join", json=join_payload, headers=jh, timeout=30)
print("JOIN:", jr.status_code, jr.text[:200])
jr.raise_for_status()

# 2) UPLOAD (/beast2/upload)
mime = guess_mime(FNAME)
uh = {"Origin": BASE, "Referer": REFERER}
with open(FNAME, "rb") as f:
    up = s.post(f"{BASE}{APP}/upload", params={"upload_id": upload_id},
                files={"files": (FNAME, f, mime)}, headers=uh, timeout=180)
print("UPLOAD(files):", up.status_code, up.text[:200])
up.raise_for_status()

# 3) (opcionális) PROGRESS
ph = {"Accept": "text/event-stream", "Origin": BASE, "Referer": REFERER}
pr = s.get(f"{BASE}{APP}/upload_progress", params={"upload_id": upload_id},
           headers=ph, stream=True, timeout=120)
print("PROGRESS:", pr.status_code)
for line in pr.iter_lines(decode_unicode=True):
    if line and line.startswith("data:"):
        msg = line[5:].strip()
        print("progress:", msg)
        if '"done"' in msg or '"complete": true' in msg:  # nálad "done"
            break

# 4) DATA (SSE) – FIGYELEM: /beast2/queue/data
dh = {"Accept": "text/event-stream", "Origin": BASE, "Referer": REFERER}
data = s.get(f"{BASE}{APP}/queue/data", params={"session_hash": session_hash},
             headers=dh, stream=True, timeout=300)
print("DATA:", data.status_code)
data.raise_for_status()

# 5) SSE feldolgozás: kinyerjük a process_completed outputját, ha van
completed_payload = None
all_lines = []
for line in data.iter_lines(decode_unicode=True):
    if line is None:
        continue
    all_lines.append(line)
    if line.startswith("data:"):
        try:
            payload = json.loads(line[5:].strip())
        except Exception:
            continue
        # tipikus gradio queue üzenetek: process_starts / process_generating / process_completed
        if payload.get("msg") == "process_completed":
            completed_payload = payload
            break

# mentsük a nyers SSE-t is
open("output_sse.txt", "w", encoding="utf-8").write("\n".join(all_lines))

if completed_payload:
    # a tényleges eredmény sokszor payload["output"]["data"][..]
    open("output.json", "w", encoding="utf-8").write(json.dumps(completed_payload, ensure_ascii=False, indent=2))
    print("✅ output.json kész (process_completed).")
else:
    print("ℹ️ Nem jött process_completed; nézd meg az output_sse.txt-t a nyers eseményekért.")
