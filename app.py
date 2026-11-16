"""
Jackson — Renovate Builders' posting agent (English only).

  • Manual posts: upload job photos + a note → Jackson writes Instagram/Facebook posts → you approve.
  • Autopilot:   on the days/time you choose, Jackson plans a post himself (educational tips,
                 seasonal advice, FAQs, trends from the web, or a project from your photo library),
                 designs carousel slides when needed, and either waits for your approval or posts.

Claude Opus 5.5 writes; Jackson publishes directly through Meta's Graph API.

Run locally:
    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=...  ADMIN_PASSWORD=...  PUBLIC_URL=https://<address Meta can reach>
    uvicorn app:app --port 8000      → open http://localhost:8000
"""

import base64
import io
import json
import os
import secrets
import shutil
import smtplib
import threading
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

import anthropic
import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from PIL import Image, ImageOps
from pydantic import BaseModel

import slides

try:                                   # lets Jackson read iPhone HEIC photos
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Settings (environment variables on Render)
# ---------------------------------------------------------------------------
BASE = Path(__file__).parent
DATA = Path(os.environ.get("DATA_DIR", BASE / "data"))
DATA.mkdir(parents=True, exist_ok=True)
META_FILE = DATA / "meta_connection.json"
AUTOPILOT_FILE = DATA / "autopilot.json"
LIBRARY = DATA / "library"

MODEL = "claude-opus-5-5"
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
# Address where Meta downloads the photos from. Render sets RENDER_EXTERNAL_URL automatically.
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
GRAPH = f"https://graph.facebook.com/{os.environ.get('GRAPH_VERSION', 'v26.0')}"
META_APP_ID = os.environ.get("META_APP_ID")          # optional: turns short test tokens into 60-day ones
META_APP_SECRET = os.environ.get("META_APP_SECRET")
# Optional e-mail notifications (e.g. Gmail with an App Password)
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")

MAX_PHOTOS = 10
INSTAGRAM_MAX_CHARS = 2200
PREPARE_HOURS_AHEAD = 24          # autopilot prepares each post this long before its time slot
PLATFORMS = ("instagram", "facebook")
CONTENT_TYPES = {
    "educational": "Educational tips carousel (e.g. '5 signs your bathroom needs re-waterproofing')",
    "seasonal": "Seasonal / timely advice carousel tied to the current month, weather or holidays",
    "faq": "Answer one question customers often ask (cost drivers, timelines, permits, materials) as a carousel",
    "trend": "A current renovation or interior-design trend, found with web search, explained as a carousel",
    "showcase": "Showcase a project from the photo library (uses the real job photos)",
}

BRAND = (BASE / "brand.md").read_text(encoding="utf-8")

client = anthropic.Anthropic()
security = HTTPBasic()
publish_lock = threading.Lock()
claim_lock = threading.Lock()
autopilot_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Autopilot settings (stored on the disk, edited on the Autopilot page)
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "enabled": False,
    "days": [0, 2, 4],                 # Monday=0 … Sunday=6  → Mon, Wed, Fri
    "times": ["18:00"],                # one post per time on each chosen day
    "mode": "approve",                 # "approve" = waits for you, "auto" = posts by itself
    "platforms": ["instagram", "facebook"],
    "content_types": ["educational", "seasonal", "faq", "trend", "showcase"],
    "use_web_search": True,
    "areas": [],                       # cities/neighborhoods served; Jackson rotates through them
    "timezone": os.environ.get("TIMEZONE", "Asia/Jerusalem"),
    "brand_color": "#c2410c",
    "notify_email": "",
    "prepared_slots": [],
    "area_history": [],
    "last_run": None,
    "last_error": None,
}


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    raw = {}
    if AUTOPILOT_FILE.exists():
        try:
            raw = json.loads(AUTOPILOT_FILE.read_text(encoding="utf-8"))
        except ValueError:
            raw = {}
    s.update(raw)
    if "time" in raw and "times" not in raw:     # saved by the older one-time-a-day version
        s["times"] = [raw["time"]]
    s.pop("time", None)
    if "areas" not in raw and raw.get("location"):  # older single-area setting
        s["areas"] = split_areas(raw["location"])
    s.pop("location", None)
    return s


def split_areas(text) -> list[str]:
    items = text if isinstance(text, list) else str(text).replace(";", ",").replace("\n", ",").split(",")
    out = []
    for a in items:
        a = str(a).strip()
        if a and a.lower() not in [x.lower() for x in out]:
            out.append(a[:60])
    return out[:30]


def pick_area(settings: dict) -> str | None:
    """Rotate through the service areas: the one used least in recent posts goes next."""
    areas = settings.get("areas") or []
    if not areas:
        return None
    used = list(reversed(settings.get("area_history") or []))[:60]    # most recent first
    def score(a):
        last = next((i for i, u in enumerate(used) if u == a), 999)   # how many posts ago it was used
        return (used.count(a), -last, areas.index(a))
    return sorted(areas, key=score)[0]


def remember_area(area: str | None):
    if not area:
        return
    s = load_settings()
    s["area_history"] = ((s.get("area_history") or []) + [area])[-60:]
    save_settings(s)


def save_settings(s: dict):
    tmp = AUTOPILOT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2), encoding="utf-8")
    tmp.replace(AUTOPILOT_FILE)


