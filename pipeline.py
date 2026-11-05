#!/usr/bin/env python3
"""
DateFlow Auto-Pilot (MoneyPrinterTurbo edition)
=================================================
0-click daily Reels pipeline for @dataflow_planner.
Generates N videos per day and schedules them across optimal posting slots.

Posting slots (Europe/Istanbul = UTC+3): 11:00, 15:00, 18:15, 21:30
(edit POST_TIMES below to change schedule).
"""
import os, sys, json, subprocess, random, re, shutil, tempfile, time
from pathlib import Path
from datetime import datetime, timedelta, timezone
import requests

# ── Paths & config ──────────────────────────────────────────────────────────
ROOT = Path(__file__).parent
OUT = ROOT / "output"
OUT.mkdir(exist_ok=True)
MPT_DIR = Path(os.environ.get("MPT_DIR", ROOT.parent / "mpt"))

# ── Scheduling config ────────────────────────────────────────────────────────
# Слоты публикации (Europe/Istanbul = UTC+3). Можно добавить/убрать время.
# Формат "HH:MM".
POST_TIMES = ["11:00", "15:00", "18:15", "21:30"]

# Количество постов за один запуск (Buffer free план: максимум 10 в очереди)
NUM_POSTS = int(os.environ.get("NUM_POSTS", len(POST_TIMES)))

UTC = timezone.utc
TRT = timezone(timedelta(hours=3), name="TRT")
def trt_now():
    return datetime.now(UTC).astimezone(TRT)

def get_scheduled_times():
    """Получить список datetime (UTC) уже запланированных постов в Buffer,
    чтобы избежать дубликатов."""
    times = set()
    try:
        if not (BUFFER_KEY and ORG_ID):
            return times
        env = os.environ.copy(); env["BUFFER_API_KEY"] = BUFFER_KEY
        r = subprocess.run(
            ["buffer", "posts", "list",
             "--organization-id", ORG_ID,
             "--limit", "50"],
            capture_output=True, text=True, timeout=30, env=env)
        if r.returncode != 0:
            return times
        data = json.loads(r.stdout)
        items = data.get("items", []) or data.get("data", {}).get("posts", {}).get("posts", [])
        for p in items:
            if p.get("status") == "scheduled":
                due = p.get("dueAt")
                if due:
                    try:
                        # dueAt в ISO UTC формате
                        dt = datetime.fromisoformat(due.replace("Z", "+00:00"))
                        times.add(dt.astimezone(UTC))
                    except Exception:
                        pass
    except Exception as e:
        print(f"  ⚠️ Could not fetch existing scheduled posts: {e}")
    return times

def next_free_slots(count):
    """Вернуть список datetime (timezone-aware, в TRT) для следующих count постов,
    пропуская слоты которые уже заняты (избегаем дубликатов)."""
    now = trt_now()
    existing_utc = get_scheduled_times()
    print(f"  📋 Existing scheduled posts in Buffer: {len(existing_utc)}")
    slots = []
    day = now.date()
    while len(slots) < count:
        for hhmm in POST_TIMES:
            h, m = map(int, hhmm.split(":"))
            t_local = datetime(day.year, day.month, day.day, h, m, tzinfo=TRT)
            if t_local <= now + timedelta(minutes=10):
                continue
            # Проверяем не занят ли этот слот (±45 минут от него)
            t_utc = t_local.astimezone(UTC)
            occupied = any(abs((t - t_utc).total_seconds()) < 2700 for t in existing_utc)
            if occupied:
                print(f"  ⏭ Slot {t_local.strftime('%a %H:%M')} already scheduled — skipping")
                continue
            slots.append(t_local)
            if len(slots) >= count:
                return slots
        day += timedelta(days=1)
    return slots

OPENAI_KEY   = os.getenv("OPENAI_API_KEY", "")
PEXELS_KEY   = os.getenv("PEXELS_API_KEY", "")
BUFFER_KEY   = os.getenv("BUFFER_API_KEY", "")
CHANNEL_ID   = os.getenv("BUFFER_CHANNEL_ID", "6adc7b9b778b240e2fac6af1")
ORG_ID       = os.getenv("BUFFER_ORG_ID", "6ad678fa27469e130f893e13")
TG_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "8795932131:AAGvoGY6DuROOSe4hxE_71rofDOvu4iDJJ0")
ADMIN_CHAT   = os.getenv("ADMIN_CHAT_ID", "7210384965")

