"""Quiz Shorts bot: AI writes + fact-checks a quiz, renders a 9:16 video with voice and sound.
Reads (never edits or deletes) your channel stats and comments so the AI learns what works."""
import json, os, sys, re, random, textwrap, subprocess, difflib, datetime, math, urllib.request, urllib.error
from functools import lru_cache
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
CFG = json.load(open("config.json")) if os.path.exists("config.json") else {}
LANG = CFG.get("language", "en")
VOICES = {"en": "en-US-GuyNeural", "fr": "fr-FR-HenriNeural"}
VOICE = CFG.get("voice", VOICES.get(LANG, VOICES["en"]))
TXT = {
 "en": {"comment": "Comment your answer. You have five seconds.", "answer": "The answer is {L}, {opt}.",
        "outro": "Follow for a new quiz every 12 hours.", "quiz": "QUIZ", "name": "English"},
 "fr": {"comment": "Commente ta réponse. Tu as cinq secondes.", "answer": "La réponse est {L}, {opt}.",
        "outro": "Abonne-toi pour un nouveau quiz toutes les 12 heures.", "quiz": "QUIZ", "name": "French"}}
T = TXT.get(LANG, TXT["en"])
LET = "ABCD"
TYPES = ["multiple_choice", "true_false", "riddle", "math", "odd_one_out", "clue_guess"]
GEN_MODEL = "openai/gpt-4.1-mini"
VERIFIERS = ["openai/gpt-4.1", "meta/Llama-3.3-70B-Instruct"]
API = "https://models.github.ai/inference/chat/completions"

HIST = json.load(open("history.json")) if os.path.exists("history.json") else []
for i, h in enumerate(HIST):
    h.setdefault("n", i + 1); h.setdefault("type", "multiple_choice"); h.setdefault("title", "")
COMMENTS = []