def tz() -> ZoneInfo:
    try:
        return ZoneInfo(load_settings()["timezone"])
    except Exception:
        return ZoneInfo("Asia/Jerusalem")


def now() -> datetime:
    return datetime.now(tz())


def parse_local(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=tz())


def require_login(creds: HTTPBasicCredentials = Depends(security)):
    if not ADMIN_PASSWORD:
        raise HTTPException(500, "Set ADMIN_PASSWORD on the server first.")
    if not (secrets.compare_digest(creds.username, ADMIN_USER) and
            secrets.compare_digest(creds.password, ADMIN_PASSWORD)):
        raise HTTPException(401, "Wrong username or password", headers={"WWW-Authenticate": "Basic"})


# ---------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------

def prepare_photo(raw: bytes) -> bytes:
    """Fix rotation, convert to JPEG, crop to Instagram's allowed shape (4:5 … 1.91:1)."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    w, h = img.size
    if w / h < 0.8:
        new_h = int(w / 0.8); top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))
    elif w / h > 1.91:
        new_w = int(h * 1.91); left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    img.thumbnail((1440, 1800))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90, optimize=True)
    return out.getvalue()


def small_b64(jpeg: bytes) -> str:
    img = Image.open(io.BytesIO(jpeg)); img.thumbnail((1024, 1024))
    out = io.BytesIO(); img.save(out, "JPEG", quality=80)
    return base64.b64encode(out.getvalue()).decode()


def safe_id(x: str) -> str:
    if not x or not x.isalnum():
        raise HTTPException(400, "Bad id")
    return x


# ---------------------------------------------------------------------------
# Drafts on disk
# ---------------------------------------------------------------------------

def draft_dir(draft_id: str) -> Path:
    d = DATA / "drafts" / safe_id(draft_id)
    if not d.exists():
        raise HTTPException(404, "Draft not found")
    return d


def load_draft(draft_id: str) -> dict:
    return json.loads((draft_dir(draft_id) / "draft.json").read_text(encoding="utf-8"))


def save_draft(d: dict):
    path = DATA / "drafts" / d["id"] / "draft.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def new_draft(photos: list[bytes], notes: str, **extra) -> dict:
    draft_id = uuid.uuid4().hex[:12]
    d_path = DATA / "drafts" / draft_id
    d_path.mkdir(parents=True)
    for i, jpg in enumerate(photos, 1):
        (d_path / f"photo_{i:02d}.jpg").write_bytes(jpg)
    return {"id": draft_id, "media_key": secrets.token_urlsafe(16), "created": now().isoformat(timespec="minutes"),
            "notes": notes.strip()[:3000], "status": "draft", "photo_count": len(photos),
            "platforms": list(PLATFORMS), "origin": "manual", **extra}


def public_photo_urls(d: dict) -> list[str]:
    return [f"{PUBLIC_URL}/media/{d['id']}/{d['media_key']}/{n}.jpg" for n in range(1, d["photo_count"] + 1)]


def all_drafts() -> list[dict]:
    out = []
    for f in (DATA / "drafts").glob("*/draft.json"):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return out


# ---------------------------------------------------------------------------
# Photo library (job photos the autopilot can reuse)
# ---------------------------------------------------------------------------

def library_items() -> list[dict]:
    items = []
    for f in LIBRARY.glob("*/item.json"):
        try:
            items.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return sorted(items, key=lambda x: x["created"], reverse=True)


def save_item(it: dict):
    (LIBRARY / it["id"] / "item.json").write_text(json.dumps(it, ensure_ascii=False, indent=2), encoding="utf-8")


def item_photos(item_id: str) -> list[bytes]:
    return [p.read_bytes() for p in sorted((LIBRARY / safe_id(item_id)).glob("photo_*.jpg"))]


# ---------------------------------------------------------------------------
# Claude: writing posts from photos (manual posts)
# ---------------------------------------------------------------------------

SYSTEM_WRITER = f"""You are Jackson, the social media manager for Renovate Builders, a renovation company.
You write in English only. Your posts win new renovation clients: practical, trustworthy, specific.
Only state facts that are visible in the photos or given to you. Never invent prices, timelines,
awards, client names or addresses. Follow the brand guide exactly.