# Viral fallback pool (used when DeepSeek is down)
SCRIPTS_POOL = [
  {"hook":"Never say \"whatever you want\"","body":"It doesn't sound chill. It sounds like you don't care. Make the decision. Pick the place. Name the time. People are attracted to certainty.","cta":"We plan the route. You just show up.","terms":"couple romantic dinner date,man deciding confident,couple restaurant night,happy couple date night,city lights couple walking","kws":["never","whatever","decision","certainty"]},
  {"hook":"First date cinema is a trap","body":"You sit next to each other for two hours in silence. You don't talk. You learn nothing. Coffee plus a walk instead.","cta":"We plan that exact combo for your city.","terms":"couple cinema movie date awkward,couple coffee date talking,happy couple walking city park,first date conversation,man woman coffee shop","kws":["cinema","first","trap","walk"]},
  {"hook":"Text this after a first date","body":"Don't overthink it. Say: I had fun, let's do this again. Two sentences. Leave her wanting more. Long paragraphs smell desperate.","cta":"Short. Confident. Direct.","terms":"woman texting phone smiling,man texting confident,romantic text message phone,couple goodnight text,phone screen dating app","kws":["text","after","first","date"]},
  {"hook":"Stop texting her every day","body":"It kills attraction faster than anything else. When you text all day you become predictable. Predictable is boring. Text once with a clear plan. Hey there's a new wine bar tomorrow 7pm. That's it.","cta":"Certainty is attractive. Neediness is not.","terms":"couple texting phone bored,woman ignoring phone,man confident dating,couple wine bar date,city night couple walking","kws":["every day","predictable","boring","plan"]},
  {"hook":"35 euro date that beats 200 euro dinner","body":"Sunset walk. Ice cream. One good cocktail. The best dates create moments not bills. Spending money doesn't create chemistry. Presence does.","cta":"We find the spots that feel expensive but aren't.","terms":"couple sunset walk beach,romantic ice cream date,couple cocktail bar night,sunset city view couple,cheap fun date ideas","kws":["35","200","moments","presence"]},
  {"hook":"Stop asking for permission","body":"Don't say can I kiss you. Don't say is it okay if I hold your hand. Read the room. Act. Confidence is reading the moment and going for it.","terms":"couple romantic first kiss,confident man holding hands woman,first kiss moment,couple romantic moment evening,close up couple eyes","kws":["permission","confidence","act","moment"]},
  {"hook":"The worst first date move","body":"Checking your phone every two minutes. It screams I'd rather be somewhere else. Put it away. Eye contact. Listen. That alone puts you in top ten percent.","cta":"Presence is the rarest quality on a date.","terms":"man checking phone date rude,woman upset phone date,man listening woman eye contact,couple deep conversation,date night phone distraction","kws":["phone","eye contact","listen","presence"]},
  {"hook":"Date spots that always work","body":"Rooftop with a view. Speakeasy bar. Bookstore coffee. River walk at sunset. Places that give you something to talk about beat the same restaurant every time.","cta":"We map these for every city you visit.","terms":"rooftop bar city view couple,speakeasy bar cocktail,bookstore coffee shop couple,river walk sunset couple,date location ideas","kws":["rooftop","speakeasy","sunset","map"]},
]


# ── 1. Script generation ────────────────────────────────────────────────────
_DS_POOL_BACKUP = list(SCRIPTS_POOL)

