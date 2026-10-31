"""Quiz Shorts bot: AI writes + fact-checks a quiz, renders a 9:16 video with voice and sound.
Reads (never edits or deletes) your channel stats and comments so the AI learns what works."""
import json, os, sys, re, random, textwrap, subprocess, difflib, datetime, math, urllib.request, urllib.error
from functools import lru_cache
from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1920
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
CFG = json.load(open("config.json")) if os.path.exists("config.json") else {}
LANG = CFG.get("language", "en")
HOURS = int(CFG.get("hours", 6))   # how often a new quiz is posted (the schedule itself is in .github/workflows/daily.yml)
VOICES = {"en": "en-US-GuyNeural", "fr": "fr-FR-HenriNeural"}
VOICE = CFG.get("voice", VOICES.get(LANG, VOICES["en"]))
TXT = {
 "en": {"comment": ["Comment your answer. You have five seconds.", "Think fast! Drop your answer in the comments.",
                    "No cheating! Comment your answer before time runs out.", "Tick tock! Type your answer in the comments."],
        "answer": "The answer is {L}, {opt}.",
        "outro": [f"Follow for a new quiz every {HOURS} hours.", "Did you get it right? Follow for more quizzes!", "Think you can beat the next one? Follow and find out."],
        "quiz": "QUIZ", "name": "English"},
 "fr": {"comment": ["Commente ta réponse. Tu as cinq secondes.", "Vite ! Écris ta réponse en commentaire.", "Pas de triche ! Commente avant la fin du temps."],
        "answer": "La réponse est {L}, {opt}.",
        "outro": [f"Abonne-toi pour un nouveau quiz toutes les {HOURS} heures.", "Tu as eu bon ? Abonne-toi pour d'autres quiz !"],
        "quiz": "QUIZ", "name": "French"}}
T = TXT.get(LANG, TXT["en"])
LET = "ABCD"
TYPES = ["multiple_choice", "true_false", "riddle", "math", "odd_one_out", "clue_guess", "emoji_guess", "flag_guess"]
GEN_MODELS = ["openai/gpt-4.1", "openai/gpt-4.1-mini", "deepseek/DeepSeek-V3-0324", "pollinations"]   # tries the next one if a model is busy
VERIFIERS = ["deepseek/DeepSeek-V3-0324", "meta/Llama-3.3-70B-Instruct", "openai/gpt-4.1-mini", "pollinations"]
API = "https://models.github.ai/inference/chat/completions"

_raw = json.load(open("history.json")) if os.path.exists("history.json") else []
if isinstance(_raw, dict): HIST, PLAYBOOK, PLAYBOOK_AT = _raw.get("videos", []), _raw.get("playbook", ""), _raw.get("playbook_at", 0)
else: HIST, PLAYBOOK, PLAYBOOK_AT = _raw, "", 0
NEXT_NUM = (HIST[-1].get("n", len(HIST)) if HIST else 0) + 1

def save_hist():
    json.dump({"videos": HIST, "playbook": PLAYBOOK, "playbook_at": PLAYBOOK_AT}, open("history.json", "w"), indent=1)
for i, h in enumerate(HIST):
    h.setdefault("n", i + 1); h.setdefault("type", "multiple_choice"); h.setdefault("title", "")
COMMENTS = []
SEEN_TITLES = []
YT = None; OWN_CHANNEL = ""; INTEL = ""
DISPUTED = []   # quizzes viewers said were wrong or unclear
DISPUTE_RE = re.compile(r"\b(incorrect|mistake|wrong answer|is wrong|not correct|not true|error)\b", re.I)
CATS = ["football", "geography", "science", "general", "anime", "minecraft", "roblox", "african culture", "movies", "history", "animals"]

# ---------------------------------------------------------------- channel sync (read-only)
def sync_channel():
    global YT, OWN_CHANNEL
    if not os.environ.get("YT_REFRESH"): return
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        c = Credentials(None, refresh_token=os.environ["YT_REFRESH"], token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ["YT_ID"], client_secret=os.environ["YT_SECRET"])
        yt = build("youtube", "v3", credentials=c)
        YT = yt; ch = yt.channels().list(part="contentDetails", mine=True).execute()["items"][0]
        OWN_CHANNEL = ch["id"]; up = ch["contentDetails"]["relatedPlaylists"]["uploads"]
        items = yt.playlistItems().list(part="contentDetails", playlistId=up, maxResults=50).execute()["items"]
        vids = yt.videos().list(part="snippet,statistics", id=",".join(i["contentDetails"]["videoId"] for i in items)).execute()["items"]
        for v in vids:
            SEEN_TITLES.append(v["snippet"]["title"].lower())
            for h in HIST:
                if f"[q{h['n']}]" in v["snippet"].get("description", ""):
                    st = v["statistics"]
                    h.update(id=v["id"], views=int(st.get("viewCount", 0)), likes=int(st.get("likeCount", 0)), published=v["snippet"].get("publishedAt", ""))
        for h in [h for h in HIST if h.get("id")][-8:]:
            try:
                r = yt.commentThreads().list(part="snippet", videoId=h["id"], maxResults=15, order="relevance").execute()
                for t in r["items"]:
                    txt = t["snippet"]["topLevelComment"]["snippet"]["textDisplay"][:300]
                    COMMENTS.append((h["hook"], txt[:200]))
                    if len(txt) > 25 and DISPUTE_RE.search(txt):
                        h["disputed"] = True; DISPUTED.append((h["hook"], txt[:160]))
            except Exception as e:
                print("comments skipped:", e)
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