<brand_guide>
{BRAND}
</brand_guide>"""

WRITE_TOOL = {
    "name": "write_posts",
    "description": "Return the finished posts.",
    "input_schema": {
        "type": "object",
        "properties": {
            "instagram": {"type": "string", "description": f"Caption with hashtags at the end. Max {INSTAGRAM_MAX_CHARS} characters."},
            "facebook": {"type": "string", "description": "Slightly longer, conversational, 0–3 hashtags."},
            "alt_text": {"type": "string", "description": "One-sentence description of the first image, for accessibility."},
            "note_for_owner": {"type": "string", "description": "Optional short tip for the owner (e.g. a blurry photo, a detail worth adding). Empty if none."},
        },
        "required": ["instagram", "facebook", "alt_text"],
    },
}


def write_posts(d: dict, feedback: str | None = None) -> dict:
    content = []
    for i, p in enumerate(sorted(draft_dir(d["id"]).glob("photo_*.jpg")), 1):
        content += [{"type": "text", "text": f"Image {i}:"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": small_b64(p.read_bytes())}}]
    request = f"Notes about this post:\n{d['notes'] or '(no notes, describe what you see)'}\n\nWrite the posts in English."
    if d.get("origin") == "autopilot":
        request += "\nThese images are carousel slides Jackson designed; write captions that match them."
    if feedback and d.get("posts"):
        request += (f"\n\nYour previous version:\n{json.dumps(d['posts'], ensure_ascii=False)}"
                    f"\n\nThe owner wants these changes: {feedback}")
    content.append({"type": "text", "text": request})
    resp = client.messages.create(model=MODEL, max_tokens=3000, system=SYSTEM_WRITER, tools=[WRITE_TOOL],
                                  tool_choice={"type": "tool", "name": "write_posts"},
                                  messages=[{"role": "user", "content": content}])
    posts = dict(next(b.input for b in resp.content if b.type == "tool_use"))
    posts["instagram"] = posts["instagram"][:INSTAGRAM_MAX_CHARS]
    posts.setdefault("note_for_owner", "")
    return posts


# ---------------------------------------------------------------------------
# Claude: autopilot planning (with optional web search for trends)
# ---------------------------------------------------------------------------

PLAN_TOOL = {
    "name": "plan_post",
    "description": "Submit the finished plan for one social media post.",
    "input_schema": {
        "type": "object",
        "properties": {
            "content_type": {"type": "string", "enum": list(CONTENT_TYPES)},
            "topic": {"type": "string", "description": "Short topic label, e.g. 'Signs of bathroom leaks'."},
            "library_item_id": {"type": "string", "description": "Only for 'showcase': the id of the library project to use."},
            "cover_title": {"type": "string", "description": "Carousel cover headline, max ~60 characters, hooks in 1 second."},
            "kicker": {"type": "string", "description": "2–3 word label above the title, e.g. 'Renovation tips', 'FAQ', 'Trend watch'."},
            "slides": {
                "type": "array", "minItems": 3, "maxItems": 6,
                "description": "Carousel content slides (not needed for 'showcase').",
                "items": {"type": "object", "properties": {
                    "headline": {"type": "string", "description": "Max ~50 characters."},
                    "body": {"type": "string", "description": "1–3 short sentences, max ~220 characters."}},
                    "required": ["headline", "body"]},
            },
            "cta_title": {"type": "string", "description": "Closing slide headline, e.g. 'Planning a bathroom renovation?'"},
            "cta_text": {"type": "string", "description": "One sentence inviting them to message Renovate Builders."},
            "instagram": {"type": "string", "description": f"Instagram caption with 8–15 hashtags at the end. Max {INSTAGRAM_MAX_CHARS} characters."},
            "facebook": {"type": "string", "description": "Facebook post, conversational, 0–3 hashtags."},
            "alt_text": {"type": "string", "description": "One sentence describing the first image."},
            "note_for_owner": {"type": "string", "description": "Why you chose this topic, and any source you used for a trend (short)."},
        },
        "required": ["content_type", "topic", "instagram", "facebook", "alt_text", "note_for_owner"],
    },
}


def plan_with_claude(settings: dict, area: str | None = None) -> dict:
    allowed = [t for t in settings["content_types"] if t in CONTENT_TYPES] or ["educational"]
    lib = [it for it in library_items()][:40]
    if not lib and "showcase" in allowed:
        allowed = [t for t in allowed if t != "showcase"] or ["educational"]
    recent = sorted(all_drafts(), key=lambda x: x["created"], reverse=True)[:15]
    recent_txt = "\n".join(f"- {d['created'][:10]} [{d.get('content_type', 'project')}] "
                           f"{d.get('topic') or d.get('notes', '')[:90]}" for d in recent) or "(none yet)"
    lib_txt = "\n".join(f"- id={it['id']} used={it.get('used', 0)}x last_used={it.get('last_used') or 'never'}: "
                        f"{it.get('notes') or '(no notes)'}" for it in lib) or "(empty)"
    n = now()
    areas = settings.get("areas") or []
    if area:
        others = ", ".join(a for a in areas if a != area)
        area_txt = (f"Focus area for THIS post: {area}. Mention it naturally (e.g. 'homeowners in {area}') and use "
                    f"1–2 local hashtags for it. Don't mention other cities in this post. "
                    f"(Other areas we serve, used in other posts: {others or 'none'}.)")
    else:
        area_txt = "Service area: see the brand guide."
    request = f"""Plan ONE new post for Renovate Builders' Instagram and Facebook.

Today: {n.strftime('%A %d %B %Y')}\n{area_txt}
Allowed content types (pick the one that best fits right now, and vary it against recent posts):
{chr(10).join(f'- {t}: {CONTENT_TYPES[t]}' for t in allowed)}

Recent posts (do NOT repeat these topics or angles):
{recent_txt}

Photo library (for 'showcase' only; prefer projects used least):
{lib_txt}

Rules:
- English only. Practical, specific, genuinely useful to homeowners. No clickbait, no invented statistics.
- For carousels: a cover title that hooks, 3–6 slides with one clear point each, then a closing call to action.
- For 'trend': {'use web search (1–3 searches) to find a current renovation or interior trend, and mention the source in note_for_owner.' if settings.get('use_web_search') else 'web search is off; pick a well-established trend instead.'}
- For 'showcase': choose a library_item_id and write captions about that project using its notes only.
  Use the project's own location from its notes (ignore the focus area); if the notes give none, name no city.