def gen_script_deepseek(avoid_hooks=()):
    if not OPENAI_KEY:
        return None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_KEY, base_url="https://api.deepseek.com")
        topics = [
            "first date conversation mistakes", "texting frequency that kills attraction",
            "body language signs she likes you", "cheap date ideas better than dinner",
            "where to take her on a first date", "how to ask her out confidently",
            "signs she's losing interest", "mistakes men make on second dates",
            "first kiss timing", "confidence vs arrogance on dates",
            "dating app profile mistakes", "red flags on first dates",
        ]
        topic = random.choice(topics)
        avoid_txt = ""
        if avoid_hooks:
            avoid_txt = "IMPORTANT: the hook must NOT be similar to any of these already-used hooks (use a completely different angle/topic):\n" + "\n".join(f"- {h}" for h in avoid_hooks) + "\n\n"
        sys_prompt = (
            "You write viral 18-25 second Instagram Reels scripts for a dating-advice account "
            "for men (age 22-35). Scripts must have: controversial/surprising hook as first sentence, "
            "1 clear actionable tip, no fluff, short punchy sentences, conversational American English, "
            "no emojis in the voiceover, ending with a 1-line CTA about DateFlow app that plans perfect date routes. "
            "Return ONLY valid JSON with fields: hook, body, cta, terms (comma-separated Pexels search terms, "
            "5 terms max, portrait-friendly stock video keywords). Do NOT wrap in markdown.\n\n" + avoid_txt
        )
        user_msg = f"Write ONE fresh Reel script about: {topic}. Make the hook shocking or counterintuitive."
        r = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role":"system","content":sys_prompt},
                      {"role":"user","content":user_msg}],
            temperature=1.0, max_tokens=400, timeout=45)
        raw = r.choices[0].message.content.strip()
        raw = re.sub(r"```(?:json)?","",raw).strip("` \n")
        s = json.loads(raw)
        for k in ("hook","body","cta","terms"):
            assert k in s and s[k], f"missing {k}"
        return s
    except Exception as e:
        print(f"  DeepSeek failed ({e}), using fallback pool")
        return None

def pick_script(avoid_hooks=()):
    s = gen_script_deepseek(avoid_hooks)
    if s and s["hook"] not in avoid_hooks:
        return s
    # Pick from pool, avoiding used hooks
    unused = [s for s in _DS_POOL_BACKUP if s["hook"] not in avoid_hooks]
    if unused:
        return random.choice(unused)
    return random.choice(_DS_POOL_BACKUP)


# ── 2. TTS (MPT handles this internally but we use its Edge TTS via CLI) ────
# MPT CLI generates TTS automatically, so no separate function needed.


# ── 3. Upload to public host (Buffer fetches from URL) ──────────────────────
def upload_github_release(path):
    """Загрузить MP4 как asset к GitHub Release.
    Требует env GITHUB_TOKEN и GITHUB_REPOSITORY (автоматически в Actions).
    Возвращает прямую ссылку на скачивание (живёт вечно)."""
    gh_token = os.environ.get("GITHUB_TOKEN")
    gh_repo = os.environ.get("GITHUB_REPOSITORY")  # e.g. "user/dateflow-pipeline"
    if not gh_token or not gh_repo:
        raise RuntimeError("GITHUB_TOKEN or GITHUB_REPOSITORY not set (not in GitHub Actions?)")
    headers = {"Authorization": f"token {gh_token}",
               "Accept": "application/vnd.github+json",
               "User-Agent": "DateFlow-Pipeline"}
    api = f"https://api.github.com/repos/{gh_repo}"
    tag = "dateflow-media"
    # 1. Get or create release
    r = requests.get(f"{api}/releases/tags/{tag}", headers=headers, timeout=30)
    if r.status_code == 404:
        r = requests.post(f"{api}/releases", headers=headers, timeout=30,
                          json={"tag_name": tag, "name": "DateFlow Reels Media",
                                "body": "Auto-generated Reels assets", "draft": False})
        r.raise_for_status()
    elif r.status_code != 200:
        r.raise_for_status()
    release = r.json()
    upload_url = release["upload_url"].replace("{?name,label}", "")
    asset_name = path.name
    # 2. Upload asset
    with open(path, "rb") as f:
        r2 = requests.post(f"{upload_url}?name={asset_name}",
                           headers={**headers, "Content-Type": "video/mp4"},
                           data=f, timeout=300)
    if r2.status_code not in (201, 200):
        # If asset already exists (duplicate name), delete old one first
        if r2.status_code == 422:
            for a in release.get("assets", []):
                if a["name"] == asset_name:
                    requests.delete(f"{api}/releases/assets/{a['id']}", headers=headers, timeout=15)
                    with open(path, "rb") as f:
                        r2 = requests.post(f"{upload_url}?name={asset_name}",
                                           headers={**headers, "Content-Type": "video/mp4"},
                                           data=f, timeout=300)
                    break
        r2.raise_for_status()
    asset = r2.json()
    url = asset["browser_download_url"]
    # Verify HEAD
    h = requests.head(url, timeout=20, allow_redirects=True)
    if h.status_code != 200:
        raise RuntimeError(f"GitHub asset not accessible: {h.status_code}")
    return url