def age_days(published):
    try: return max(1.0, (datetime.datetime.utcnow() - datetime.datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ")).total_seconds() / 86400)
    except Exception: return None

def vpd(h):
    """Views per day since upload, so a new video is not punished for being new."""
    if h.get("views") is None or not h.get("published"): return None
    a = age_days(h["published"]); return h["views"] / a if a else None

def verdicts():
    """Judge every video against the channel's own typical performance: GOOD, OK or WEAK."""
    vals = sorted(v for v in (vpd(h) for h in HIST) if v is not None)
    if len(vals) < 3: return {}, 0.0
    med = vals[len(vals) // 2]; out = {}
    for h in HIST:
        v = vpd(h)
        if v is None: continue
        out[h["n"]] = "GOOD" if v >= max(1.5 * med, med + 1) else "WEAK" if v <= 0.5 * med else "OK"
    return out, med

def learning_summary():
    use_ret = sum("retention" in h for h in HIST) >= 5
    vd, med = verdicts(); groups = {}
    for h in HIST:
        s_ = h.get("retention") if use_ret else vpd(h)
        if s_ is not None: groups.setdefault(h.get("format", "?"), []).append(s_)
    if sum(len(v) for v in groups.values()) < 4: return "No reliable data yet: vary types and categories.", use_ret
    avg = sorted(((sum(v) / len(v), k, len(v)) for k, v in groups.items()), reverse=True)
    unit = "% watched" if use_ret else "views/day"; fmt = lambda x: f"{x[1]} ({x[0]:.1f} {unit}, {x[2]} videos)"
    good = [h["hook"] for h in HIST if vd.get(h["n"]) == "GOOD"][-5:]; weak = [h["hook"] for h in HIST if vd.get(h["n"]) == "WEAK"][-5:]
    tried = [h.get("type", "") for h in HIST] + [h.get("format", "").split("/")[-1] for h in HIST]
    least = sorted(TYPES + CATS, key=lambda x: tried.count(x))[:4]
    return (f"Channel typical: {med:.1f} views/day. GOOD videos (make more like these): {good}. WEAK videos (avoid this style): {weak}. "
            f"Best formats: " + "; ".join(fmt(x) for x in avg[:3]) + ". Worst: " + "; ".join(fmt(x) for x in avg[-2:]) +
            f". Least tried so far (test these sometimes): {least}."), use_ret

def iso_secs(d):
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    return (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)) if m else 9999

def market_intel():
    """Reads PUBLIC stats of other quiz Shorts (titles, views, likes, tags) to see what is winning right now.
    It cannot watch them, but it can see which ones work and which flop, and the AI learns the patterns (never copies)."""
    global INTEL
    if YT is None: return
    try:
        vd, _ = verdicts()
        good_cats = [h.get("format", "").split("/")[-1] for h in HIST if vd.get(h["n"]) == "GOOD"]
        niche = good_cats[-1] if good_cats else random.choice(["minecraft", "football", "flag", "roblox", "anime", "emoji"])
        after = (datetime.datetime.utcnow() - datetime.timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ"); ids = []
        for qq in ("quiz shorts", f"{niche} quiz shorts"):
            r = YT.search().list(part="snippet", q=qq, type="video", videoDuration="short", order="viewCount", publishedAfter=after,
                                 maxResults=15, relevanceLanguage=LANG).execute()
            ids += [i["id"]["videoId"] for i in r.get("items", []) if i.get("id", {}).get("videoId")]
        ids = list(dict.fromkeys(ids))[:50]
        if not ids: return
        rows = []
        for v in YT.videos().list(part="snippet,statistics,contentDetails", id=",".join(ids)).execute()["items"]:
            if v["snippet"].get("channelId") == OWN_CHANNEL or iso_secs(v["contentDetails"].get("duration")) > 75: continue
            a = age_days(v["snippet"].get("publishedAt", "")) or 1.0; st = v["statistics"]; views = int(st.get("viewCount", 0))
            rows.append((views / a, views, int(st.get("likeCount", 0)), a, v["snippet"]["title"], (v["snippet"].get("tags") or [])[:5]))
        if len(rows) < 4: return
        rows.sort(reverse=True)
        f = lambda r: f'"{r[4]}" ({r[1]} views in {r[3]:.0f} days, {r[2]} likes, tags {r[5]})'
        INTEL = "Top quiz Shorts on YouTube right now: " + "; ".join(f(r) for r in rows[:8]) + ". Weak ones in the same niche: " + "; ".join(f(r) for r in rows[-3:]) + "."
        try:
            tr = YT.videos().list(part="snippet", chart="mostPopular", videoCategoryId="20", regionCode=CFG.get("region", "US"), maxResults=10).execute()["items"]
            INTEL += " Trending in gaming: " + "; ".join(t["snippet"]["title"] for t in tr[:8]) + "."
        except Exception: pass
        INTEL = INTEL[:2800]; print("market intel gathered from", len(rows), "videos")
    except Exception as e:
        print("market intel skipped:", str(e)[:150])

def write_report(use_ret=False):
    vd, med = verdicts()
    L = ["# Channel report", "", f"Typical video: {med:.1f} views/day", "", "| Verdict | Views | Watched % | Likes | Format | Question |", "|---|---|---|---|---|---|"]
    for h in reversed(HIST[-40:]):
        L.append(f'| {vd.get(h["n"], "-")} | {h.get("views","-")} | {h.get("retention","-")} | {h.get("likes","-")} | {h.get("format","")} | {h["hook"]} |')
    L += ["", "## Latest comments", ""] + ([f"- **{q[:50]}**: {t}" for q, t in COMMENTS[:40]] or ["(none yet)"])
    L += ["", "## Playbook (what the AI has learned)", "", PLAYBOOK or "(not enough data yet)", "", "## What other quiz channels are doing", "", INTEL or "(not gathered yet)"]
    open("REPORT.md", "w").write("\n".join(L))

# ---------------------------------------------------------------- AI: write + fact-check
def call_pollinations(prompt, temp):
    """Free AI that needs no key. Best effort: used only when GitHub's AI is unavailable."""
    if os.environ.get("TEST_TTS"): return None
    req = urllib.request.Request("https://text.pollinations.ai/openai", data=json.dumps({"model": "openai", "temperature": temp,
        "messages": [{"role": "user", "content": prompt}]}).encode(), headers={"Content-Type": "application/json", "User-Agent": "quiz-shorts-bot"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return json.loads(r.read().decode("utf-8", "replace"))["choices"][0]["message"]["content"]
    except Exception as e:
        print("AI call failed: pollinations", str(e)[:150]); return None

def call_ai(model, prompt, temp=0.7):
    if model == "pollinations": return call_pollinations(prompt, temp)
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

def call_chain(models, prompt, temp=0.7):
    for m in models:
        r = call_ai(m, prompt, temp)
        if r is not None: return r, m
    return None, None

def maybe_update_playbook():
    """The AI reviews real results (and what other channels do) every 4 videos and rewrites its own rules."""
    global PLAYBOOK, PLAYBOOK_AT
    vd, med = verdicts(); scored = [h for h in HIST if h["n"] in vd]
    if len(scored) < 5 or len(HIST) - PLAYBOOK_AT < 4: return
    show = lambda h: f'[{vd[h["n"]]}] {h.get("format", "?")} / {h.get("difficulty", "?")}: "{h["hook"]}" -> {vpd(h):.1f} views/day, {h.get("likes", 0)} likes'
    prompt = ("You are the strategist of a faceless YouTube Shorts quiz channel. Here are the real results, each judged against the channel's typical "
              f"{med:.1f} views/day:\n" + "\n".join(show(h) for h in sorted(scored, key=lambda h: -vpd(h))[:24]) +
              f"\nWhat other quiz channels are doing right now: {INTEL or 'unknown'}\nViewer comments: {[t for _, t in COMMENTS][:25]}\n"
              f"Previous playbook: {PLAYBOOK or 'none'}\n"
              "Write the updated playbook: at most 8 short rules, plain text, one per line, about what to make more and less of "
              "(quiz types, categories, difficulty, hooks, title style, topics viewers ask for, patterns seen in winning channels). "
              "Base every rule on the evidence above, keep rules that still hold, drop ones the data contradicts. Never copy another channel's exact title or question. Reply with the rules only.")
    r, m = call_chain(GEN_MODELS, prompt, 0.4)
    if r:
        PLAYBOOK, PLAYBOOK_AT = r.strip()[:1200], len(HIST); print("playbook updated by", m)

def jparse(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    try: return json.loads(m.group(0)) if m else None
    except Exception: return None

def strip_emoji(s):
    return re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]", "", str(s)).strip()

def clean(q):
    try:
        q = dict(q)
        t = q.get("type", "multiple_choice"); t = t if t in TYPES else "multiple_choice"
        opts = [strip_emoji(o) for o in q["options"]]
        n = 2 if t == "true_false" else 4
        assert len(opts) == n and len({o.lower() for o in opts}) == n and all(0 < len(o) <= 30 for o in opts)
        a = int(q["answer"]); assert 0 <= a < n
        qu = strip_emoji(q["question"]); assert 8 <= len(qu) <= 120
        emoji = "".join(str(q.get("emoji", "")).split()) if t in ("emoji_guess", "flag_guess") else ""
        if t in ("emoji_guess", "flag_guess"):
            assert emoji and len(emoji_clusters(emoji)) <= 6 and len(qu) <= 50
        icon = ""
        if not emoji:
            cl = emoji_clusters(str(q.get("icon", ""))); icon = cl[0] if cl else ""
        if t != "true_false":
            good = opts[a]; random.shuffle(opts); a = opts.index(good)
        diff = str(q.get("difficulty", "")).lower(); diff = diff if diff in ("easy", "medium", "hard") else ""
        q.update(type=t, options=opts, answer=a, question=qu, emoji=emoji, icon=icon, difficulty=diff,
                 explain=strip_emoji(q.get("explain", ""))[:140], cat=str(q.get("cat", "general")).lower()[:20],
                 hook=strip_emoji(q.get("hook", ""))[:50], title=str(q.get("title", "")).strip()[:90],
                 tags=[str(x)[:30] for x in q.get("tags", [])][:8])
        return q
    except Exception as e:
        print("bad quiz skipped:", e); return None

def too_similar(q):
    return any(difflib.SequenceMatcher(None, q["question"].lower(), h["hook"].lower()).ratio() > 0.8 for h in HIST[-80:])

def verify(q, skip=None):
    """A stronger, different AI solves the quiz blind. Only quizzes where it agrees AND is sure get posted."""
    prompt = ("Solve this quiz question yourself. Be strict: if it is ambiguous, has no single correct option, "
              "or you are not completely sure, or the answer depends on a condition the question does not state (like day or night, or a changing fact), say sure=false.\nQuestion: " + q["question"] + "\n" +
              (f"Emojis shown to the player: {q['emoji']}\n" if q.get("emoji") else "") +
              "\n".join(f"{i}: {o}" for i, o in enumerate(q["options"])) +
              '\nReply ONLY JSON: {"answer": <option index>, "sure": true or false}')
    for m in [m for m in VERIFIERS if m != skip]:
        r = jparse(call_ai(m, prompt, 0))
        if r is not None:
            try: return int(r["answer"]) == q["answer"] and r.get("sure") is True
            except Exception: return False
    return False

def gen_prompt(summary):
    seen = [h["hook"] for h in HIST][-40:]
    return (f"You write ONE quiz for a 25-second YouTube Short on a fun, high-energy faceless quiz channel. Write it in {T['name']}.\n"
        f"Types: {TYPES}. true_false has 2 options [\"True\",\"False\"] (translated if not English); every other type has exactly 4 short options (max 28 characters each).\n"
        "odd_one_out: which option does not belong. clue_guess: 2 or 3 short clues, guess the country/person/thing. math: quick mental maths with one exact answer.\n"
        "flag_guess: put exactly ONE country flag emoji in the emoji field, question like 'Which country's flag is this?', options are 4 country names. "
        "Every quiz that is NOT an emoji puzzle needs an icon: ONE emoji that illustrates the question topic (a fire emoji for burning, a planet emoji for space, a football for football). "
        "Write questions that make people say wait, really? Avoid boring textbook questions like how many continents are there. Mix easy and tricky, and make the explanation a surprising fact. "
        "emoji_guess: put 2 to 5 emojis in the emoji field that spell out a famous movie, animal, food, or word; question like 'Which movie is this?'; keep these two types' questions under 50 characters. "
        "Emoji puzzles are very popular: use them in about one of every three quizzes. The emoji field must be empty for all other types.\n"
        "Categories: football, geography, science, general, anime, minecraft, roblox, african culture, movies, history, animals.\n"
        f"Today's date is {datetime.date.today().isoformat()}. If a big event is happening around now that you are certain about (a tournament, a new movie or game), you may use it.\n"
        f"Your playbook, the lessons you learned from past results (follow them): {PLAYBOOK or 'none yet'}\n"
        f"Market intel from public YouTube data (learn the topics, hooks and title styles that win; NEVER copy any title or question): {INTEL or 'none'}\n"
        + ("EXPLORE MODE: for this quiz, pick one of the least-tried types or categories from the data above, to test something new.\n" if NEXT_NUM % 3 == 0 else "")
        + ("This is a BOSS LEVEL quiz: difficulty must be hard, but fair and 100 percent verifiable.\n" if NEXT_NUM % 5 == 0 else "") +
        f"Already used, never repeat or rephrase: {seen}\n{summary}\n"
        f"Viewers said these quizzes were wrong or unclear. Never repeat them, and write more precisely (state conditions such as day or night, or a year, when the answer depends on them): {DISPUTED[-8:] or 'none'}\n"
        f"Viewer comments (they may request topics, say an answer is wrong, or complain; learn from them, never repeat a question people say is wrong): {[t for _, t in COMMENTS][:25]}\n"
        "With no data, vary type, category and difficulty. Only facts you are 100 percent sure of; no politics, religion, violence, or anything about real private people; family friendly. "
        "Make the explanation fun and surprising in one short sentence.\n"
        'Reply ONLY JSON: {"type":"...","cat":"...","difficulty":"easy|medium|hard","hook":"max 5 word attention grabber like Think fast! or Can you get this? Never invent statistics or percentages","question":"...","emoji":"","icon":"","options":["..",".."],"answer":0,"explain":"one short fun sentence","title":"catchy YouTube title max 60 chars","tags":["quiz","trivia","..."]} '
        "where answer is the index of the correct option.")

def pick_quiz():
    summary, use_ret = learning_summary()
    for attempt in range(5):
        raw, used = call_chain(GEN_MODELS, gen_prompt(summary), 1.0)
        if raw is None: return None  # AI unreachable: stop retrying, use the backup quiz
        q = clean(jparse(raw) or {})
        if not q or too_similar(q): continue
        if q["icon"] and not puzzle_images(q["icon"]): q["icon"] = ""
        if q["emoji"] and not puzzle_images(q["emoji"]):
            print("emoji images unavailable, trying another quiz"); continue
        if verify(q, used): return q
        print("fact-check failed, trying another quiz")
    return None

COUNTRIES = [("Cameroon","CM","Yaoundé","Africa"),("Nigeria","NG","Abuja","Africa"),("Ghana","GH","Accra","Africa"),("Senegal","SN","Dakar","Africa"),
 ("Kenya","KE","Nairobi","Africa"),("Egypt","EG","Cairo","Africa"),("South Africa","ZA",None,"Africa"),("Morocco","MA","Rabat","Africa"),
 ("Algeria","DZ","Algiers","Africa"),("Ethiopia","ET","Addis Ababa","Africa"),("Tanzania","TZ","Dodoma","Africa"),("Uganda","UG","Kampala","Africa"),
 ("Ivory Coast","CI",None,"Africa"),("Mali","ML","Bamako","Africa"),("Tunisia","TN","Tunis","Africa"),("Zimbabwe","ZW","Harare","Africa"),
 ("Zambia","ZM","Lusaka","Africa"),("Angola","AO","Luanda","Africa"),("Gabon","GA","Libreville","Africa"),("DR Congo","CD","Kinshasa","Africa"),
 ("Namibia","NA","Windhoek","Africa"),("Rwanda","RW","Kigali","Africa"),("Libya","LY","Tripoli","Africa"),("Sudan","SD","Khartoum","Africa"),
 ("Botswana","BW","Gaborone","Africa"),("Madagascar","MG","Antananarivo","Africa"),("Mozambique","MZ","Maputo","Africa"),("Chad","TD","N'Djamena","Africa"),
 ("Niger","NE","Niamey","Africa"),("Burkina Faso","BF","Ouagadougou","Africa"),("Togo","TG","Lomé","Africa"),("Guinea","GN","Conakry","Africa"),
 ("Republic of the Congo","CG","Brazzaville","Africa"),("Central African Republic","CF","Bangui","Africa"),
 ("France","FR","Paris","Europe"),("Germany","DE","Berlin","Europe"),("Italy","IT","Rome","Europe"),("Spain","ES","Madrid","Europe"),
 ("Portugal","PT","Lisbon","Europe"),("United Kingdom","GB","London","Europe"),("Netherlands","NL","Amsterdam","Europe"),("Belgium","BE","Brussels","Europe"),
 ("Switzerland","CH","Bern","Europe"),("Sweden","SE","Stockholm","Europe"),("Norway","NO","Oslo","Europe"),("Greece","GR","Athens","Europe"),
 ("Poland","PL","Warsaw","Europe"),("Ireland","IE","Dublin","Europe"),("Austria","AT","Vienna","Europe"),("Croatia","HR","Zagreb","Europe"),("Denmark","DK","Copenhagen","Europe"),
 ("Japan","JP","Tokyo","Asia"),("China","CN","Beijing","Asia"),("India","IN","New Delhi","Asia"),("South Korea","KR","Seoul","Asia"),
 ("Saudi Arabia","SA","Riyadh","Asia"),("Thailand","TH","Bangkok","Asia"),("Vietnam","VN","Hanoi","Asia"),("Indonesia","ID","Jakarta","Asia"),
 ("Pakistan","PK","Islamabad","Asia"),("Iran","IR","Tehran","Asia"),("Qatar","QA","Doha","Asia"),("Turkey","TR","Ankara","Asia"),
 ("United States","US","Washington, D.C.","Americas"),("Canada","CA","Ottawa","Americas"),("Brazil","BR","Brasília","Americas"),("Argentina","AR","Buenos Aires","Americas"),
 ("Mexico","MX","Mexico City","Americas"),("Colombia","CO","Bogotá","Americas"),("Chile","CL","Santiago","Americas"),("Peru","PE","Lima","Americas"),
 ("Cuba","CU","Havana","Americas"),("Jamaica","JM","Kingston","Americas"),("Australia","AU","Canberra","Oceania"),("New Zealand","NZ","Wellington","Oceania")]

def flag_emoji(code): return "".join(chr(0x1F1E6 + ord(c) - 65) for c in code)

def pick_others(rnd, c, k, field):
    pool = [x for x in COUNTRIES if x[0] != c[0] and x[field] and x[field] != c[field]]
    same = [x for x in pool if x[3] == c[3]]; rnd.shuffle(same); rnd.shuffle(pool)
    out = same[:2] + [x for x in pool if x not in same[:2]]
    return out[:k]

def proc_one(kind, rnd):
    if kind == "flag":
        c = rnd.choice(COUNTRIES); opts = [c[0]] + [x[0] for x in pick_others(rnd, c, 3, 0)]
        return dict(type="flag_guess", cat="geography", difficulty="easy" if c[3] == "Africa" else "medium", emoji=flag_emoji(c[1]),
            hook=rnd.choice(["Do you know this flag?", "Flag challenge!", "Can you name it?"]), question="Which country's flag is this?",
            options=opts, answer=0, explain=f"That is the flag of {c[0]}." + (f" Its capital is {c[2]}." if c[2] else ""),
            title=f"Can you name this flag? {flag_emoji(c[1])}", tags=["flags", "geography", "quiz", "trivia"])
    if kind == "capital":
        c = rnd.choice([x for x in COUNTRIES if x[2]]); opts = [c[2]] + [x[2] for x in pick_others(rnd, c, 3, 2)]
        return dict(type="multiple_choice", cat="geography", difficulty="easy" if c[3] == "Africa" else "medium", icon=flag_emoji(c[1]),
            hook=rnd.choice(["Think fast!", "Capital check!", "Easy or tricky?"]), question=f"What is the capital of {c[0]}?",
            options=opts, answer=0, explain=f"{c[2]} is the capital of {c[0]}.",
            title=f"Do you know the capital of {c[0]}?", tags=["capitals", "geography", "quiz", "trivia"])
    k = rnd.choice(["mul", "pct", "add", "sub"])
    if k == "mul": a, b = rnd.randint(6, 15), rnd.randint(6, 15); ans = a * b; qq = f"Quick maths: what is {a} times {b}?"; ex = f"{a} times {b} is {ans}."
    elif k == "pct": p, base = rnd.choice([10, 20, 25, 50]), rnd.choice(range(40, 401, 20)); ans = base * p // 100; qq = f"Quick maths: what is {p} percent of {base}?"; ex = f"{p} percent of {base} is {ans}."
    elif k == "add": a, b = rnd.randint(120, 899), rnd.randint(120, 899); ans = a + b; qq = f"Quick maths: what is {a} plus {b}?"; ex = f"{a} plus {b} is {ans}."
    else: a = rnd.randint(300, 999); b = rnd.randint(100, a - 50); ans = a - b; qq = f"Quick maths: what is {a} minus {b}?"; ex = f"{a} minus {b} is {ans}."
    wrong = set()
    while len(wrong) < 3:
        d = ans + rnd.choice([-20, -10, -5, -3, -2, -1, 1, 2, 3, 5, 10, 20])
        if d > 0 and d != ans: wrong.add(d)
    return dict(type="math", cat="general", difficulty="easy", icon="🧮", hook=rnd.choice(["Quick maths!", "Beat the clock!", "No calculator!"]),
        question=qq, options=[str(ans)] + [str(x) for x in wrong], answer=0, explain=ex, title="Can you solve this in 5 seconds?", tags=["maths", "quiz", "brain teaser"])

def backup_quiz():
    """If every AI is unavailable the bot MAKES a fresh quiz itself (flags, capitals or maths), never a saved one, never a repeat."""
    if LANG != "en": return None
    rnd = random.Random(); used = {(h["hook"], h.get("emoji", "")) for h in HIST}
    kinds = ["flag", "capital", "math"]
    for i in range(3):
        for _ in range(60):
            raw = proc_one(kinds[(NEXT_NUM + i) % 3], rnd)
            q = clean(raw)
            if not q or (q["question"], q["emoji"]) in used: continue
            if q["emoji"] and not puzzle_images(q["emoji"]): continue
            if q["icon"] and not puzzle_images(q["icon"]): q["icon"] = ""
            return q
    return None

# ---------------------------------------------------------------- video rendering (pro look)
import shutil
from PIL import ImageFilter, ImageOps
FPS = 20
CX, LEFT, RIGHT, SAFE_BOTTOM = 500, 70, 930, 1470   # stay clear of the Shorts buttons (right) and caption bar (bottom)
PAD_SHADOW = 48
FONT_BASE = "https://raw.githubusercontent.com/google/fonts/main/ofl/poppins/"
FONT_FILES = {"xb": "Poppins-ExtraBold.ttf", "b": "Poppins-Bold.ttf", "sb": "Poppins-SemiBold.ttf"}
FALLBACK = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONTS = {}
GREEN, CORAL, WHITE = (46, 204, 113), (255, 99, 99), (255, 255, 255)
THEMES = {"football": ((6, 52, 38), (14, 112, 70), (255, 226, 102)), "geography": ((8, 40, 86), (18, 106, 160), (255, 208, 70)),
 "science": ((30, 16, 84), (88, 52, 176), (110, 255, 205)), "minecraft": ((30, 24, 16), (62, 108, 40), (255, 196, 70)),
 "roblox": ((60, 8, 20), (150, 28, 44), (255, 214, 90)), "anime": ((58, 16, 84), (168, 48, 132), (255, 220, 240)),
 "movies": ((24, 8, 12), (98, 18, 30), (255, 196, 60)), "history": ((52, 34, 12), (138, 92, 38), (255, 232, 160)),
 "animals": ((12, 54, 36), (66, 128, 58), (255, 222, 110)), "african": ((62, 26, 8), (168, 78, 18), (255, 226, 84))}
DEFAULT = ((14, 32, 56), (60, 84, 140), (255, 208, 63))

def theme(cat):
    for k, v in THEMES.items():
        if k in cat: return v
    return DEFAULT

def get_fonts():
    os.makedirs("fonts", exist_ok=True)
    for k, name in FONT_FILES.items():
        p = f"fonts/{name}"
        if not os.path.exists(p) or os.path.getsize(p) < 50000:
            try: urllib.request.urlretrieve(FONT_BASE + name, p)
            except Exception as e: print("font download failed, using fallback:", e)
        FONTS[k] = p if os.path.exists(p) and os.path.getsize(p) > 50000 else FALLBACK

@lru_cache(None)
def font(weight, size): return ImageFont.truetype(FONTS[weight], size)

_d = ImageDraw.Draw(Image.new("RGB", (4, 4)))
def tw(text, f): return _d.textlength(text, font=f)

def wrap_text(text, f, maxw):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if tw(t, f) <= maxw: cur = t
        else:
            if cur: lines.append(cur)
            cur = w
    return lines + ([cur] if cur else [])

def fit_font(weight, size, text, maxw):
    while size > 24 and tw(text, font(weight, size)) > maxw: size -= 2
    return font(weight, size)

def ease(x): return 1 - (1 - x) ** 3

_bg = {}
def bg_base(th):
    if th not in _bg:
        g = Image.linear_gradient("L").resize((W, H))
        base = ImageOps.colorize(g, th[0], th[1]).convert("RGB")
        dots = Image.new("RGBA", (W, H), (0, 0, 0, 0)); dd = ImageDraw.Draw(dots)
        for y in range(60, H, 96):
            for x in range(60, W, 96): dd.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(255, 255, 255, 24))
        base.paste(dots, (0, 0), dots)
        rg = Image.radial_gradient("L")
        glow = Image.new("RGBA", (1000, 1000), (*th[2], 0))
        glow.putalpha(rg.resize((1000, 1000)).point(lambda v: int(max(0.0, 1 - v / 179) ** 1.6 * 255 * 0.30)))
        vig = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        vig.putalpha(rg.resize((W, H)).point(lambda v: int(min(255, max(0.0, v / 179 - 0.6) * 210))))
        _bg[th] = (base, glow, vig)
    return _bg[th]

def background(th, t):
    base, glow, vig = bg_base(th)
    c = base.copy()
    for bx, by, ph in ((150, 420, 0.0), (850, 1400, 2.1)):
        c.paste(glow, (bx - 500 + int(70 * math.sin(t * 0.5 + ph)), by - 500 + int(60 * math.cos(t * 0.4 + ph))), glow)
    c.paste(vig, (0, 0), vig)
    return c

def put(c, spr, x, y, a=1.0):
    if a <= 0.01: return
    if a < 0.99:
        spr = spr.copy(); spr.putalpha(spr.getchannel("A").point(lambda v, a=a: int(v * a)))
    c.paste(spr, (x, y), spr)

def pill(w, h, r, fill, outline, ow=2):
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle([0, 0, w - 1, h - 1], r, fill=fill, outline=outline, width=ow)
    return im

def with_shadow(card, blur=16, off=10, alpha=120):
    pad = PAD_SHADOW
    out = Image.new("RGBA", (card.width + 2 * pad, card.height + 2 * pad), (0, 0, 0, 0))
    sh = Image.new("RGBA", out.size, (0, 0, 0, 0))
    sh.paste((0, 0, 0, alpha), (pad, pad + off, pad + card.width, pad + off + card.height), card.getchannel("A"))
    out.alpha_composite(sh.filter(ImageFilter.GaussianBlur(blur)))
    out.alpha_composite(card, (pad, pad))
    return out

def text_layer(lines, f, fill, line_h, w):
    h = line_h * len(lines) + 16
    lay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sh = Image.new("RGBA", (w, h), (0, 0, 0, 0)); sd = ImageDraw.Draw(sh); d = ImageDraw.Draw(lay)
    for i, l in enumerate(lines): sd.text((w // 2, 8 + i * line_h + 5), l, font=f, fill=(0, 0, 0, 150), anchor="mt")
    lay.alpha_composite(sh.filter(ImageFilter.GaussianBlur(5)))
    for i, l in enumerate(lines): d.text((w // 2, 8 + i * line_h), l, font=f, fill=fill, anchor="mt")
    return lay

def option_sprite(L, k, text, state, acc):
    w, h = RIGHT - LEFT, L["OH"]
    r = h // 2 if h < 150 else 44
    if state == "correct": fill, outline, ow = (*GREEN, 255), (255, 255, 255, 235), 4
    elif state == "active": fill, outline, ow = (*acc, 70), (*acc, 255), 5
    else: fill, outline, ow = (8, 16, 36, 135), (255, 255, 255, 85), 2
    card = pill(w, h, r, fill, outline, ow); d = ImageDraw.Draw(card)
    if L["letters"]:
        bd, bx, by = h - 30, 15, 15
        d.ellipse([bx, by, bx + bd, by + bd], fill=(255, 255, 255, 255) if state == "correct" else (*acc, 255))
        if state == "correct":
            cx_, cy_ = bx + bd / 2, by + bd / 2
            d.line([(cx_ - bd * .2, cy_), (cx_ - bd * .04, cy_ + bd * .16), (cx_ + bd * .22, cy_ - bd * .16)], fill=GREEN, width=9, joint="curve")
        else:
            d.text((bx + bd // 2, by + bd // 2 + 2), LET[k], font=font("xb", int(bd * 0.52)), fill=(10, 20, 40), anchor="mm")
        tx0 = bx + bd + 28
        d.text((tx0, h // 2 + 2), text, font=fit_font("sb", 50, text, w - tx0 - 34), fill=WHITE, anchor="lm")
    else:
        d.text((w // 2, h // 2 + 2), text, font=fit_font("xb", 64, text, w - 80), fill=WHITE, anchor="mm")
    return with_shadow(card)

def chip_sprite(text, acc):
    f = font("b", 40); w = int(tw(text, f)) + 90
    card = pill(w, 78, 39, (8, 16, 36, 150), (*acc, 255), 3)
    ImageDraw.Draw(card).text((w // 2, 40), text, font=f, fill=acc, anchor="mm")
    return with_shadow(card, blur=10, off=6, alpha=90)

def ease_back(x):
    x = min(1.0, max(0.0, x)); c1 = 1.70158; c3 = c1 + 1
    return 1 + c3 * (x - 1) ** 3 + c1 * (x - 1) ** 2

def ring_sprite(frac, num, color, S=210):
    k = 3; im = Image.new("RGBA", (S * k, S * k), (0, 0, 0, 0)); d = ImageDraw.Draw(im); m, wd = 14 * k, 22 * k
    d.ellipse([m, m, S * k - m, S * k - m], outline=(255, 255, 255, 70), width=wd)
    if frac > 0.003: d.arc([m, m, S * k - m, S * k - m], -90, -90 + 360 * frac, fill=(*color, 255), width=wd)
    d.text((S * k // 2, S * k // 2 + 6 * k), str(num), font=font("xb", int(S * 0.52) * k), fill=WHITE, anchor="mm")
    return im.resize((S, S), Image.LANCZOS)

def expl_sprite(q, h):
    w = RIGHT - LEFT
    card = pill(w, h, 36, (8, 16, 36, 175), (*GREEN, 255), 3); d = ImageDraw.Draw(card)
    ans = f"{LET[q['answer']]}.  {q['options'][q['answer']]}" if len(q["options"]) == 4 else q["options"][q["answer"]]
    d.ellipse([26, 24, 78, 76], fill=(*GREEN, 255))
    d.line([(38, 50), (49, 61), (67, 38)], fill=WHITE, width=7, joint="curve")
    d.text((98, 50), ans, font=fit_font("xb", 46, ans, w - 130), fill=GREEN, anchor="lm")
    f = font("sb", 34); lines = wrap_text(q["explain"], f, w - 60)[:3 if h >= 235 else 2 if h >= 190 else 1]
    for i, l in enumerate(lines): d.text((30, 112 + i * 46), l, font=f, fill=WHITE, anchor="lm")
    return with_shadow(card)

def make_confetti(acc):
    rnd = random.Random(7); cols = [acc, (92, 200, 255), GREEN, CORAL, WHITE, (255, 210, 63)]
    return [dict(x=rnd.uniform(40, 1040), vx=rnd.uniform(-80, 80), vy=rnd.uniform(200, 620), s=rnd.uniform(12, 26),
                 r0=rnd.uniform(0, 6.28), spin=rnd.uniform(-8, 8), col=rnd.choice(cols)) for _ in range(80)]

def draw_confetti(c, parts, tr):
    if tr > 2.6: return
    d = ImageDraw.Draw(c)
    for p in parts:
        y = -80 + p["vy"] * tr + 260 * tr * tr; x = p["x"] + p["vx"] * tr
        if y > H: continue
        a = p["r0"] + p["spin"] * tr; s = p["s"]
        pts = [(x + s * math.cos(a + o) * m, y + s * 0.6 * math.sin(a + o) * m) for o, m in ((0.8, 1), (2.4, 1), (3.9, 1), (5.5, 1))]
        d.polygon(pts, fill=p["col"])

# ---------------------------------------------------------------- emoji pictures (Twemoji, free) + sound design
EMOJI_BASE = "https://raw.githubusercontent.com/jdecked/twemoji/main/assets/72x72/"
SR = 24000

def emoji_clusters(s):
    s = [c for c in s if not c.isspace()]; out = []; i = 0
    while i < len(s):
        cl = [s[i]]; i += 1
        if 0x1F1E6 <= ord(cl[0]) <= 0x1F1FF and i < len(s) and 0x1F1E6 <= ord(s[i]) <= 0x1F1FF:
            cl.append(s[i]); i += 1
        while i < len(s):
            if s[i] == "\u200d" and i + 1 < len(s): cl += [s[i], s[i + 1]]; i += 2
            elif s[i] == "\ufe0f" or 0x1F3FB <= ord(s[i]) <= 0x1F3FF: cl.append(s[i]); i += 1
            else: break
        out.append("".join(cl))
    return out

def emoji_file(cluster):
    os.makedirs("emoji_cache", exist_ok=True)
    cps = [f"{ord(c):x}" for c in cluster]
    for name in dict.fromkeys(["-".join(x for x in cps if x != "fe0f"), "-".join(cps)]):
        p = f"emoji_cache/{name}.png"
        if os.path.exists(p) and os.path.getsize(p) > 100: return p
        try:
            urllib.request.urlretrieve(EMOJI_BASE + name + ".png", p); return p
        except Exception: continue
    return None

def puzzle_images(emoji):
    out = []
    for cl in emoji_clusters(emoji)[:6]:
        p = emoji_file(cl)
        if not p: return None
        out.append(Image.open(p).convert("RGBA"))
    return out or None

def write_wav(path, samples):
    import wave, array
    a = array.array("h", (int(max(-1.0, min(1.0, s)) * 32000) for s in samples))
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(a.tobytes())

def synth_sfx():
    pop = [0.7 * math.sin(2 * math.pi * (520 * (i / SR) + 1200 * (i / SR) ** 2)) * math.exp(-28 * i / SR) for i in range(int(SR * 0.14))]
    write_wav("pop.wav", pop)
    chime = [0.0] * int(SR * 0.8)
    for t0, f in ((0.0, 1046.5), (0.11, 1318.5), (0.22, 1568.0)):
        for i in range(int(t0 * SR), len(chime)):
            t = i / SR - t0
            chime[i] += 0.32 * (math.sin(2 * math.pi * f * t) + 0.3 * math.sin(4 * math.pi * f * t)) * math.exp(-6 * t)
    write_wav("chime.wav", chime)
    rnd = random.Random(5); n = int(SR * 0.4); y = 0.0; wh = []
    for i in range(n):
        x = i / n; a = 0.04 + 0.5 * x  # filter opens as the sweep rises
        y += a * (rnd.uniform(-1, 1) - y); wh.append(0.9 * y * math.sin(math.pi * x) ** 1.5)
    write_wav("whoosh.wav", wh)

def synth_music(total):
    rnd = random.Random(3); bpm = 112; beat = 60 / bpm; loop_beats = 16
    chords = [(110.0, (440, 523.25, 659.25, 783.99)), (87.31, (349.23, 440, 523.25, 698.46)),
              (130.81, (523.25, 659.25, 783.99, 1046.5)), (98.0, (392, 493.88, 587.33, 783.99))]
    loop_n = int(SR * beat * loop_beats); buf = [0.0] * loop_n
    def add(start, samples):
        s0 = int(start * SR)
        for i, v in enumerate(samples):
            if s0 + i < loop_n: buf[s0 + i] += v
    kick = [0.9 * math.sin(2 * math.pi * (48 * (i / SR) + 82 * (1 - math.exp(-35 * i / SR)) / 35)) * math.exp(-9 * i / SR) for i in range(int(SR * 0.22))]
    snare = [(0.35 * rnd.uniform(-1, 1) + 0.2 * math.sin(2 * math.pi * 190 * i / SR)) * math.exp(-22 * i / SR) for i in range(int(SR * 0.15))]
    prev = 0.0; hat = []
    for i in range(int(SR * 0.05)):
        x = rnd.uniform(-1, 1); hat.append(0.12 * (x - prev) * math.exp(-90 * i / SR)); prev = x
    for bar, (root, notes) in enumerate(chords):
        b0 = bar * 4 * beat
        for bt in (0, 2): add(b0 + bt * beat, kick)
        for bt in (1, 3): add(b0 + bt * beat, snare)
        for e in range(8): add(b0 + (e + 0.5) * beat * 0.5 + (0 if e % 2 else 0), hat) if e % 2 == 1 else None
        for bt in (0, 1.5, 2, 3.25):
            add(b0 + bt * beat, [0.5 * (math.sin(2 * math.pi * root * i / SR) + 0.25 * math.sin(4 * math.pi * root * i / SR)) * math.exp(-4 * i / SR) for i in range(int(SR * 0.45))])
        for e, idx in enumerate((0, 2, 1, 3, 2, 3, 1, 2)):
            f = notes[idx]
            add(b0 + e * beat * 0.5, [0.16 * (math.sin(2 * math.pi * f * i / SR) + 0.4 * math.sin(4 * math.pi * f * i / SR)) * math.exp(-9 * i / SR) for i in range(int(SR * 0.3))])
    peak = max(abs(v) for v in buf) or 1.0
    buf = [0.85 * v / peak for v in buf]
    n = int(SR * (total + 1)); write_wav("music.wav", [buf[i % loop_n] for i in range(n)])

def tts(text, path):
    if os.environ.get("TEST_TTS"):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=300:duration={max(0.8, len(text) * 0.055):.2f}:sample_rate=24000", "-f", "wav", path], check=True); return
    subprocess.run(["edge-tts", "--voice", VOICE, "--rate=+8%", "--text", text, "--write-media", path], check=True)

def dur(path):
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path]).decode().strip())

def sh(*a): subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *a], check=True)

def scaled(spr, s):
    return spr.resize((max(1, int(spr.width * s)), max(1, int(spr.height * s))), Image.LANCZOS)

def make_video(q, num):
    get_fonts(); shutil.rmtree("frames", ignore_errors=True); os.makedirs("frames"); os.makedirs("out", exist_ok=True)
    th = theme(q["cat"]); acc = th[2]; opts = q["options"]; n = len(opts); letters = q["type"] != "true_false"
    rnd = random.Random(num); music_on = CFG.get("music", True)
    # ---- voice timeline (every visual below is timed from these real durations)
    PAD = 0.2; clips = []; times = {}; t = 0.0
    def say(name, text, pad=PAD):
        tts(text, name); clips.append((name, 0, pad)); return dur(name) + pad
    if q["hook"]:
        d_ = say("hook.mp3", q["hook"]); times["hook"] = (t, t + d_); t += d_
    d_ = say("q.mp3", q["question"]); times["q"] = (t, t + d_); t += d_
    times["opt"] = []
    for k, o in enumerate(opts):
        d_ = say(f"o{k}.mp3", f"{LET[k]}, {o}." if letters else o + "."); times["opt"].append((t, t + d_)); t += d_
    d_ = say("c.mp3", rnd.choice(T["comment"])); times["comment"] = (t, t + d_); t += d_
    sh("-f", "lavfi", "-i", "sine=frequency=1500:duration=0.05:sample_rate=24000", "-af", "apad=whole_dur=1,volume=0.45", "tick.wav")
    clips.append(("tick.wav", 4, 0)); times["count"] = (t, t + 5); t += 5
    synth_sfx(); clips.append(("chime.wav", 0, 0))
    tts(T["answer"].format(L=LET[q["answer"]], opt=opts[q["answer"]]) + " " + q["explain"] + " " + rnd.choice(T["outro"]), "a.mp3")
    clips.append(("a.mp3", 0, 0.5)); times["reveal"] = (t, t + 0.8 + dur("a.mp3") + 0.5); END = times["reveal"][1]
    cmd, fl = [], []
    for i, (p, loops, pad) in enumerate(clips):
        if loops: cmd += ["-stream_loop", str(loops)]
        cmd += ["-i", p]
        fl.append(f"[{i}:a]aresample=24000,aformat=channel_layouts=mono" + (f",apad=pad_dur={pad}" if pad else "") + f"[a{i}]")
    fl.append("".join(f"[a{i}]" for i in range(len(clips))) + f"concat=n={len(clips)}:v=0:a=1[out]")
    sh(*cmd, "-filter_complex", ";".join(fl), "-map", "[out]", "voice.wav")
    # ---- sound effects + music bed mixed under the voice
    pops = [max(0.05, times["opt"][k][0] - 0.2) for k in range(n)]
    ins = ["-i", "voice.wav", "-i", "pop.wav", "-i", "whoosh.wav"]; fm = []; labels = ["[0:a]"]
    fm.append(f"[1:a]asplit={n}" + "".join(f"[p{k}]" for k in range(n)))
    for k in range(n):
        fm.append(f"[p{k}]adelay={int(pops[k] * 1000)}:all=1,volume=0.5[d{k}]"); labels.append(f"[d{k}]")
    fm.append(f"[2:a]adelay={int(times['q'][0] * 1000)}:all=1,volume=0.45[wh]"); labels.append("[wh]")
    if music_on:
        synth_music(END); ins += ["-i", "music.wav"]
        fm.append(f"[3:a]volume=0.13,atrim=0:{END:.2f},afade=t=in:d=0.6,afade=t=out:st={max(0, END - 1.2):.2f}:d=1.2[mu]"); labels.append("[mu]")
    fm.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0:duration=first,alimiter=limit=0.95[mix]")
    sh(*ins, "-filter_complex", ";".join(fm), "-map", "[mix]", "mix.wav")
    # ---- layout, kept inside the Shorts safe area
    puz = puzzle_images(q["emoji"]) if q.get("emoji") else None
    icon_imgs = puzzle_images(q["icon"]) if (not puz and q.get("icon")) else None
    for qs in (74, 64, 56, 50, 44):
        qf = font("xb", qs); qlines = wrap_text(q["question"], qf, 860)
        if len(qlines) <= (3 if qs > 56 else 4): break
    qlines = qlines[:5]; LH = int(qs * 1.25); qh = LH * len(qlines) + 16
    ICON_S, ICON_CY = 150, 350
    Q_Y = 450 if icon_imgs else 320
    PUZ_Y, PUZ_H, puz_sprites = Q_Y + qh + 6, 0, []
    if puz:
        S = min(190, int(820 / len(puz))); gap = 18; tot = len(puz) * S + (len(puz) - 1) * gap; PUZ_H = S + 30
        for i, im in enumerate(puz):
            e = im.resize((S, S), Image.LANCZOS); sp = Image.new("RGBA", (S + 2 * PAD_SHADOW, S + 2 * PAD_SHADOW), (0, 0, 0, 0))
            shl = Image.new("RGBA", sp.size, (0, 0, 0, 0)); shl.paste((0, 0, 0, 110), (PAD_SHADOW + 4, PAD_SHADOW + 10, PAD_SHADOW + 4 + S, PAD_SHADOW + 10 + S), e.getchannel("A"))
            sp.alpha_composite(shl.filter(ImageFilter.GaussianBlur(12))); sp.alpha_composite(e, (PAD_SHADOW, PAD_SHADOW))
            puz_sprites.append((sp, CX - tot // 2 + i * (S + gap) + S // 2))
    OPT_Y = (PUZ_Y + PUZ_H + 40) if puz else Q_Y + qh + 50
    if n == 4:
        for OH in ((96,) if puz else (118, 108, 100, 92)):
            GAP = int(OH * 0.22)
            if OPT_Y + 4 * OH + 3 * GAP <= 1250: break
    else: OH, GAP = 190, 40
    L = {"OH": OH, "letters": letters}
    OPT_END = OPT_Y + n * OH + (n - 1) * GAP; TY = min((OPT_END + SAFE_BOTTOM) // 2, SAFE_BOTTOM - 105)
    # question words pop in one by one, lit up as the voice reads them
    allw = [w for ln in qlines for w in ln.split()]; total_chars = sum(len(w) + 1 for w in allw) or 1
    q0, q1 = times["q"]; span = max(0.6, (q1 - PAD - q0) * 0.9); cum = 0; qwords = []
    for li, line in enumerate(qlines):
        x = (860 - tw(line, qf)) / 2
        for w in line.split():
            ww = int(tw(w, qf)) + 40
            qwords.append((text_layer([w], qf, WHITE, LH, ww), text_layer([w], qf, acc, LH, ww), LEFT + x - 20, Q_Y + li * LH, q0 + 0.1 + span * cum / total_chars))
            x += tw(w + " ", qf); cum += len(w) + 1
    icon_sprite = None
    if icon_imgs:
        e = icon_imgs[0].resize((ICON_S, ICON_S), Image.LANCZOS); gd = 300; o = (gd - ICON_S) // 2
        gl = Image.new("RGBA", (gd, gd), (*acc, 0)); gl.putalpha(Image.radial_gradient("L").resize((gd, gd)).point(lambda v: int(max(0.0, 1 - v / 179) ** 1.3 * 255 * 0.55)))
        icon_sprite = Image.new("RGBA", (gd, gd), (0, 0, 0, 0)); icon_sprite.alpha_composite(gl)
        shl = Image.new("RGBA", (gd, gd), (0, 0, 0, 0)); shl.paste((0, 0, 0, 110), (o + 4, o + 12, o + 4 + ICON_S, o + 12 + ICON_S), e.getchannel("A"))
        icon_sprite.alpha_composite(shl.filter(ImageFilter.GaussianBlur(10))); icon_sprite.alpha_composite(e, (o, o))
    rb = random.Random(11); bubbles = []
    for _ in range(14):
        d_ = rb.randint(30, 90); bb = Image.new("RGBA", (d_, d_), (0, 0, 0, 0))
        ImageDraw.Draw(bb).ellipse([1, 1, d_ - 2, d_ - 2], fill=(255, 255, 255, 24), outline=(255, 255, 255, 70), width=2)
        bubbles.append((bb, rb.uniform(40, 1000), rb.uniform(25, 70), rb.uniform(0, 6.28), rb.uniform(0, H + 200)))
    tag = ["BOSS LEVEL"] if q.get("boss") else ([q["difficulty"].upper()] if q.get("difficulty") else [])
    chip = chip_sprite("  •  ".join([q["cat"].upper()] + tag), CORAL if q.get("boss") else acc)
    hook_lines = wrap_text(q["hook"].upper(), font("xb", 104), 860)[:3] if q["hook"] else []
    hook_spr = text_layer(hook_lines, font("xb", 104), acc, 128, 860) if hook_lines else None
    osp = {(k, s): option_sprite(L, k, o, s, acc) for k, o in enumerate(opts) for s in ("normal", "active", "correct")}
    expl = expl_sprite(q, max(150, min(230, SAFE_BOTTOM - (OPT_END + 34))))
    conf = make_confetti(acc)

    def render(t):
        c = background(th, t)
        for bb, bx, sp_, ph, off in bubbles:
            put(c, bb, int(bx + 25 * math.sin(t * 0.8 + ph)), int(H + 100 - ((t * sp_ + off) % (H + 200))))
        put(c, chip, CX - chip.width // 2, 200 - PAD_SHADOW)
        if hook_spr and t < times["hook"][1]:
            a = ease(min(1, t / 0.3)); put(c, hook_spr, CX - hook_spr.width // 2, 640 + int(30 * (1 - a)), a); return c
        if icon_sprite:
            ti = t - times["q"][0] + 0.05
            if ti > 0:
                s_ = (0.3 + 0.7 * ease_back(ti / 0.5)) * (1 + 0.04 * math.sin(t * 3.1)); sc = scaled(icon_sprite, s_)
                put(c, sc, CX - sc.width // 2, ICON_CY + int(8 * math.sin(t * 2.6)) - sc.height // 2, min(1, ti / 0.25))
        for w_white, w_acc, wx, wy, ts in qwords:
            if t < ts: continue
            u = (t - ts) / 0.3; sp_ = w_acc if t < ts + 0.38 else w_white
            sc = scaled(sp_, 0.8 + 0.2 * ease_back(u)) if u < 1 else sp_
            put(c, sc, int(wx - (sc.width - sp_.width) / 2), int(wy - (sc.height - sp_.height) / 2), min(1.0, u * 2))
        for i, (sp, cx_) in enumerate(puz_sprites):
            tp = times["q"][0] + 0.35 + 0.12 * i
            if t < tp: continue
            s = 0.3 + 0.7 * ease_back((t - tp) / 0.45); sc = scaled(sp, s)
            put(c, sc, cx_ - sc.width // 2, PUZ_Y + (PUZ_H - 34) // 2 - sc.height // 2 + 17, min(1, (t - tp) / 0.2))
        rv = t >= times["reveal"][0]
        for k in range(n):
            s0, e0 = times["opt"][k][0], times["opt"][k][1] - PAD
            ta = t - (s0 - 0.25)
            if ta < 0: continue
            ab = ease_back(min(1, ta / 0.4)); a = min(1.0, ease(min(1, ta / 0.3))); y = OPT_Y + k * (OH + GAP)
            state = "normal"
            if rv: state = "correct" if k == q["answer"] else "normal"
            elif s0 <= t < e0: state = "active"
            al = a; spr = osp[(k, state)]; x = LEFT - PAD_SHADOW + int(90 * (1 - ab)); yy = y - PAD_SHADOW
            if rv and k != q["answer"]: al = a * (1 - 0.7 * ease(min(1, (t - times["reveal"][0]) / 0.4)))
            if rv and k == q["answer"] and t - times["reveal"][0] < 0.5:
                s = 1 + 0.07 * math.sin(math.pi * (t - times["reveal"][0]) / 0.5); big = scaled(spr, s)
                x -= (big.width - spr.width) // 2; yy -= (big.height - spr.height) // 2; spr = big
            put(c, spr, x, yy, al)
        if t >= times["comment"][0] and not rv:
            if t < times["count"][0]: rem, el = 5.0, 0.0
            else: el = t - times["count"][0]; rem = max(0.0, 5 - el)
            col = (92, 200, 255) if rem > 3 else (255, 190, 50) if rem > 1.5 else CORAL
            pulse = max(0.0, 1 - (el % 1) / 0.25) if el > 0 else 0.0
            ring = ring_sprite(rem / 5, max(1, math.ceil(rem - 1e-6)), col, int(210 * (1 + 0.10 * pulse)))
            put(c, ring, CX - ring.width // 2, TY - ring.height // 2, ease(min(1, (t - times["comment"][0]) / 0.3)))
        if rv:
            tr = t - times["reveal"][0]; a = ease(min(1, tr / 0.45))
            put(c, expl, CX - expl.width // 2, OPT_END + 34 - PAD_SHADOW + int(40 * (1 - a)), a)
            draw_confetti(c, conf, tr)
        return c

    for i in range(int(END * FPS)): render(i / FPS).save(f"frames/f{i:05d}.jpg", quality=92)
    name = f"out/short_{num}.mp4"
    sh("-framerate", str(FPS), "-i", "frames/f%05d.jpg", "-i", "mix.wav", "-vf", "fps=30,format=yuv420p", "-c:v", "libx264",
       "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", name)
    return name

# ---------------------------------------------------------------- main
if __name__ == "__main__":
    sync_channel()
    market_intel()
    maybe_update_playbook()
    q = pick_quiz()
    if not q:
        print("AI quiz unavailable or failed the fact-check; the bot is making a flag, capital or maths quiz itself.")
        q = backup_quiz()
    if not q:
        print("No quiz available this run. Skipping."); write_report(); sys.exit(0)
    num = NEXT_NUM
    q["boss"] = num % 5 == 0 and q.get("difficulty") == "hard"
    name = make_video(q, num)
    title = q["title"] or ("Can you get this right? " + q["question"])[:90]
    meta = {"title": title, "tags": q["tags"] or ["quiz", "trivia"],
            "description": f"{title}\n\nNew quiz every {HOURS} hours. #shorts #quiz" + (f"\n\nPlay free games: {CFG['link']}" if CFG.get("link") else "") + f"\n\n[q{num}]", "language": LANG}
    json.dump(meta, open("out/meta.json", "w"))
    open("out/title.txt", "w").write(title)
    HIST.append({"n": num, "hook": q["question"], "type": q["type"], "emoji": q.get("emoji", ""), "format": f"{q['type']}/{q['cat']}",
                 "difficulty": q.get("difficulty", ""), "title": title, "id": None})
    save_hist()
    write_report()
    print("made", name)