- Finish by calling plan_post exactly once."""

    tools = [PLAN_TOOL]
    if settings.get("use_web_search") and "trend" in allowed:
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}, PLAN_TOOL]
    messages = [{"role": "user", "content": request}]

    for attempt in range(6):
        try:
            resp = client.messages.create(model=MODEL, max_tokens=4000, system=SYSTEM_WRITER,
                                          tools=tools, messages=messages,
                                          **({"tool_choice": {"type": "tool", "name": "plan_post"}} if attempt >= 3 else {}))
        except anthropic.BadRequestError as e:
            if len(tools) > 1 and "web search" in str(e).lower():   # web search disabled in the Console
                tools = [PLAN_TOOL]
                continue
            raise
        plan = next((b.input for b in resp.content if b.type == "tool_use" and b.name == "plan_post"), None)
        if plan:
            return dict(plan)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "pause_turn":          # long web search: let Claude continue
            continue
        messages.append({"role": "user", "content": "Now call plan_post with the finished plan."})
    raise RuntimeError("Jackson couldn't finish planning the post.")


def handle_for_slides() -> str:
    m = load_meta() or {}
    return f"@{m['ig_username']}" if m.get("ig_username") else "Renovate Builders"


def build_auto_draft(settings: dict, slot: datetime | None) -> dict:
    """Plan + build one autopilot draft. Returns the saved draft."""
    area = pick_area(settings)
    plan = plan_with_claude(settings, area)
    ctype = plan.get("content_type", "educational")
    lib_ids = {it["id"]: it for it in library_items()}
    item = lib_ids.get(plan.get("library_item_id") or "")

    if ctype == "showcase" and item:
        photos = item_photos(item["id"])[:MAX_PHOTOS]
        item["used"] = item.get("used", 0) + 1
        item["last_used"] = now().isoformat(timespec="minutes")
        save_item(item)
    else:
        if ctype == "showcase":                       # no valid project chosen → fall back to tips
            ctype = "educational"
        cover_photo = None
        if lib_ids:                                   # use the least-used library photo as cover background
            pick = sorted(lib_ids.values(), key=lambda x: (x.get("cover_used", 0), x["created"]))[0]
            pics = item_photos(pick["id"])
            if pics:
                cover_photo = pics[0]
                pick["cover_used"] = pick.get("cover_used", 0) + 1
                save_item(pick)
        photos = slides.render_carousel(
            plan.get("cover_title") or plan["topic"], plan.get("kicker") or "Renovation tips",
            plan.get("slides") or [], plan.get("cta_title", ""), plan.get("cta_text", ""),
            handle_for_slides(), settings.get("brand_color", "#c2410c"), cover_photo)

    notes = f"Autopilot · {ctype}: {plan['topic']}"
    if item and ctype == "showcase":
        notes += f"\nProject notes: {item.get('notes', '')}"
    if area and ctype != "showcase":
        notes += f" · {area}"
        remember_area(area)
    d = new_draft(photos, notes, origin="autopilot", content_type=ctype, topic=plan["topic"],
                  area=area if ctype != "showcase" else None,
                  platforms=[p for p in settings["platforms"] if p in PLATFORMS] or list(PLATFORMS))
    d["posts"] = {"instagram": plan["instagram"][:INSTAGRAM_MAX_CHARS], "facebook": plan["facebook"],
                  "alt_text": plan.get("alt_text", ""), "note_for_owner": plan.get("note_for_owner", "")}
    slot_s = slot.strftime("%Y-%m-%dT%H:%M") if slot else None
    d["slot"] = slot_s
    if settings["mode"] == "auto" and slot:
        d["status"], d["schedule_at"] = "scheduled", slot_s
    else:
        d["status"], d["schedule_at"] = "awaiting_approval", slot_s
    save_draft(d)
    return d


def notify(subject: str, body: str):
    to = load_settings().get("notify_email")
    if not (to and SMTP_HOST and SMTP_USER and SMTP_PASSWORD):
        return
    try:
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, SMTP_USER, to
        msg.set_content(body)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.send_message(msg)
    except Exception:
        traceback.print_exc()


def parse_times(times: list[str]) -> list[tuple[int, int]]:
    out = set()
    for t in times or []:
        try:
            hh, mm = (int(x) for x in str(t).split(":")[:2])
            if 0 <= hh < 24 and 0 <= mm < 60:
                out.add((hh, mm))
        except ValueError:
            pass
    return sorted(out)


def upcoming_slots(s: dict, horizon_hours: int = 24 * 8) -> list[datetime]:
    n = now()
    out = []
    for day in range(0, horizon_hours // 24 + 2):
        for hh, mm in parse_times(s.get("times")) or [(18, 0)]:
            dt = (n + timedelta(days=day)).replace(hour=hh, minute=mm, second=0, microsecond=0)
            if dt > n and dt.weekday() in s["days"] and dt <= n + timedelta(hours=horizon_hours):
                out.append(dt)
    return sorted(out)


def describe_slot(slot: str | None) -> str:
    return parse_local(slot).strftime("%A %d %b, %H:%M") if slot else "as soon as you approve"


def run_autopilot_once(slot: datetime | None) -> dict:
    """Prepare one post (for a slot, or right now when slot is None) and notify the owner."""
    with autopilot_lock:
        s = load_settings()
        try:
            d = build_auto_draft(s, slot)
            s = load_settings()
            s["last_run"], s["last_error"] = now().isoformat(timespec="minutes"), None
            save_settings(s)
        except Exception as e:
            traceback.print_exc()
            s = load_settings()
            s["last_error"] = f"{now().strftime('%d %b %H:%M')}: {e}"
            save_settings(s)
            notify("Jackson couldn't prepare a post", f"Jackson hit a problem while preparing a post:\n\n{e}")
            raise
    link = f"{PUBLIC_URL}/?draft={d['id']}" if PUBLIC_URL else ""
    if d["status"] == "awaiting_approval":
        notify(f"Jackson prepared a post: {d['topic']}",
               f"Jackson prepared a new post ({d['content_type']}): {d['topic']}\n\n"
               f"Planned for: {describe_slot(d.get('schedule_at'))}\n\n"
               f"Review and approve it here:\n{link}\n\nInstagram caption:\n{d['posts']['instagram']}")
    else:
        notify(f"Jackson scheduled a post: {d['topic']}",
               f"Autopilot scheduled this post for {describe_slot(d.get('schedule_at'))}.\n"
               f"Check or cancel it here:\n{link}\n\nInstagram caption:\n{d['posts']['instagram']}")
    return d


def autopilot_tick():
    s = load_settings()
    if not s["enabled"]:
        return
    n = now()
    for slot in upcoming_slots(s, PREPARE_HOURS_AHEAD):
        key = slot.strftime("%Y-%m-%dT%H:%M")
        if key in s["prepared_slots"]:
            continue
        s["prepared_slots"] = (s["prepared_slots"] + [key])[-60:]   # mark first: never prepare a slot twice
        save_settings(s)
        try:
            run_autopilot_once(slot)
        except Exception:
            pass
        s = load_settings()


# ---------------------------------------------------------------------------
# Meta (Facebook + Instagram) connection
# ---------------------------------------------------------------------------

class MetaError(Exception):
    pass


def graph(method: str, path: str, token: str, **params) -> dict:
    try:
        r = httpx.request(method, f"{GRAPH}/{path.lstrip('/')}", timeout=60,
                          **({"params": {**params, "access_token": token}} if method == "GET"
                             else {"data": {**params, "access_token": token}}))
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise MetaError(f"Couldn't reach Meta: {e}")
    if "error" in data:
        err = data["error"]
        msg = err.get("error_user_msg") or err.get("message", "unknown error")
        if err.get("code") == 190:
            msg += " (the access key is expired or was revoked: reconnect on the Connect page)"
        raise MetaError(msg)
    return data


def load_meta() -> dict | None:
    return json.loads(META_FILE.read_text()) if META_FILE.exists() else None


def meta_status() -> dict:
    m = load_meta()
    if not m:
        return {"connected": False}
    exp = m.get("expires_at")
    days_left = None if not exp else max(0, int((exp - time.time()) // 86400))
    return {"connected": True, "page_name": m["page_name"], "page_id": m["page_id"],
            "instagram": m.get("ig_username"), "expires_in_days": days_left,
            "connected_at": m.get("connected_at")}


def token_info(token: str) -> dict:
    try:
        return graph("GET", "debug_token", token, input_token=token).get("data", {})
    except MetaError:
        return {}


def maybe_extend(token: str) -> str:
    """Turn a short (1–2 hour) test token into a 60-day one, if the app id/secret are configured."""
    exp = token_info(token).get("expires_at", 0)
    if META_APP_ID and META_APP_SECRET and exp and exp - time.time() < 7 * 86400:
        try:
            r = httpx.get(f"{GRAPH}/oauth/access_token", timeout=30, params={
                "grant_type": "fb_exchange_token", "client_id": META_APP_ID,
                "client_secret": META_APP_SECRET, "fb_exchange_token": token}).json()
            return r.get("access_token", token)
        except (httpx.HTTPError, ValueError):
            pass
    return token


def list_pages(token: str) -> list[dict]:
    data = graph("GET", "me/accounts", token, limit=100,
                 fields="id,name,access_token,instagram_business_account{id,username}")
    return data.get("data", [])


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def wait_ready(container_id: str, token: str, timeout: int = 120):
    start = time.time()
    while time.time() - start < timeout:
        status = graph("GET", container_id, token, fields="status_code").get("status_code")
        if status in ("FINISHED", "PUBLISHED"):
            return
        if status in ("ERROR", "EXPIRED"):
            raise MetaError(f"Instagram couldn't process the image ({status}). Try a different photo.")
        time.sleep(3)
    raise MetaError("Instagram took too long to process the images. Try again in a few minutes.")


def publish_instagram(m: dict, urls: list[str], caption: str, alt: str) -> str:
    ig, tok = m["ig_user_id"], m["user_token"]
    if len(urls) == 1:
        container = graph("POST", f"{ig}/media", tok, image_url=urls[0], caption=caption, alt_text=alt[:1000])["id"]
    else:
        children = [graph("POST", f"{ig}/media", tok, image_url=u, is_carousel_item="true")["id"] for u in urls]
        for c in children:
            wait_ready(c, tok)
        container = graph("POST", f"{ig}/media", tok, media_type="CAROUSEL",
                          children=",".join(children), caption=caption)["id"]
    wait_ready(container, tok)
    media_id = graph("POST", f"{ig}/media_publish", tok, creation_id=container)["id"]
    try:
        return graph("GET", media_id, tok, fields="permalink").get("permalink", "")
    except MetaError:
        return ""


def publish_facebook(m: dict, urls: list[str], message: str) -> str:
    page, tok = m["page_id"], m["page_token"]
    if len(urls) == 1:
        post_id = graph("POST", f"{page}/photos", tok, url=urls[0], message=message).get("post_id")
    else:
        ids = [graph("POST", f"{page}/photos", tok, url=u, published="false")["id"] for u in urls]
        attached = {f"attached_media[{i}]": json.dumps({"media_fbid": pid}) for i, pid in enumerate(ids)}
        post_id = graph("POST", f"{page}/feed", tok, message=message, **attached)["id"]
    return f"https://www.facebook.com/{post_id}" if post_id else ""


def run_publish(draft_id: str):
    """Publish a draft to every chosen platform that hasn't succeeded yet."""
    with publish_lock:
        d = load_draft(draft_id)
        m = load_meta()
        d.setdefault("results", {})
        d["errors"] = {}
        if not m:
            d["errors"]["setup"] = "Instagram/Facebook aren't connected yet. Open the Connect page."
        elif not PUBLIC_URL:
            d["errors"]["setup"] = "PUBLIC_URL isn't set on the server, so Meta can't download the images."
        else:
            urls = public_photo_urls(d)
            for p in d["platforms"]:
                if d["results"].get(p):
                    continue
                try:
                    if p == "instagram":
                        if not m.get("ig_user_id"):
                            raise MetaError("No Instagram Business account is linked to this Facebook Page.")
                        d["results"][p] = publish_instagram(m, urls, d["posts"]["instagram"],
                                                            d["posts"].get("alt_text", "")) or "published"
                    else:
                        d["results"][p] = publish_facebook(m, urls, d["posts"]["facebook"]) or "published"
                except MetaError as e:
                    d["errors"][p] = str(e)
                except Exception as e:  # never let one platform crash the other
                    traceback.print_exc()
                    d["errors"][p] = f"Unexpected error: {e}"
        done = all(d["results"].get(p) for p in d["platforms"])
        d["status"] = "published" if done else "error"
        if done:
            d["published_at"] = now().isoformat(timespec="minutes")
        save_draft(d)
    if not done and d.get("origin") == "autopilot":
        notify("Jackson couldn't publish a post",
               f"Publishing '{d.get('topic')}' failed:\n{json.dumps(d['errors'], indent=1)}\n\n"
               f"Open it here to retry:\n{PUBLIC_URL}/?draft={d['id']}")
    return d