def upload_uguu(path):
    """uguu.se — хранит файлы долго (~120 дней), прямая ссылка, полный файл без сжатия."""
    with open(path,"rb") as f:
        r = requests.post("https://uguu.se/upload",
                          files={"files[]":(path.name,f,"video/mp4")},
                          headers={"User-Agent":"Mozilla/5.0"},
                          timeout=300)
    r.raise_for_status()
    data = r.json()
    assert data.get("success"), f"uguu bad response: {data}"
    url = data["files"][0]["url"]
    assert url.startswith("http"), f"uguu url bad: {url[:300]}"
    # Verify size matches (no compression)
    expected = path.stat().st_size
    h = requests.head(url, timeout=20, allow_redirects=True, headers={"User-Agent":"Mozilla/5.0"})
    got = int(h.headers.get("content-length", 0))
    assert h.status_code == 200 and abs(got - expected) < expected * 0.05, \
        f"uguu size mismatch: expected {expected}, got {got}"
    return url

def upload_imgur(path):
    """Imgur анонимная загрузка — CDN, живёт вечно (но может пережимать короткие видео)."""
    with open(path, "rb") as f:
        r = requests.post("https://api.imgur.com/3/upload",
                          headers={"Authorization": "Client-ID 546c25a59c58ad7"},
                          data={"type":"file","title":"DateFlow Reel"},
                          files={"video":(path.name,f,"video/mp4")},
                          timeout=300)
    r.raise_for_status()
    data = r.json()
    assert data.get("success"), f"imgur upload failed: {data}"
    url = data["data"]["link"]
    assert url.endswith(".mp4"), f"not mp4: {url}"
    return url

def upload_litterbox(path, expiry="72h"):
    """litterbox.catbox.moe — 3 дня."""
    with open(path, "rb") as f:
        r = requests.post("https://litterbox.catbox.moe/resources/internals/api.php",
                          data={"reqtype":"fileupload","time":expiry},
                          files={"fileToUpload":(path.name,f,"video/mp4")},
                          headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                                   "Origin":"https://litterbox.catbox.moe",
                                   "Referer":"https://litterbox.catbox.moe/"},
                          timeout=300)
    url = r.text.strip()
    assert url.startswith("https://litter.catbox.moe/"), f"litterbox bad: {r.status_code} → {url[:300]}"
    h = requests.head(url, timeout=20, allow_redirects=True, headers={"User-Agent":"Mozilla/5.0"})
    assert h.status_code == 200, f"litterbox HEAD: {h.status_code}"
    return url

def _upload_with_retries(fn, path, tries=3, delay=8):
    last_err = None
    for i in range(tries):
        try:
            return fn(path)
        except Exception as e:
            last_err = e
            print(f"    attempt {i+1}/{tries} failed: {str(e)[:200]}")
            if i < tries - 1:
                time.sleep(delay)
    raise last_err

def upload_video(path):
    """Порядок хостов: uguu (полный файл, долго живёт) → GitHub Release → imgur → litterbox."""
    attempts = [("uguu", upload_uguu)]
    if os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOSITORY"):
        attempts.append(("github", upload_github_release))
    attempts.append(("imgur", upload_imgur))
    attempts.append(("litterbox", upload_litterbox))
    last_err = None
    for name, fn in attempts:
        try:
            url = _upload_with_retries(fn, path, tries=3, delay=8)
            print(f"  ✅ Upload ({name}): {url}")
            return url
        except Exception as e:
            last_err = e
            print(f"  ❌ {name} failed after retries: {str(e)[:200]}")
    raise RuntimeError(f"All uploaders failed. Last error: {last_err}")


# ── 4. Render via MoneyPrinterTurbo CLI ─────────────────────────────────────
def ensure_mpt_config():
    cfg = MPT_DIR / "config.toml"
    cfg.write_text(f"""log_level = "WARNING"
listen_host = "127.0.0.1"
listen_port = 8080
[app]
video_source = "pexels"
pexels_api_keys = ["{PEXELS_KEY}"]
llm_provider = "openai"
n_threads = 4
video_codec = "libx264"
subtitle_enabled = true
subtitle_display_mode = "word_by_word"
subtitle_animation = "pop_spring"
font_name = "Anton-Regular.ttf"
font_size = 78
text_fore_color = "#FFFFFF"
stroke_color = "#000000"
stroke_width = 3.5
subtitle_position = "two_thirds_bottom"
subtitle_background_enabled = false
video_transition_mode = "fade-in"
video_fit_mode = "cover"
bgm_type = "none"
video_aspect_pexels = "9:16"
video_clip_duration = 2.8
openai_api_key = "{OPENAI_KEY}"
openai_base_url = "https://api.deepseek.com"
openai_model_name = "deepseek-chat"
[whisper]
model_size = "base"
device = "cpu"
compute_type = "int8"
""", encoding="utf-8")

    # Ensure font exists
    fonts_dir = MPT_DIR / "resource" / "fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)
    font = fonts_dir / "Anton-Regular.ttf"
    if not font.exists():
        try:
            r = requests.get("https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf", timeout=30)
            font.write_bytes(r.content)
        except Exception:
            # Fallback: copy system bold font
            sys_font = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
            if sys_font.exists():
                shutil.copy(sys_font, font)

