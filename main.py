import requests, time, random, string, mimetypes, json, sys

BASE = "https://phon.nytud.hu"
APP  = "/beast2"
REFERER = f"{BASE}{APP}/"
FNAME = "LL5p.m4a"

def rid(n=11): 
    return ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))

def guess_mime(p):
    return mimetypes.guess_type(p)[0] or "application/octet-stream"

s = requests.Session()

session_hash = rid()
event_id     = rid()      # ideiglenes; a JOIN visszaad egy "valódi" event_id-t
upload_id    = rid()
print("session_hash:", session_hash, "event_id:", event_id, "upload_id:", upload_id)

# 1) JOIN (nálad listát kér a 'data' mező)
join_payload = {"data": [], "session_hash": session_hash, "event_id": event_id, "fn_index": 0}
jh = {"Origin": BASE, "Referer": REFERER, "Content-Type": "application/json"}
jr = s.post(f"{BASE}{APP}/queue/join", json=join_payload, headers=jh, timeout=30)
print("JOIN:", jr.status_code, jr.text[:200])
jr.raise_for_status()

# Használd a JOIN válaszában kapott event_id-t!
try:
    join_event_id = jr.json().get("event_id") or event_id
except Exception:
    join_event_id = event_id

# 2) UPLOAD
mime = guess_mime(FNAME)
uh = {"Origin": BASE, "Referer": REFERER}
with open(FNAME, "rb") as f:
    up = s.post(f"{BASE}{APP}/upload",
                params={"upload_id": upload_id},
                files={"files": (FNAME, f, mime)},
                headers=uh, timeout=180)
print("UPLOAD(files):", up.status_code, up.text[:200])
up.raise_for_status()

# tmp path a modell inputjához
tmp_list = up.json() if up.headers.get("content-type","").startswith("application/json") else []
if not tmp_list:
    sys.exit("❌ Nem találtam tmp fájl útvonalat az upload válaszában.")
tmp_path = tmp_list[0]

# 3) PREDICT / PUSH – több fn_index és payload forma próbálása
push_url = f"{BASE}{APP}/queue/push"
ph = {"Origin": BASE, "Referer": REFERER, "Content-Type": "application/json"}

fn_index_found = None
push_ok = False
push_resp_text = ""

# két gyakori adatforma: csak az útvonal, vagy {name, data} objektum
payload_variants = [
    lambda fn_idx: {"data": [tmp_path], "event_id": join_event_id, "session_hash": session_hash, "fn_index": fn_idx},
    lambda fn_idx: {"data": [{"name": FNAME, "data": tmp_path}], "event_id": join_event_id, "session_hash": session_hash, "fn_index": fn_idx},
]

for fn_idx in range(0, 7):  # ha kell, emeld feljebb
    for build in payload_variants:
        pp = build(fn_idx)
        r = s.post(push_url, json=pp, headers=ph, timeout=60)
        push_resp_text = r.text[:200]
        print(f"PUSH fn_index={fn_idx} ->", r.status_code, push_resp_text)
        # siker eset: 200 OK, és nem "function has no backend method." hiba
        if r.ok and "no backend method" not in r.text:
            fn_index_found = fn_idx
            push_ok = True
            break
    if push_ok:
        break

if not push_ok:
    sys.exit("❌ Nem sikerült elindítani a feldolgozást (push). Nézd meg DevTools→/beast2/queue/push → Request Payload: pontos data-list és fn_index.")

# 3/b) opcionális: progress SSE (done-ig figyelünk)
prog_headers = {"Accept": "text/event-stream", "Origin": BASE, "Referer": REFERER}
prog = s.get(f"{BASE}{APP}/upload_progress",
             params={"upload_id": upload_id},
             headers=prog_headers, stream=True, timeout=120)
print("PROGRESS:", prog.status_code)
if prog.ok:
    for line in prog.iter_lines(decode_unicode=True):
        if line and line.startswith("data:"):
            msg = line[5:].strip()
            print("progress:", msg)
            if '"done"' in msg or '"complete": true' in msg:
                break

# 4) DATA (SSE) – itt jön a kész eredmény
dh = {"Accept": "text/event-stream", "Origin": BASE, "Referer": REFERER}
data = s.get(f"{BASE}{APP}/queue/data",
             params={"session_hash": session_hash},
             headers=dh, stream=True, timeout=300)
print("DATA:", data.status_code)
data.raise_for_status()

# 5) process_completed feldolgozás
completed = None
all_lines = []
for line in data.iter_lines(decode_unicode=True):
    if line is None:
        continue
    all_lines.append(line)
    if line.startswith("data:"):
        raw = line[5:].strip()
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if obj.get("msg") == "process_completed":
            completed = obj
            break

open("output_sse.txt", "w", encoding="utf-8").write("\n".join(all_lines))

if not completed:
    print("ℹ️ Nem jött process_completed; nézd meg az output_sse.txt-t.")
else:
    out = completed.get("output", {})
    # mentsük teljesen
    open("output.json", "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=2))
    print("✅ output.json kész.")

    # próbáljuk kiszedni a leiratot tipikus helyekről
    transcript = None
    if isinstance(out, dict) and "data" in out:
        # gradio általában listát ad vissza "data" kulcsban
        for item in out["data"]:
            if isinstance(item, str) and len(item) > 50:
                transcript = item
                break
            if isinstance(item, list):
                # néha listában van a szöveg
                for sub in item:
                    if isinstance(sub, str) and len(sub) > 50:
                        transcript = sub
                        break
            if transcript:
                break

    if transcript:
        open("transcript.txt", "w", encoding="utf-8").write(transcript)
        print("📝 transcript.txt mentve.")
    else:
        print("ℹ️ Nem találtam szöveges leiratot az outputban – nézd meg az output.json-t a szerkezet miatt.")