def claim(draft_id: str, allowed: tuple[str, ...]) -> bool:
    """Atomically mark a draft as 'publishing' so it can never be sent twice."""
    with claim_lock:
        d = load_draft(draft_id)
        if d["status"] not in allowed:
            return False
        d["status"] = "publishing"
        save_draft(d)
        return True


def publish_due_posts():
    for d in all_drafts():
        if d.get("status") == "scheduled" and d.get("schedule_at") and parse_local(d["schedule_at"]) <= now():
            if claim(d["id"], ("scheduled",)):
                run_publish(d["id"])


def scheduler_loop():
    """Every 30 seconds: publish posts that are due, and let the autopilot prepare upcoming ones."""
    while True:
        for job in (publish_due_posts, autopilot_tick):
            try:
                job()
            except Exception:
                traceback.print_exc()
        time.sleep(30)


@asynccontextmanager
async def lifespan(_app):
    (DATA / "drafts").mkdir(exist_ok=True)
    LIBRARY.mkdir(exist_ok=True)
    for d in all_drafts():               # anything stuck mid-publish after a restart goes back to "scheduled"
        if d.get("status") == "publishing":
            d["status"] = "scheduled"
            d["schedule_at"] = d.get("schedule_at") or now().strftime("%Y-%m-%dT%H:%M")
            save_draft(d)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    yield