def render_mpt(script, terms):
    ensure_mpt_config()
    full_text = f"{script['hook']}. {script['body']} {script['cta']}"
    cmd = [
        sys.executable, "cli.py",
        "--video-script", full_text,
        "--video-terms", terms,
        "--video-language", "en-US",
        "--video-aspect", "9:16",
        "--video-source", "pexels",
        "--voice-name", "en-US-GuyNeural",
        "--voice-rate", "1.1",
        "--subtitle-display-mode", "word_by_word",
        "--subtitle-animation", "pop_spring",
        "--video-transition-mode", "fade-in",
        "--stroke-width", "3",
        "--font-size", "78",
        "--font-name", "Anton-Regular.ttf",
        "--bgm-type", "none",
        "--n-threads", "4",
    ]
    print(f"🎬 Rendering via MoneyPrinterTurbo...")
    r = subprocess.run(cmd, cwd=str(MPT_DIR), capture_output=True, text=True, timeout=600)
    # Parse JSON result from last line
    out_lines = [l for l in r.stdout.strip().splitlines() if l.strip().startswith("{")]
    if not out_lines:
        # Print logs for debugging
        print("STDERR tail:", r.stderr[-2000:])
        print("STDOUT tail:", r.stdout[-2000:])
        raise RuntimeError("MPT did not return a result JSON")
    try:
        result = json.loads(out_lines[-1])
        video_path = Path(result["result"]["videos"][0])
        print(f"✅ MPT render complete: {video_path.name} ({video_path.stat().st_size/1024/1024:.1f} MB)")
        return video_path
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print("STDOUT:", r.stdout[-1500:])
        print("STDERR:", r.stderr[-1500:])
        raise RuntimeError(f"MPT parse failed: {e}")


# ── 5. Schedule on Buffer via CLI ──────────────────────────────────────────
def schedule_buffer(video_url, caption, publish_trt):
    """publish_trt: timezone-aware datetime in TRT."""
    # dueAt в API — UTC
    due_at_utc = publish_trt.astimezone(UTC)
    due_at = due_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    post = {
        "channelId": CHANNEL_ID,
        "schedulingType": "automatic",
        "mode": "customScheduled",
        "dueAt": due_at,
        "text": caption,
        "assets": [{"video":{"url":video_url, "metadata":{"title":"DateFlow Reel","thumbnailOffset":2000}}}],
        "metadata": {"instagram":{"type":"reel","shouldShareToFeed":True}},
        "aiAssisted": True
    }
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as jf:
        json.dump(post, jf); tmp = jf.name

    env = os.environ.copy(); env["BUFFER_API_KEY"] = BUFFER_KEY
    r = subprocess.run(["buffer","posts","create","--input",tmp], capture_output=True, text=True, timeout=180, env=env)
    Path(tmp).unlink(missing_ok=True)
    print("STDOUT:", r.stdout[:500])
    if r.stderr: print("STDERR:", r.stderr[:400])
    ok = r.returncode==0 and '"id"' in r.stdout
    return ok, publish_trt, r.stdout


# ── 6. Telegram notification ───────────────────────────────────────────────
def notify_tg(text, video_path=None):
    try:
        base = f"https://api.telegram.org/bot{TG_TOKEN}"
        if video_path and Path(video_path).exists():
            with open(video_path,"rb") as v:
                requests.post(f"{base}/sendVideo",
                              data={"chat_id":ADMIN_CHAT,"caption":text[:1000]},
                              files={"video":v}, timeout=60)
        else:
            requests.post(f"{base}/sendMessage",
                          json={"chat_id":ADMIN_CHAT,"text":text[:4000]}, timeout=30)
    except Exception as e:
        print(f"  Telegram notify failed: {e}")


