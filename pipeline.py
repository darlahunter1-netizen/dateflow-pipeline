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
    """Получить UTC времена уже запланированных постов в Buffer. Пробуем несколько форматов ответа."""
    times = set()
    try:
        if not (BUFFER_KEY and ORG_ID):
            return times
        env = os.environ.copy(); env["BUFFER_API_KEY"] = BUFFER_KEY
        r = subprocess.run(
            ["buffer", "posts", "list",
             "--organization-id", ORG_ID,
             "--limit", "100"],
            capture_output=True, text=True, timeout=30, env=env)
        if r.returncode != 0:
            print(f"  ⚠️ buffer posts list stderr: {r.stderr[:300]}")
            return times
        try:
            data = json.loads(r.stdout)
        except Exception:
            print(f"  ⚠ buffer posts list not JSON: {r.stdout[:300]}")
            return times
        # Пробуем разные форматы ответа
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = (data.get("items") or
                     data.get("data", {}).get("posts", []) or
                     data.get("posts", []) or
                     data.get("data", []))
            if isinstance(data.get("data"), dict):
                items = items or data["data"].get("items", [])
        print(f"  🔍 Buffer returned {len(items)} posts total")
        for p in items:
            # Считаем и scheduled, и sending/error как существующие (чтобы не дублировать битые)
            status = p.get("status", "")
            due = p.get("dueAt") or p.get("scheduled_at") or p.get("due_at") or p.get("scheduledAt")
            if due and status in ("scheduled", "sending", "pending", "error", "failed"):
                try:
                    dt = datetime.fromisoformat(str(due).replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    times.add(dt.astimezone(UTC))
                except Exception:
                    pass
    except Exception as e:
        print(f"  ⚠️ Could not fetch existing scheduled posts: {e}")
    return times

def next_free_slots(count):
    """Выбрать слоты для постов.
    Защита от пропусков и дубликатов:
    - Если запуск по расписанию: берём ближайший свободный слот в окне до 6 часов
      (чтобы если один cron пропустил, следующий за ним подхватил и не было пропусков в дне)
    - Если ручной запуск — любые свободные слоты в ближайшие 5 дней
    - Никогда не создаём 2 поста на один и тот же слот (±45 минут проверка)
    - Никогда не возвращаем слоты в прошлом."""
    now = trt_now()
    existing_utc = get_scheduled_times()
    print(f"  📋 Existing scheduled posts in Buffer: {len(existing_utc)}")
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    is_schedule = (event_name == "schedule") and count == 1

    slots = []
    day = now.date()
    while len(slots) < count:
        for hhmm in POST_TIMES:
            h, m = map(int, hhmm.split(":"))
            t_local = datetime(day.year, day.month, day.day, h, m, tzinfo=TRT)
            delta = t_local - now
            if delta <= timedelta(0):
                continue  # слот уже наступил или в прошлом
            # Защита от дубликатов: если в автоматическом запуске уже взяли слот
            # в пределах just-in-time, не берём следующий
            if is_schedule and slots:
                break
            if is_schedule and delta > timedelta(hours=6):
                # В автоматическом запуске не смотрим слоты дальше 6 часов вперёд
                continue
            # Проверяем не занят ли этот слот (±45 минут)
            t_utc = t_local.astimezone(UTC)
            occupied = any(abs((t - t_utc).total_seconds()) < 2700 for t in existing_utc)
            if occupied:
                print(f"  ⏭ Slot {t_local.strftime('%a %H:%M')} (in {delta}) already scheduled — skip")
                continue
            print(f"  ✅ Picking slot {t_local.strftime('%a %H:%M')} (in {delta})")
            slots.append(t_local)
            if len(slots) >= count:
                return slots
        day += timedelta(days=1)
        if day > (now + timedelta(days=5)).date():
            break
    if not slots:
        print(f"  ℹ Nothing to schedule right now (now={now.strftime('%H:%M %d %b')}).")
    return slots


OPENAI_KEY   = os.getenv("OPENAI_API_KEY", "")
PEXELS_KEY   = os.getenv("PEXELS_API_KEY", "")
BUFFER_KEY   = os.getenv("BUFFER_API_KEY", "")
CHANNEL_ID   = os.getenv("BUFFER_CHANNEL_ID", "6adc7b9b778b240e2fac6af1")
ORG_ID       = os.getenv("BUFFER_ORG_ID", "6ad678fa27469e130f893e13")
TG_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_CHAT   = os.getenv("ADMIN_CHAT_ID", "")

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
    """Загрузить MP4 как asset к GitHub Release (живёт вечно)."""
    gh_token = os.environ.get("GITHUB_TOKEN")
    gh_repo = os.environ.get("GITHUB_REPOSITORY")
    if not gh_token or not gh_repo:
        raise RuntimeError("GITHUB_TOKEN or GITHUB_REPOSITORY not set")
    headers = {"Authorization": f"token {gh_token}",
               "Accept": "application/vnd.github+json",
               "User-Agent": "DateFlow-Pipeline"}
    api = f"https://api.github.com/repos/{gh_repo}"
    tag = "dateflow-media"
    # 1. Get or create release (идемпотентно: создаём tag через refs если нужно, потом release)
    print(f"  📦 GitHub: checking release tag={tag} in {gh_repo}...")
    r = requests.get(f"{api}/releases/tags/{tag}", headers=headers, timeout=30)
    if r.status_code == 404:
        # Создаём lightweight tag на текущем HEAD
        sha = os.environ.get("GITHUB_SHA")
        print(f"  📦 GitHub: release not found, creating tag {tag} on {sha[:8] if sha else 'HEAD'}...")
        if sha:
            rt = requests.post(f"{api}/git/refs", headers=headers, timeout=20,
                               json={"ref": f"refs/tags/{tag}", "sha": sha})
            if rt.status_code == 422:
                # Тег уже существует — норм
                pass
            elif rt.status_code not in (200, 201):
                print(f"  ⚠ Could not create tag: {rt.status_code} → {rt.text[:200]}")
        print(f"  📦 GitHub: creating new release...")
        r = requests.post(f"{api}/releases", headers=headers, timeout=30,
                          json={"tag_name": tag, "name": "DateFlow Reels Media",
                                "body": "Auto-generated Reels assets", "draft": False,
                                "prerelease": False, "target_commitish": "main"})
        if r.status_code not in (200, 201):
            # Иногда тег создаётся релизом сам — пробуем ещё раз получить
            time.sleep(3)
            r = requests.get(f"{api}/releases/tags/{tag}", headers=headers, timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"GitHub create release failed: {r.status_code} → {r.text[:300]}")
    elif r.status_code != 200:
        raise RuntimeError(f"GitHub get release failed: {r.status_code} → {r.text[:300]}")
    release = r.json()
    upload_url = release["upload_url"].replace("{?name,label}", "")
    asset_name = path.name
    print(f"  📦 GitHub: uploading {asset_name} ({path.stat().st_size/1024/1024:.1f}MB)...")
    # 2. Upload asset
    with open(path, "rb") as f:
        r2 = requests.post(f"{upload_url}?name={asset_name}",
                           headers={**headers, "Content-Type": "application/octet-stream"},
                           data=f, timeout=600)
    if r2.status_code not in (201, 200):
        # If asset already exists (duplicate name), delete old one first then retry
        if r2.status_code == 422:
            print(f"  📦 GitHub: asset exists, deleting old one and retrying...")
            rel_id = release["id"]
            rl = requests.get(f"{api}/releases/{rel_id}/assets", headers=headers, timeout=30)
            for a in rl.json():
                if a["name"] == asset_name:
                    requests.delete(f"{api}/releases/assets/{a['id']}", headers=headers, timeout=30)
            with open(path, "rb") as f:
                r2 = requests.post(f"{upload_url}?name={asset_name}",
                                   headers={**headers, "Content-Type": "application/octet-stream"},
                                   data=f, timeout=600)
        if r2.status_code not in (200, 201):
            raise RuntimeError(f"GitHub upload failed: {r2.status_code} → {r2.text[:500]}")
    asset = r2.json()
    url = asset["browser_download_url"]
    asset_id = asset.get("id")
    # GitHub CDN может раздавать 404 первые до 60 секунд после загрузки — ждём
    print(f"  📦 GitHub: asset uploaded (id={asset_id}), waiting for CDN to propagate...")
    total_wait = 0
    for wait_sec in (3, 5, 7, 10, 10, 15, 15):
        time.sleep(wait_sec)
        total_wait += wait_sec
        h = requests.head(url, timeout=20, allow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "video/mp4,application/octet-stream,*/*"})
        size = int(h.headers.get("content-length", 0))
        ctype = h.headers.get("content-type", "")
        print(f"    waited {total_wait}s → status={h.status_code}, type={ctype}, size={size/1024/1024:.1f}MB")
        if h.status_code == 200 and size > 1024*1024:
            print(f"  ✅ GitHub asset is live: {url}")
            break
    else:
        # Попробуем GET с Range (иногда HEAD не работает пока файл не полностью пропагационен)
        r_test = requests.get(url, timeout=30, stream=True,
                              headers={"User-Agent": "Mozilla/5.0", "Range": "bytes=0-1023"})
        if r_test.status_code in (200, 206):
            print(f"  ✅ GitHub asset is live (GET check passed): {url}")
            return url
        raise RuntimeError(f"GitHub asset not accessible after {total_wait}s: {h.status_code}")
    return url