app = FastAPI(title="Jackson posting agent", lifespan=lifespan)
EDITABLE = ("draft", "error", "awaiting_approval")


# ---------------------------------------------------------------------------
# API: drafts
# ---------------------------------------------------------------------------

@app.post("/api/drafts", dependencies=[Depends(require_login)])
async def create_draft(photos: list[UploadFile] = File(...), notes: str = Form("")):
    if not photos:
        raise HTTPException(400, "Add at least one photo.")
    if len(photos) > MAX_PHOTOS:
        raise HTTPException(400, f"Up to {MAX_PHOTOS} photos per post.")
    try:
        jpgs = [prepare_photo(await f.read()) for f in photos]
    except Exception:
        raise HTTPException(400, "One of the files isn't a photo Jackson can read (use JPG, PNG or HEIC).")
    d = new_draft(jpgs, notes)
    try:
        d["posts"] = write_posts(d)
    except anthropic.APIError as e:
        shutil.rmtree(DATA / "drafts" / d["id"], ignore_errors=True)
        raise HTTPException(502, f"Jackson couldn't write the post right now ({e.__class__.__name__}). Try again.")
    save_draft(d)
    return d


class RegenerateIn(BaseModel):
    feedback: str = ""


@app.post("/api/drafts/{draft_id}/regenerate", dependencies=[Depends(require_login)])
def regenerate(draft_id: str, body: RegenerateIn):
    d = load_draft(draft_id)
    if d["status"] not in EDITABLE:
        raise HTTPException(409, "This post was already sent or scheduled.")
    try:
        new = write_posts(d, body.feedback.strip()[:1000] or "Write a fresh alternative version.")
    except anthropic.APIError as e:
        raise HTTPException(502, f"Jackson couldn't rewrite the post right now ({e.__class__.__name__}).")
    d["posts"] = new
    save_draft(d)
    return d