# ---------------------------------------------------------------- channel sync (read-only)
def sync_channel():
    if not os.environ.get("YT_REFRESH"): return
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        c = Credentials(None, refresh_token=os.environ["YT_REFRESH"], token_uri="https://oauth2.googleapis.com/token",
        try:  # optional: watch-time retention (needs the analytics read permission; skipped if absent)
            ids = [h["id"] for h in HIST if h.get("id")][-50:]
            ya = build("youtubeAnalytics", "v2", credentials=c)
            r = ya.reports().query(ids="channel==MINE", startDate="2020-01-01", endDate=datetime.date.today().isoformat(),
                metrics="views,averageViewPercentage", dimensions="video", filters="video==" + ",".join(ids)).execute()
            for row in r.get("rows", []):
                for h in HIST:
                    if h.get("id") == row[0]: h["retention"] = round(float(row[2]), 1)
        except Exception as e:
            print("retention skipped (optional):", str(e)[:120])
    except Exception as e:
        print("channel sync skipped:", e)

def score(h, use_ret):
    return h.get("retention") if use_ret else h.get("views")

def learning_summary():
    use_ret = sum("retention" in h for h in HIST) >= 5
    groups = {}
    for h in HIST:
        s = score(h, use_ret)
        if s is not None: groups.setdefault(h.get("format", "?"), []).append(s)
    avg = sorted(((sum(v) / len(v), k, len(v)) for k, v in groups.items()), reverse=True)
    if sum(n for _, _, n in avg) < 6: return "No reliable data yet: vary types and categories.", use_ret
    unit = "% watched" if use_ret else "views"
    fmt = lambda x: f"{x[1]} (avg {x[0]:.0f} {unit}, {x[2]} videos)"
    return "Best formats: " + "; ".join(fmt(x) for x in avg[:3]) + ". Worst: " + "; ".join(fmt(x) for x in avg[-2:]) + ".", use_ret

def write_report(use_ret=False):
    L = ["# Channel report", "", "| Views | Watched % | Likes | Format | Question |", "|---|---|---|---|---|"]
    for h in reversed(HIST[-40:]):
        L.append(f'| {h.get("views","-")} | {h.get("retention","-")} | {h.get("likes","-")} | {h.get("format","")} | {h["hook"]} |')
    L += ["", "## Latest comments", ""] + ([f"- **{q[:50]}**: {t}" for q, t in COMMENTS[:40]] or ["(none yet)"])
    open("REPORT.md", "w").write("\n".join(L))

# ---------------------------------------------------------------- AI: write + fact-check
def call_ai(model, prompt, temp=0.7):
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        print("AI skipped: no GITHUB_TOKEN"); return None
    body = json.dumps({"model": model, "temperature": temp, "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "Authorization": "Bearer " + tok, "Content-Type": "application/json",
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "quiz-shorts-bot"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            status, raw = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        print("AI HTTP error:", model, e.code, e.read().decode("utf-8", "replace")[:300]); return None
    except Exception as e:
        print("AI call failed:", model, str(e)[:150]); return None
    try:
        return json.loads(raw)["choices"][0]["message"]["content"]
    except Exception:
        print("AI bad reply:", model, "status", status, "body starts:", raw[:300]); return None

def jparse(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    try: return json.loads(m.group(0)) if m else None
    except Exception: return None

def clean(q):
    try:
        q = dict(q)
        t = q.get("type", "multiple_choice"); t = t if t in TYPES else "multiple_choice"
        opts = [str(o).strip() for o in q["options"]]
        n = 2 if t == "true_false" else 4
        assert len(opts) == n and len({o.lower() for o in opts}) == n and all(0 < len(o) <= 30 for o in opts)
        a = int(q["answer"]); assert 0 <= a < n
        qu = str(q["question"]).strip(); assert 8 <= len(qu) <= 150
        if t != "true_false":
            good = opts[a]; random.shuffle(opts); a = opts.index(good)
        q.update(type=t, options=opts, answer=a, question=qu, explain=str(q.get("explain", "")).strip()[:140],
                 cat=str(q.get("cat", "general")).lower()[:20], hook=str(q.get("hook", "")).strip()[:50],
                 title=str(q.get("title", "")).strip()[:90], tags=[str(x)[:30] for x in q.get("tags", [])][:8])
        return q
    except Exception as e:
        print("bad quiz skipped:", e); return None

def too_similar(q):
    return any(difflib.SequenceMatcher(None, q["question"].lower(), h["hook"].lower()).ratio() > 0.8 for h in HIST[-80:])

def verify(q):
    """A stronger, different AI solves the quiz blind. Only quizzes where it agrees AND is sure get posted."""
    prompt = ("Solve this quiz question yourself. Be strict: if it is ambiguous, has no single correct option, "
              "or you are not completely sure, say sure=false.\nQuestion: " + q["question"] + "\n" +
              "\n".join(f"{i}: {o}" for i, o in enumerate(q["options"])) +
              '\nReply ONLY JSON: {"answer": <option index>, "sure": true or false}')
    for m in VERIFIERS:
        r = jparse(call_ai(m, prompt, 0))
        if r is not None:
            try: return int(r["answer"]) == q["answer"] and r.get("sure") is True
            except Exception: return False
    return False

def gen_prompt(summary):
    seen = [h["hook"] for h in HIST][-40:]
    return (f"You write ONE quiz for a 25-second YouTube Short on a faceless quiz channel. Write it in {T['name']}.\n"
        f"Types: {TYPES}. true_false has 2 options [\"True\",\"False\"] (translated if not English); every other type has exactly 4 short options (max 28 characters each). "
        "odd_one_out: which option does not belong. clue_guess: 2 or 3 short clues, guess the country/person/thing. math: quick mental maths with one exact answer.\n"
        "Categories: football, geography, science, general, anime, minecraft, roblox, african culture, movies, history, animals.\n"
        f"Already used, never repeat or rephrase: {seen}\n{summary}\n"
        f"Viewer comments (they may request topics, say an answer is wrong, or complain; learn from them, never repeat a question people say is wrong): {[t for _, t in COMMENTS][:25]}\n"
        "With no data, vary type, category and difficulty. Only facts you are 100 percent sure of; no politics, religion, violence, or anything about real private people; family friendly.\n"
        'Reply ONLY JSON: {"type":"...","cat":"...","difficulty":"easy|medium|hard","hook":"max 6 word attention grabber, e.g. Only 5 percent get this","question":"...","options":["..",".."],"answer":0,"explain":"one short sentence","title":"catchy YouTube title max 60 chars","tags":["quiz","trivia","..."]} '
        "where answer is the index of the correct option.")

def pick_quiz():
    summary, use_ret = learning_summary()
    for attempt in range(5):
        raw = call_ai(GEN_MODEL, gen_prompt(summary), 1.0)
        if raw is None: return None  # AI unreachable: stop retrying, use the backup quiz
        q = clean(jparse(raw) or {})
        if not q or too_similar(q): continue
        if verify(q): return q
        print("fact-check failed, trying another quiz")
    return None

def backup_quiz():
    if LANG != "en": return None
    used = [h["hook"] for h in HIST]
    bank = json.load(open("quizzes.json"))
    for b in bank + bank:
        if b["question"] not in used or b is bank[-1]:
            q = clean(b)
            if q: return q
    return clean(bank[0])

# ---------------------------------------------------------------- video rendering
THEMES = {"football": ((8, 70, 45), (16, 125, 72), (255, 236, 120)), "geography": ((8, 50, 90), (20, 115, 155), (255, 210, 63)),
 "science": ((40, 20, 90), (95, 55, 175), (120, 255, 200)), "minecraft": ((38, 28, 18), (72, 118, 42), (255, 200, 80)),
 "roblox": ((70, 10, 20), (160, 30, 45), (255, 220, 100)), "anime": ((70, 20, 90), (175, 55, 135), (255, 225, 240)),
 "movies": ((30, 10, 10), (105, 20, 32), (255, 200, 60)), "history": ((60, 40, 15), (140, 95, 40), (255, 235, 170)),
 "animals": ((15, 60, 40), (70, 130, 60), (255, 225, 120)), "african": ((70, 30, 10), (170, 80, 20), (255, 230, 90))}
DEFAULT = ((16, 36, 58), (76, 96, 138), (255, 210, 63))
INK, GREEN, DIM, CORAL = (242, 248, 255), (61, 220, 151), (82, 100, 120), (255, 107, 107)

def theme(cat):
    for k, v in THEMES.items():
        if k in cat: return v
    return DEFAULT

_bg = {}
def background(th):
    if th not in _bg:
        img = Image.new("RGB", (W, H)); d = ImageDraw.Draw(img)
        for y in range(H):
            t = y / H
            d.line([(0, y), (W, y)], fill=tuple(int(th[0][i] + (th[1][i] - th[0][i]) * t) for i in range(3)))
        _bg[th] = img
    return _bg[th].copy()

@lru_cache(None)
def font(size): return ImageFont.truetype(FONT, size)

def wrap(d, text, size, maxw):
    f, lines, cur = font(size), [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if d.textlength(t, font=f) <= maxw: cur = t
        else:
            if cur: lines.append(cur)
            cur = w
    return lines + ([cur] if cur else [])

def fit(d, text, size, maxw):
    while size > 28 and d.textlength(text, font=font(size)) > maxw: size -= 2
    return font(size)

def card(path, q, num, mode, remaining=5.0, pop=0):
    th = theme(q["cat"]); acc = th[2]
    img = background(th); d = ImageDraw.Draw(img)
    d.text((W // 2, 120), f"{q['cat'].upper()} {T['quiz']} #{num}", font=font(44), fill=acc, anchor="mm")
    if mode == "hook":
        y = H // 2 - 150
        for line in wrap(d, q["hook"], 104, 900):
            d.text((W // 2, y), line, font=font(104), fill=acc, anchor="mm"); y += 130
        img.save(path); return
    qs = 72 if len(q["question"]) <= 60 else 62 if len(q["question"]) <= 100 else 54
    y = 290
    for line in wrap(d, q["question"], qs, 920):
        d.text((W // 2, y), line, font=font(qs), fill=acc, anchor="mm"); y += qs + 18
    n = len(q["options"]); bh, gap = (150, 40) if n == 4 else (230, 60)
    y += 60
    for k, opt in enumerate(q["options"]):
        col = (225, 238, 255)
        grow = 0
        if mode == "reveal":
            col = GREEN if k == q["answer"] else DIM
            grow = pop if k == q["answer"] else 0
        d.rounded_rectangle([90 - grow, y - grow, W - 90 + grow, y + bh + grow], 30, fill=col)
        txt = f"{LET[k]}.  {opt}" if n == 4 else opt
        d.text((W // 2, y + bh // 2), txt, font=fit(d, txt, 52, 820), fill=(16, 36, 58), anchor="mm")
        y += bh + gap
    if mode == "count":
        d.text((W // 2, H - 330), str(math.ceil(remaining - 1e-6)), font=font(190), fill=CORAL, anchor="mm")
        d.rounded_rectangle([140, H - 190, W - 140, H - 160], 15, fill=(0, 0, 0))
        d.rounded_rectangle([140, H - 190, 140 + int((W - 280) * max(remaining, 0) / 5), H - 160], 15, fill=CORAL)
    if mode == "reveal":
        for k, line in enumerate(wrap(d, q["explain"], 50, 900)[:3]):
            d.text((W // 2, H - 330 + k * 66), line, font=font(50), fill=INK, anchor="mm")
    img.save(path)

def tts(text, path):
    if os.environ.get("TEST_TTS"):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=2.5:sample_rate=24000", "-f", "wav", path], check=True); return
    subprocess.run(["edge-tts", "--voice", VOICE, "--text", text, "--write-media", path], check=True)

def dur(path):
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]).decode().strip())

def sh(*a): subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *a], check=True)

def make_video(q, num):
    os.makedirs("frames", exist_ok=True); os.makedirs("out", exist_ok=True)
    spoken_q = q["question"] + " " + " ".join(f"{LET[k]}, {o}." for k, o in enumerate(q["options"])) + " " + T["comment"]
    spoken_a = T["answer"].format(L=LET[q["answer"]], opt=q["options"][q["answer"]]) + " " + q["explain"] + " " + T["outro"]
    clips = []
    if q["hook"]:
        tts(q["hook"], "hook.mp3"); clips.append(("hook.mp3", 0))
    tts(spoken_q, "q.mp3"); clips.append(("q.mp3", 0))
    sh("-f", "lavfi", "-i", "sine=frequency=1500:duration=0.05:sample_rate=24000", "-af", "apad=whole_dur=1,volume=0.45", "tick.wav")
    clips.append(("tick.wav", 4))  # looped to 5 ticks = 5 seconds
    sh("-f", "lavfi", "-i", "sine=frequency=1320:duration=0.8:sample_rate=24000", "-af", "afade=t=out:st=0.05:d=0.75,volume=0.5", "ding.wav")
    clips.append(("ding.wav", 0))
    tts(spoken_a, "a.mp3"); clips.append(("a.mp3", 0))
    cmd, fl = [], []
    for i, (p, loops) in enumerate(clips):
        if loops: cmd += ["-stream_loop", str(loops)]
        cmd += ["-i", p]
        fl.append(f"[{i}:a]aresample=24000,aformat=channel_layouts=mono[a{i}]")
    fl.append("".join(f"[a{i}]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=0:a=1[out]")
    sh(*cmd, "-filter_complex", ";".join(fl), "-map", "[out]", "voice.wav")

    seq = []
    if q["hook"]:
        card("frames/hook.png", q, num, "hook"); seq.append(("frames/hook.png", dur("hook.mp3")))
    card("frames/q.png", q, num, "question"); seq.append(("frames/q.png", dur("q.mp3")))
    for k in range(50):
        card(f"frames/c{k}.png", q, num, "count", remaining=5 - k * 0.1); seq.append((f"frames/c{k}.png", 0.1))
    pops = [0, 14, 26, 14]
    for k, p in enumerate(pops):
        card(f"frames/r{k}.png", q, num, "reveal", pop=p); seq.append((f"frames/r{k}.png", 0.1))
    card("frames/r_end.png", q, num, "reveal"); seq.append(("frames/r_end.png", 0.8 + dur("a.mp3") - 0.1 * len(pops)))
    with open("list.txt", "w") as f:
        for p, d_ in seq: f.write(f"file '{p}'\nduration {d_:.3f}\n")
        f.write(f"file '{seq[-1][0]}'\n")
    name = f"out/short_{num}.mp4"
    sh("-f", "concat", "-i", "list.txt", "-i", "voice.wav", "-vf", "fps=30,format=yuv420p", "-c:v", "libx264",
       "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", name)
    return name

# ---------------------------------------------------------------- main
if __name__ == "__main__":
    sync_channel()
    q = pick_quiz()
    if not q:
        print("AI quiz unavailable or failed the fact-check; using the pre-checked backup quiz.")
        q = backup_quiz()
    if not q:
        print("No quiz available this run (non-English backup bank is empty). Skipping."); write_report(); sys.exit(0)
    num = (HIST[-1]["n"] if HIST else 0) + 1
    name = make_video(q, num)
    title = q["title"] or ("Can you get this right? " + q["question"])[:90]
    meta = {"title": title, "tags": q["tags"] or ["quiz", "trivia"],
            "description": f"{title}\n\nNew quiz every 12 hours. #shorts #quiz\n\n[q{num}]", "language": LANG}
    json.dump(meta, open("out/meta.json", "w"))
    open("out/title.txt", "w").write(title)
    HIST.append({"n": num, "hook": q["question"], "type": q["type"], "format": f"{q['type']}/{q['cat']}",
                 "difficulty": q.get("difficulty", ""), "title": title, "id": None})
    json.dump(HIST, open("history.json", "w"), indent=1)
    write_report()
    print("made", name)