def upload_uguu(path):
    """uguu.se — хранит файлы 3 часа, прямая ссылка, полный файл без сжатия.
    Достаточно для just-in-time (загрузка за 25 мин до публикации)."""
    with open(path,"rb") as f:
        r = requests.post("https://uguu.se/upload",
                          files={"files[]":(path.name,f,"video/mp4")},
                          headers={
                              "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                              "Origin": "https://uguu.se",
                              "Referer": "https://uguu.se/"},
                          timeout=300)
    r.raise_for_status()
    data = r.json()
    assert data.get("success"), f"uguu bad response: {data}"
    url = data["files"][0]["url"]
    assert url.startswith("http") and ".uguu.se/" in url, f"uguu url bad: {url[:300]}"
    # Проверяем размер сразу
    expected = path.stat().st_size
    h = requests.head(url, timeout=20, allow_redirects=True, headers={"User-Agent":"Mozilla/5.0"})
    got = int(h.headers.get("content-length", 0))
    assert h.status_code == 200 and abs(got - expected) < expected * 0.05, \
        f"uguu size/HTTP mismatch: status={h.status_code}, expected {expected}, got {got}"
    print(f"    (uguu link: {url}, size={got/1024/1024:.1f}MB, live now)")
    return url

def upload_imgur(path):
    """Imgur анонимная загрузка — CDN. Видео лимит на Imgur до 1GB для анонимных."""
    with open(path, "rb") as f:
        r = requests.post("https://api.imgur.com/3/upload",
                          headers={"Authorization": "Client-ID 546c25a59c58ad7"},
                          data={"type":"file","title":"DateFlow Reel","name":path.name},
                          files={"video":(path.name,f,"video/mp4")},
                          timeout=300)
    if r.status_code != 200:
        raise RuntimeError(f"imgur status {r.status_code}: {r.text[:300]}")
    data = r.json()
    if not data.get("success"):
        raise RuntimeError(f"imgur upload failed: {data}")
    url = data["data"]["link"]
    if not url.endswith(".mp4"):
        # Попробуем получить mp4-версию
        mp4_url = data["data"].get("mp4")
        if mp4_url:
            url = mp4_url
    assert url.endswith(".mp4"), f"not mp4: {url}"
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