class PublishIn(BaseModel):
    platforms: list[str]
    instagram: str = ""
    facebook: str = ""
    schedule_at: str | None = None     # "YYYY-MM-DDTHH:MM" local time; None = now


@app.post("/api/drafts/{draft_id}/publish", dependencies=[Depends(require_login)])
def publish(draft_id: str, body: PublishIn):
    d = load_draft(draft_id)
    if d["status"] not in EDITABLE:
        raise HTTPException(409, "This post was already sent or scheduled.")
    platforms = [p for p in body.platforms if p in PLATFORMS]
    if not platforms:
        raise HTTPException(400, "Choose at least one platform.")
    if not load_meta():
        raise HTTPException(400, "Connect Instagram and Facebook first (Connect page).")
    if not PUBLIC_URL:
        raise HTTPException(500, "PUBLIC_URL isn't set on the server.")
    d["posts"].update(instagram=body.instagram[:INSTAGRAM_MAX_CHARS], facebook=body.facebook)
    d["platforms"] = platforms
    if body.schedule_at:
        if parse_local(body.schedule_at) <= now():
            raise HTTPException(400, "The scheduled time is in the past.")
        d["status"], d["schedule_at"] = "scheduled", parse_local(body.schedule_at).strftime("%Y-%m-%dT%H:%M")
        save_draft(d)
        return d
    save_draft(d)
    if not claim(draft_id, EDITABLE):
        raise HTTPException(409, "This post is already being sent.")
    return run_publish(draft_id)


@app.post("/api/drafts/{draft_id}/unschedule", dependencies=[Depends(require_login)])
def unschedule(draft_id: str):
    d = load_draft(draft_id)
    if d["status"] != "scheduled":
        raise HTTPException(409, "This post isn't scheduled.")
    d["status"] = "awaiting_approval" if d.get("origin") == "autopilot" else "draft"
    d["schedule_at"] = None
    save_draft(d)
    return d


@app.delete("/api/drafts/{draft_id}", dependencies=[Depends(require_login)])
def delete_draft(draft_id: str):
    d = load_draft(draft_id)
    if d["status"] not in EDITABLE + ("scheduled",):
        raise HTTPException(409, "Published posts can't be deleted here.")
    shutil.rmtree(draft_dir(draft_id), ignore_errors=True)
    return {"deleted": True}


@app.get("/api/drafts/{draft_id}", dependencies=[Depends(require_login)])
def get_draft(draft_id: str):
    return load_draft(draft_id)


@app.get("/api/history", dependencies=[Depends(require_login)])
def history():
    items = [{k: d.get(k) for k in ("id", "created", "status", "notes", "platforms", "schedule_at",
                                     "published_at", "origin", "topic", "content_type")}
             | {"preview": (d.get("posts") or {}).get("instagram", "")[:140]} for d in all_drafts()]
    return sorted(items, key=lambda x: x["created"], reverse=True)[:60]


# ---------------------------------------------------------------------------
# API: photo library
# ---------------------------------------------------------------------------

@app.get("/api/library", dependencies=[Depends(require_login)])
def get_library():
    return library_items()


@app.post("/api/library", dependencies=[Depends(require_login)])
async def add_library(photos: list[UploadFile] = File(...), notes: str = Form("")):
    if not photos or len(photos) > MAX_PHOTOS:
        raise HTTPException(400, f"Add 1–{MAX_PHOTOS} photos of one project.")
    try:
        jpgs = [prepare_photo(await f.read()) for f in photos]
    except Exception:
        raise HTTPException(400, "One of the files isn't a photo Jackson can read (use JPG, PNG or HEIC).")
    item_id = uuid.uuid4().hex[:12]
    (LIBRARY / item_id).mkdir(parents=True)
    for i, j in enumerate(jpgs, 1):
        (LIBRARY / item_id / f"photo_{i:02d}.jpg").write_bytes(j)
    it = {"id": item_id, "created": now().isoformat(timespec="minutes"), "notes": notes.strip()[:1500],
          "photo_count": len(jpgs), "used": 0, "last_used": None}
    save_item(it)
    return it


@app.delete("/api/library/{item_id}", dependencies=[Depends(require_login)])
def delete_library(item_id: str):
    shutil.rmtree(LIBRARY / safe_id(item_id), ignore_errors=True)
    return {"deleted": True}


@app.get("/library/{item_id}/{n}", dependencies=[Depends(require_login)])
def library_photo(item_id: str, n: int):
    p = LIBRARY / safe_id(item_id) / f"photo_{n:02d}.jpg"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg")


# ---------------------------------------------------------------------------
# API: autopilot
# ---------------------------------------------------------------------------

class SettingsIn(BaseModel):
    enabled: bool
    days: list[int]
    times: list[str]
    mode: str
    platforms: list[str]
    content_types: list[str]
    use_web_search: bool
    areas: list[str] = []
    timezone: str
    brand_color: str = "#c2410c"
    notify_email: str = ""


