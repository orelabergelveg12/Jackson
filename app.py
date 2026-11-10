"""
Jackson — Renovate Builders' posting agent.
Claude Opus 5.5 writes the posts; Jackson publishes them himself to Instagram and Facebook
through Meta's official Graph API (no third-party posting service).

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
import threading
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic
import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from PIL import Image, ImageOps
from pydantic import BaseModel

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

MODEL = "claude-opus-5-5"
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
# Address where Meta downloads the photos from. Render sets RENDER_EXTERNAL_URL automatically.
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
GRAPH = f"https://graph.facebook.com/{os.environ.get('GRAPH_VERSION', 'v26.0')}"
META_APP_ID = os.environ.get("META_APP_ID")          # optional: turns short test tokens into 60-day ones
META_APP_SECRET = os.environ.get("META_APP_SECRET")
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Jerusalem")
TZ = ZoneInfo(TIMEZONE)
MAX_PHOTOS = 10
INSTAGRAM_MAX_CHARS = 2200

BRAND = (BASE / "brand.md").read_text(encoding="utf-8")
LANGUAGES = {"en": "English", "he": "Hebrew", "both": "Hebrew first, then the same text in English"}
PLATFORMS = ("instagram", "facebook")

client = anthropic.Anthropic()
security = HTTPBasic()
publish_lock = threading.Lock()


def now() -> datetime:
    return datetime.now(TZ)


def require_login(creds: HTTPBasicCredentials = Depends(security)):
    if not ADMIN_PASSWORD:
        raise HTTPException(500, "Set ADMIN_PASSWORD on the server first.")
    if not (secrets.compare_digest(creds.username, ADMIN_USER) and
            secrets.compare_digest(creds.password, ADMIN_PASSWORD)):
        raise HTTPException(401, "Wrong username or password", headers={"WWW-Authenticate": "Basic"})


# ---------------------------------------------------------------------------
# Photos: fix rotation, convert to JPEG, crop to Instagram's allowed shape (4:5 … 1.91:1)
# ---------------------------------------------------------------------------

def prepare_photo(raw: bytes) -> bytes:
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


# ---------------------------------------------------------------------------
# Drafts on disk
# ---------------------------------------------------------------------------

def draft_dir(draft_id: str) -> Path:
    if not draft_id.isalnum():
        raise HTTPException(400, "Bad draft id")
    d = DATA / "drafts" / draft_id
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


def public_photo_urls(d: dict) -> list[str]:
    return [f"{PUBLIC_URL}/media/{d['id']}/{d['media_key']}/{n}.jpg" for n in range(1, d["photo_count"] + 1)]


# ---------------------------------------------------------------------------
# Claude writes the posts
# ---------------------------------------------------------------------------

WRITE_TOOL = {
    "name": "write_posts",
    "description": "Return the finished posts.",
    "input_schema": {
        "type": "object",
        "properties": {
            "instagram": {"type": "string", "description": f"Caption with hashtags at the end. Max {INSTAGRAM_MAX_CHARS} characters."},
            "facebook": {"type": "string", "description": "Slightly longer, conversational, 0–3 hashtags."},
            "alt_text": {"type": "string", "description": "One-sentence description of the first photo, for accessibility."},
            "note_for_owner": {"type": "string", "description": "Optional short tip for the owner (e.g. a blurry photo, a detail worth adding). Empty if none."},
        },
        "required": ["instagram", "facebook", "alt_text"],
    },
}


def write_posts(d: dict, feedback: str | None = None) -> dict:
    content = []
    for i, p in enumerate(sorted(draft_dir(d["id"]).glob("photo_*.jpg")), 1):
        content += [{"type": "text", "text": f"Photo {i}:"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": small_b64(p.read_bytes())}}]
    request = (f"Owner's notes about this job:\n{d['notes'] or '(no notes, describe what you see)'}\n\n"
               f"Write the posts in: {LANGUAGES[d['language']]}.")
    if feedback and d.get("posts"):
        request += (f"\n\nYour previous version:\n{json.dumps(d['posts'], ensure_ascii=False)}"
                    f"\n\nThe owner wants these changes: {feedback}")
    content.append({"type": "text", "text": request})

    system = f"""You are Jackson, the social media writer for Renovate Builders, a renovation company.
You turn photos of renovation jobs and the owner's quick notes into posts that win new clients.
Look carefully at the photos and describe real, visible details. Only state facts that are visible
in the photos or written in the notes. If the photos show before/after, say so.
Follow the brand guide exactly.