def upload_litterbox(path, expiry="1h"):
    """litterbox.catbox.moe — 1 час по умолчанию (для just-in-time достаточно)."""
    with open(path, "rb") as f:
        r = requests.post("https://litterbox.catbox.moe/resources/internals/api.php",
                          data={"reqtype":"fileupload","time":expiry},
                          files={"fileToUpload":(path.name,f,"video/mp4")},
                          headers={
                              "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                              "Origin":"https://litterbox.catbox.moe",
                              "Referer":"https://litterbox.catbox.moe/"},
                          timeout=300)
    r.raise_for_status()
    url = r.text.strip()
    assert url.startswith("https://litter.catbox.moe/"), f"litterbox bad: {r.status_code} → {url[:300]}"
    h = requests.head(url, timeout=20, allow_redirects=True, headers={"User-Agent":"Mozilla/5.0"})
    assert h.status_code == 200, f"litterbox HEAD: {h.status_code}"
    return url

def upload_video(path):
    """Загрузить видео в публичный GitHub Release.
    Репозиторий должен быть публичным: Buffer скачивает asset по browser_download_url.
    Временные хостинги здесь намеренно не используются.
    """
    print("  📤 Primary media host: GitHub Releases")
    return _upload_with_retries(upload_github_release, path, tries=3, delay=15)

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