# ── 7. Main pipeline ────────────────────────────────────────────────────────
def build_one(idx, total, publish_time, used_hooks):
    """Build ONE reel and schedule it for the given publish_time."""
    print(f"\n{'='*60}")
    print(f"🎬 Reel {idx}/{total} → {publish_time.strftime('%a %H:%M')} TRT")
    print('='*60)

    # Pick script (avoid repeats within the batch)
    s = pick_script(avoid_hooks=list(used_hooks))
    hook = s["hook"]; body = s["body"]; cta = s["cta"]
    terms = s.get("terms","dating couple romantic date")
    used_hooks.add(hook)
    print(f"Hook: {hook}")

    # Render via MPT
    mpt_video = render_mpt(s, terms)

    # Save to output with time stamp
    ts = publish_time.strftime("%Y-%m-%d_%H%M")
    out_mp4 = OUT / f"reel_{ts}.mp4"
    shutil.copy(mpt_video, out_mp4)
    print(f"📁 Saved: {out_mp4.name} ({out_mp4.stat().st_size/1024/1024:.1f} MB)")

    # Caption
    import random as _r
    hashtags = [
        "#dateideas","#datenight","#firstdate","#datingtips","#dateflow",
        "#datingadvice","#relationshipadvice","#datingcoach","#dating101",
        "#relationshiptips","#moderndating","#datingformen","#relationshipgoals"
    ]
    random_tags = " ".join(_r.sample(hashtags, 7))
    caption = f"{hook} 🌹\n\n{body}\n\nTag someone who needs this 👇\n\n{random_tags}"

    no_publish = os.environ.get("NO_PUBLISH","0")=="1"
    if no_publish:
        print("🧪 NO_PUBLISH=1 — skipping Buffer schedule")
        notify_tg(f"🧪 Dry run Reel {idx}/{total} for {publish_time.strftime('%H:%M')}.\nHook: {hook}", video_path=out_mp4)
        return True, out_mp4

    # Upload
    print("📤 Uploading to public host...")
    try:
        video_url = upload_video(out_mp4)
    except Exception as e:
        notify_tg(f"❌ Reel {idx} upload failed: {e}")
        return False, out_mp4

    # Schedule
    print(f"📋 Scheduling to Buffer for {publish_time.strftime('%a %H:%M')} TRT...")
    ok, pub_time, raw = schedule_buffer(video_url, caption, publish_time)
    pub_str = pub_time.strftime("%a %H:%M")

    if ok:
        try:
            j = json.loads(raw); pid = (j.get("post") or {}).get("id","?")
        except Exception: pid = "?"
        msg = (f"✅ Reel #{idx}/{total} scheduled for {pub_str} TRT\n\n"
               f"Post ID: {pid}\nHook: {hook}\nVideo: {video_url}")
        print(f"✅ SUCCESS — auto-publishes at {pub_str}")
    else:
        msg = f"⚠️ Reel #{idx} Buffer failed. Schedule manually:\n{video_url}\n\nError: {raw[:300]}"
        print(f"❌ Reel #{idx} failed")

    notify_tg(msg, video_path=out_mp4 if ok else None)
    return ok, out_mp4


def build_and_publish():
    print(f"🌹 DateFlow MPT Auto-Pilot starting — {NUM_POSTS} reel(s)")
    slots = next_free_slots(NUM_POSTS)
    print(f"📅 Scheduling posts at: {[s.strftime('%a %H:%M') for s in slots]}")

    successes = 0
    used_hooks = set()
    for i, slot in enumerate(slots, 1):
        try:
            ok, path = build_one(i, NUM_POSTS, slot, used_hooks)
            if ok: successes += 1
        except Exception as e:
            import traceback; traceback.print_exc()
            notify_tg(f"❌ Reel #{i} crashed: {e}")
        if i < NUM_POSTS:
            time.sleep(5)

    # Итоговое сообщение
    total_msg = f"🎉 Batch complete: {successes}/{NUM_POSTS} reels scheduled.\n\nSlots:\n"
    for i, s in enumerate(slots, 1):
        total_msg += f"  {i}. {s.strftime('%a %d %b %H:%M')} TRT\n"
    print(total_msg)
    notify_tg(total_msg)
    return successes > 0


if __name__ == "__main__":
    ok = build_and_publish()
    sys.exit(0 if ok else 1)