<brand_guide>
{BRAND}
</brand_guide>"""

    resp = client.messages.create(model=MODEL, max_tokens=3000, system=system, tools=[WRITE_TOOL],
                                  tool_choice={"type": "tool", "name": "write_posts"},
                                  messages=[{"role": "user", "content": content}])
    posts = dict(next(b.input for b in resp.content if b.type == "tool_use"))
    posts["instagram"] = posts["instagram"][:INSTAGRAM_MAX_CHARS]
    posts.setdefault("note_for_owner", "")
    return posts


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
    """Ask Meta whether the token is valid and when it expires (0 = never)."""
    try:
        info = graph("GET", "debug_token", token, input_token=token).get("data", {})
    except MetaError:
        return {}
    return info


def maybe_extend(token: str) -> str:
    """Turn a short (1–2 hour) test token into a 60-day one, if the app id/secret are configured."""
    info = token_info(token)
    exp = info.get("expires_at", 0)
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


def wait_ready(container_id: str, token: str, timeout: int = 120):
    start = time.time()
    while time.time() - start < timeout:
        status = graph("GET", container_id, token, fields="status_code").get("status_code")
        if status in ("FINISHED", "PUBLISHED"):
            return
        if status in ("ERROR", "EXPIRED"):
            raise MetaError(f"Instagram couldn't process the photo ({status}). Try a different photo.")
        time.sleep(3)
    raise MetaError("Instagram took too long to process the photos. Try again in a few minutes.")


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
            d["errors"]["setup"] = "PUBLIC_URL isn't set on the server, so Meta can't download the photos."
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
        return d


claim_lock = threading.Lock()


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
    for f in (DATA / "drafts").glob("*/draft.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if d.get("status") == "scheduled" and datetime.fromisoformat(d["schedule_at"]).replace(tzinfo=TZ) <= now():
            if claim(d["id"], ("scheduled",)):
                run_publish(d["id"])


def scheduler_loop():
    """Every 30 seconds, publish scheduled posts whose time has come."""
    while True:
        try:
            publish_due_posts()
        except Exception:
            traceback.print_exc()
        time.sleep(30)


@asynccontextmanager
async def lifespan(_app):
    (DATA / "drafts").mkdir(exist_ok=True)
    # anything stuck mid-publish when the server restarted goes back to "scheduled"
    for f in (DATA / "drafts").glob("*/draft.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("status") == "publishing":
            d["status"] = "scheduled"; save_draft(d)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    yield


app = FastAPI(title="Jackson posting agent", lifespan=lifespan)


# ---------------------------------------------------------------------------
# API: drafts
# ---------------------------------------------------------------------------

@app.post("/api/drafts", dependencies=[Depends(require_login)])
async def create_draft(photos: list[UploadFile] = File(...), notes: str = Form(""), language: str = Form("en")):
    if not photos:
        raise HTTPException(400, "Add at least one photo.")
    if len(photos) > MAX_PHOTOS:
        raise HTTPException(400, f"Up to {MAX_PHOTOS} photos per post.")
    draft_id = uuid.uuid4().hex[:12]
    d_path = DATA / "drafts" / draft_id
    d_path.mkdir(parents=True)
    try:
        for i, f in enumerate(photos, 1):
            (d_path / f"photo_{i:02d}.jpg").write_bytes(prepare_photo(await f.read()))
    except Exception:
        raise HTTPException(400, "One of the files isn't a photo Jackson can read (use JPG, PNG or HEIC).")
    d = {"id": draft_id, "media_key": secrets.token_urlsafe(16), "created": now().isoformat(timespec="minutes"),
         "notes": notes.strip()[:3000], "language": language if language in LANGUAGES else "en",
         "status": "draft", "photo_count": len(photos), "platforms": list(PLATFORMS)}
    try:
        d["posts"] = write_posts(d)
    except anthropic.APIError as e:
        raise HTTPException(502, f"Jackson couldn't write the post right now ({e.__class__.__name__}). Try again.")
    save_draft(d)
    return d


class RegenerateIn(BaseModel):
    feedback: str = ""


@app.post("/api/drafts/{draft_id}/regenerate", dependencies=[Depends(require_login)])
def regenerate(draft_id: str, body: RegenerateIn):
    d = load_draft(draft_id)
    if d["status"] not in ("draft", "error"):
        raise HTTPException(409, "This post was already sent.")
    try:
        d["posts"] = write_posts(d, body.feedback.strip()[:1000] or "Write a fresh alternative version.")
    except anthropic.APIError as e:
        raise HTTPException(502, f"Jackson couldn't rewrite the post right now ({e.__class__.__name__}).")
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
    if d["status"] in ("published", "publishing"):
        raise HTTPException(409, "This post was already sent.")
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
        when = datetime.fromisoformat(body.schedule_at).replace(tzinfo=TZ)
        if when <= now():
            raise HTTPException(400, "The scheduled time is in the past.")
        d["status"], d["schedule_at"] = "scheduled", when.strftime("%Y-%m-%dT%H:%M")
        save_draft(d)
        return d
    save_draft(d)
    if not claim(draft_id, ("draft", "error")):
        raise HTTPException(409, "This post is already being sent.")
    return run_publish(draft_id)


@app.post("/api/drafts/{draft_id}/unschedule", dependencies=[Depends(require_login)])
def unschedule(draft_id: str):
    d = load_draft(draft_id)
    if d["status"] != "scheduled":
        raise HTTPException(409, "This post isn't scheduled.")
    d["status"], d["schedule_at"] = "draft", None
    save_draft(d)
    return d


@app.get("/api/drafts/{draft_id}", dependencies=[Depends(require_login)])
def get_draft(draft_id: str):
    return load_draft(draft_id)


@app.get("/api/history", dependencies=[Depends(require_login)])
def history():
    items = []
    for f in (DATA / "drafts").glob("*/draft.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        items.append({k: d.get(k) for k in ("id", "created", "status", "notes", "platforms", "schedule_at", "published_at")}
                     | {"preview": (d.get("posts") or {}).get("instagram", "")[:140]})
    return sorted(items, key=lambda x: x["created"], reverse=True)[:50]


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
    """Photos Meta downloads when publishing. Unguessable link, no login needed."""
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
    return {"ok": True, "agent": "Jackson", "model": MODEL, "public_url": PUBLIC_URL or None,
            "meta_connected": META_FILE.exists(), "data_dir": str(DATA)}


@app.get("/static/style.css")
def stylesheet():
    return FileResponse(BASE / "style.css", media_type="text/css")


@app.get("/connect", dependencies=[Depends(require_login)])
def connect_page():
    return FileResponse(BASE / "connect.html")


@app.get("/", dependencies=[Depends(require_login)])
def home():
    return FileResponse(BASE / "index.html")