def _preflight_edge_tts(voice="en-US-GuyNeural"):
    """Проверяем что edge-tts может синтезировать короткий текст перед запуском MPT."""
    import importlib.util
    if not importlib.util.find_spec("edge_tts"):
        print("  ⚠ edge-tts not installed, skipping preflight")
        return
    import asyncio, edge_tts
    for attempt in range(3):
        try:
            com = edge_tts.Communicate("Testing.", voice, rate="+0%")
            out = b""
            for chunk in com.stream_sync():
                if chunk["type"] == "audio":
                    out += chunk["data"]
                    break
            if out:
                print(f"  ✅ edge-tts connectivity OK (attempt {attempt+1})")
                return True
        except Exception as e:
            print(f"  ⚠ edge-tts preflight attempt {attempt+1} failed: {str(e)[:150]}")
            time.sleep(5)
    print("  ⚠ edge-tts preflight failed — continuing anyway (MPT will retry internally)")
    return False

def render_mpt(script, terms):
    ensure_mpt_config()
    _preflight_edge_tts()
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
    last_err = None
    for attempt in range(2):
        r = subprocess.run(cmd, cwd=str(MPT_DIR), capture_output=True, text=True, timeout=900)
        # Parse JSON result from last line
        out_lines = [l for l in r.stdout.strip().splitlines() if l.strip().startswith("{")]
        if out_lines:
            break
        print(f"  ⚠ MPT attempt {attempt+1} returned no JSON, stderr tail: {r.stderr[-600:]}")
        last_err = (r, RuntimeError("MPT did not return a result JSON"))
        time.sleep(10)
        if attempt == 1:
            r, _ = last_err
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
    print(f"🕒 event={os.environ.get('GITHUB_EVENT_NAME', 'unknown')} | "
          f"run={os.environ.get('GITHUB_RUN_ID', 'local')} "
          f"attempt={os.environ.get('GITHUB_RUN_ATTEMPT', '1')} | "
          f"TRT now={trt_now().strftime('%Y-%m-%d %H:%M:%S %Z')}")
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
    if slots:
        total_msg = f"🎉 Batch complete: {successes}/{len(slots)} reels scheduled.\n\nSlots:\n"
        for i, s in enumerate(slots, 1):
            total_msg += f"  {i}. {s.strftime('%a %d %b %H:%M')} TRT\n"
        print(total_msg)
        notify_tg(total_msg)
    else:
        print("(nothing to do — all needed slots already have posts scheduled)")
    # Завершаемся с кодом 0 ВСЕГДА (если нет слотов — это нормально, например
    # срабатывание cron у чужого слота; не должно помечать запуск как failed)
    return 0


if __name__ == "__main__":
    sys.exit(build_and_publish())