def autopilot_status() -> dict:
    s = load_settings()
    return {**{k: v for k, v in s.items() if k not in ("prepared_slots", "area_history")},
            "next_slots": [describe_slot(x.strftime("%Y-%m-%dT%H:%M")) for x in upcoming_slots(s)][:6]
            if s["enabled"] else [],
            "email_ready": bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD),
            "library_count": len(library_items()),
            "content_type_labels": CONTENT_TYPES}


@app.get("/api/autopilot", dependencies=[Depends(require_login)])
def get_autopilot():
    return autopilot_status()


@app.post("/api/autopilot", dependencies=[Depends(require_login)])
def set_autopilot(body: SettingsIn):
    if body.timezone not in available_timezones():
        raise HTTPException(400, "Unknown time zone.")
    times = parse_times(body.times)[:4]
    if not times:
        raise HTTPException(400, "Add at least one time, like 18:00.")
    days = sorted({d for d in body.days if 0 <= d <= 6})
    if body.enabled and not days:
        raise HTTPException(400, "Pick at least one day.")
    s = load_settings()
    s.update(enabled=body.enabled, days=days, times=[f"{h:02d}:{m:02d}" for h, m in times],
             mode="auto" if body.mode == "auto" else "approve",
             platforms=[p for p in body.platforms if p in PLATFORMS] or list(PLATFORMS),
             content_types=[c for c in body.content_types if c in CONTENT_TYPES] or ["educational"],
             use_web_search=body.use_web_search, areas=split_areas(body.areas),
             timezone=body.timezone, brand_color=body.brand_color if body.brand_color.startswith("#") else "#c2410c",
             notify_email=body.notify_email.strip()[:200])
    save_settings(s)
    return autopilot_status()


@app.post("/api/autopilot/run-now", dependencies=[Depends(require_login)])
def autopilot_run_now():
    if autopilot_lock.locked():
        raise HTTPException(409, "Jackson is already preparing a post. Give him a minute.")
    try:
        return run_autopilot_once(None)
    except Exception as e:
        raise HTTPException(502, f"Jackson couldn't prepare a post: {e}")


# ---------------------------------------------------------------------------
# API: Meta connection
# ---------------------------------------------------------------------------

class ConnectIn(BaseModel):
    token: str
    page_id: str | None = None


@app.get("/api/meta/status", dependencies=[Depends(require_login)])
def get_meta_status():
    return meta_status() | {"public_url": PUBLIC_URL}


@app.post("/api/meta/connect", dependencies=[Depends(require_login)])
def connect_meta(body: ConnectIn):
    token = maybe_extend(body.token.strip())
    try:
        pages = list_pages(token)
    except MetaError as e:
        raise HTTPException(400, f"Meta said: {e}")
    if not pages:
        raise HTTPException(400, "This access key can't see any Facebook Page. Make sure you gave it access "
                                 "to your Page and the pages_show_list permission.")
    if len(pages) > 1 and not body.page_id:
        return {"choose_page": [{"id": p["id"], "name": p["name"],
                                 "instagram": (p.get("instagram_business_account") or {}).get("username")} for p in pages]}
    page = next((p for p in pages if p["id"] == body.page_id), pages[0])
    ig = page.get("instagram_business_account") or {}
    exp = token_info(token).get("expires_at") or None
    META_FILE.write_text(json.dumps({
        "user_token": token, "page_id": page["id"], "page_name": page["name"],
        "page_token": page["access_token"], "ig_user_id": ig.get("id"), "ig_username": ig.get("username"),
        "expires_at": exp, "connected_at": now().isoformat(timespec="minutes")}, indent=2))
    return meta_status()


@app.post("/api/meta/disconnect", dependencies=[Depends(require_login)])
def disconnect_meta():
    META_FILE.unlink(missing_ok=True)
    return {"connected": False}


# ---------------------------------------------------------------------------
# Pages and files
# ---------------------------------------------------------------------------

@app.get("/media/{draft_id}/{key}/{n}.jpg")
def public_media(draft_id: str, key: str, n: int):
    """Images Meta downloads when publishing. Unguessable link, no login needed."""
    d = load_draft(draft_id)
    if not secrets.compare_digest(key, d["media_key"]):
        raise HTTPException(404)
    p = draft_dir(draft_id) / f"photo_{n:02d}.jpg"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg")


@app.get("/photos/{draft_id}/{n}", dependencies=[Depends(require_login)])
def photo(draft_id: str, n: int):
    p = draft_dir(draft_id) / f"photo_{n:02d}.jpg"
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, media_type="image/jpeg")


@app.get("/health")
def health():
    s = load_settings()
    return {"ok": True, "agent": "Jackson", "model": MODEL, "public_url": PUBLIC_URL or None,
            "meta_connected": META_FILE.exists(), "data_dir": str(DATA),
            "autopilot": "on" if s["enabled"] else "off"}


@app.get("/static/style.css")
def stylesheet():
    return FileResponse(BASE / "style.css", media_type="text/css")


@app.get("/connect", dependencies=[Depends(require_login)])
def connect_page():
    return FileResponse(BASE / "connect.html")


@app.get("/autopilot", dependencies=[Depends(require_login)])
def autopilot_page():
    return FileResponse(BASE / "autopilot.html")


@app.get("/", dependencies=[Depends(require_login)])
def home():
    return FileResponse(BASE / "index.html")
