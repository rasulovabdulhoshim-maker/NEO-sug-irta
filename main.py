"""AI video montaj sayti — FastAPI serveri."""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import ai, editing

log = logging.getLogger("montaj")
logging.basicConfig(level=logging.INFO)

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR.parent / "data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "500"))
MAX_ASSET_MB = int(os.getenv("MAX_ASSET_MB", "50"))
MAX_VIDEO_MIN = float(os.getenv("MAX_VIDEO_MIN", "20"))
RETENTION_HOURS = float(os.getenv("RETENTION_HOURS", "24"))
MAX_JOBS = int(os.getenv("MAX_JOBS", "2"))
UPLOADS_PER_HOUR = int(os.getenv("UPLOADS_PER_HOUR", "10"))
JOBS_PER_HOUR = int(os.getenv("JOBS_PER_HOUR", "60"))
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
TRUST_PROXY = os.getenv("TRUST_PROXY", "") == "1"

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

ID_RE = re.compile(r"^[0-9a-f]{32}$")
app = FastAPI(title="AI Video Montaj")
pool = ThreadPoolExecutor(max_workers=MAX_JOBS)
_lock = threading.RLock()


# --------------------------------------------------------------------------
# Parol himoyasi (ixtiyoriy) va cheklovlar
# --------------------------------------------------------------------------

@app.middleware("http")
async def password_gate(request: Request, call_next):
    if APP_PASSWORD:
        ok = False
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                _, _, pw = base64.b64decode(auth[6:]).decode().partition(":")
                ok = secrets.compare_digest(pw, APP_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response("Parol kerak", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Video montaj"'})
    return await call_next(request)


_hits: dict[str, list[float]] = {}


def client_ip(request: Request) -> str:
    if TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def rate_limit(request: Request, bucket: str, limit: int) -> None:
    key = f"{bucket}:{client_ip(request)}"
    now = time.time()
    with _lock:
        hits = [t for t in _hits.get(key, []) if now - t < 3600]
        if len(hits) >= limit:
            raise HTTPException(429, "Juda ko'p so'rov. Bir ozdan keyin urinib ko'ring.")
        hits.append(now)
        _hits[key] = hits


# --------------------------------------------------------------------------
# Loyiha ombori
# --------------------------------------------------------------------------

def pdir(pid: str) -> Path:
    if not ID_RE.match(pid):
        raise HTTPException(404, "Loyiha topilmadi.")
    d = DATA_DIR / pid
    if not d.is_dir():
        raise HTTPException(404, "Loyiha topilmadi yoki muddati tugagan.")
    return d


def load(pid: str) -> dict:
    d = pdir(pid)
    with _lock:
        return json.loads((d / "project.json").read_text())


def save(pid: str, proj: dict) -> None:
    with _lock:
        tmp = DATA_DIR / pid / "project.json.tmp"
        tmp.write_text(json.dumps(proj, ensure_ascii=False))
        tmp.replace(DATA_DIR / pid / "project.json")


def update(pid: str, **fields) -> dict:
    with _lock:
        proj = load(pid)
        proj.update(fields)
        save(pid, proj)
        return proj


def public(proj: dict) -> dict:
    return {k: v for k, v in proj.items() if k not in ("files",)}


def version_path(pid: str, proj: dict, n: int) -> Path:
    for v in proj["versions"]:
        if v["n"] == n:
            return DATA_DIR / pid / v["file"]
    raise HTTPException(404, "Bunday versiya yo'q.")


async def save_upload(up: UploadFile, dest: Path, max_mb: int) -> None:
    limit = max_mb * 1024 * 1024
    size = 0
    with dest.open("wb") as f:
        while chunk := await up.read(1024 * 1024):
            size += len(chunk)
            if size > limit:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"Fayl juda katta (ruxsat: {max_mb} MB).")
            f.write(chunk)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.post("/api/projects")
async def create_project(request: Request, file: UploadFile = File(...)):
    rate_limit(request, "upload", UPLOADS_PER_HOUR)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in VIDEO_EXT:
        raise HTTPException(400, "Faqat video fayl yuklang (mp4, mov, mkv, webm, avi).")
    pid = uuid.uuid4().hex
    d = DATA_DIR / pid
    d.mkdir(parents=True)
    src = d / f"v0{ext}"
    try:
        await save_upload(file, src, MAX_UPLOAD_MB)
        info = editing.probe(src)
        if info.duration > MAX_VIDEO_MIN * 60:
            raise HTTPException(413, f"Video juda uzun (ruxsat: {MAX_VIDEO_MIN:g} daqiqa).")
    except editing.EditError:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, "Bu fayl video sifatida o'qilmadi.")
    except HTTPException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    proj = {
        "id": pid, "created": time.time(), "status": "idle", "progress": None, "error": None,
        "versions": [{"n": 0, "file": src.name, "label": "Asl video",
                      "notes": [], "duration": round(info.duration, 1)}],
        "history": [], "has_logo": False, "has_music": False,
        "filename": file.filename, "has_audio": info.has_audio,
    }
    save(pid, proj)
    return public(proj)


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    return public(load(pid))


@app.post("/api/projects/{pid}/assets")
async def upload_asset(pid: str, request: Request, kind: str = Form(...),
                       file: UploadFile = File(...)):
    rate_limit(request, "upload", UPLOADS_PER_HOUR * 3)
    d = pdir(pid)
    ext = Path(file.filename or "").suffix.lower()
    if kind == "logo" and ext in IMAGE_EXT:
        pass
    elif kind == "music" and ext in AUDIO_EXT:
        pass
    else:
        raise HTTPException(400, "Logo uchun rasm (png/jpg), musiqa uchun audio (mp3/wav/m4a) yuklang.")
    for old in d.glob(f"{kind}.*"):
        old.unlink()
    await save_upload(file, d / f"{kind}{ext}", MAX_ASSET_MB)
    return public(update(pid, **{f"has_{kind}": True}))


class AutoOptions(BaseModel):
    silence: bool = True
    normalize: bool = True
    subtitles: bool = True
    language: Optional[str] = None
    fade: bool = True
    music: bool = False
    logo: bool = False
    aspect: Optional[str] = None
    aspect_mode: str = "crop"
    base: Optional[int] = None


class ChatIn(BaseModel):
    message: str
    base: Optional[int] = None


def _start_job(pid: str, request: Request, kind: str, payload: dict, base: Optional[int]) -> dict:
    rate_limit(request, "job", JOBS_PER_HOUR)
    with _lock:
        proj = load(pid)
        if proj["status"] in ("queued", "processing"):
            raise HTTPException(409, "Oldingi montaj hali tugamadi. Kuting.")
        base_n = proj["versions"][-1]["n"] if base is None else base
        version_path(pid, proj, base_n)
        proj.update(status="queued", error=None, progress={"step": 0, "total": 0, "name": "navbatda"})
        save(pid, proj)
    pool.submit(run_job, pid, kind, payload, base_n)
    return public(proj)


@app.post("/api/projects/{pid}/auto")
def auto_edit(pid: str, opts: AutoOptions, request: Request):
    proj = load(pid)
    if opts.music and not proj["has_music"]:
        raise HTTPException(400, "Musiqa tanlandi, lekin musiqa fayli yuklanmagan.")
    if opts.logo and not proj["has_logo"]:
        raise HTTPException(400, "Logo tanlandi, lekin logo yuklanmagan.")
    payload = opts.model_dump(exclude={"base"})
    return _start_job(pid, request, "auto", payload, opts.base)


@app.post("/api/projects/{pid}/chat")
def chat_edit(pid: str, body: ChatIn, request: Request):
    msg = body.message.strip()
    if not msg:
        raise HTTPException(400, "Xabar bo'sh.")
    if len(msg) > 2000:
        raise HTTPException(400, "Xabar juda uzun.")
    return _start_job(pid, request, "chat", {"message": msg}, body.base)


@app.get("/api/projects/{pid}/versions/{n}/video")
def get_video(pid: str, n: int, download: int = 0):
    proj = load(pid)
    path = version_path(pid, proj, n)
    name = f"montaj_v{n}{path.suffix}"
    if download:
        return FileResponse(path, filename=name, media_type="video/mp4")
    return FileResponse(path, media_type="video/mp4")


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    shutil.rmtree(pdir(pid), ignore_errors=True)
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True, "ai": bool(os.getenv("ANTHROPIC_API_KEY"))}


# --------------------------------------------------------------------------
# Fon ishi
# --------------------------------------------------------------------------

def _asset(d: Path, kind: str) -> Optional[Path]:
    found = list(d.glob(f"{kind}.*"))
    return found[0] if found else None


def run_job(pid: str, kind: str, payload: dict, base_n: int) -> None:
    d = DATA_DIR / pid
    work = d / "work"
    try:
        proj = load(pid)
        base = version_path(pid, proj, base_n)
        info = editing.probe(base)
        history = proj["history"]
        reply = ""
        update(pid, status="processing", progress={"step": 0, "total": 0, "name": "reja tuzilmoqda"})

        if kind == "auto":
            ops = editing.auto_plan(payload)
            label = "Avto montaj"
            if not ops:
                raise editing.EditError("Hech qanday amal tanlanmadi.")
            history = history + [{"role": "user", "content": "Avto montaj: " + ", ".join(o["op"] for o in ops)}]
        else:
            msg = payload["message"]
            plan = ai.plan_edits(msg, duration=info.duration, has_audio=info.has_audio,
                                 has_logo=proj["has_logo"], has_music=proj["has_music"],
                                 history=history)
            ops, reply = plan["ops"], plan["reply"]
            label = msg[:60]
            history = history + [{"role": "user", "content": msg}]
            if not ops:
                history.append({"role": "assistant", "content": reply or "Tushunmadim, aniqroq yozing."})
                update(pid, status="idle", progress=None, history=history[-30:])
                return

        def progress(i, total, name):
            update(pid, progress={"step": i + 1, "total": total, "name": name})

        ctx = editing.Ctx(workdir=work, music=_asset(d, "music"), logo=_asset(d, "logo"))
        out, notes = editing.apply_ops(base, ops, ctx, progress)

        with _lock:
            proj = load(pid)
            n = proj["versions"][-1]["n"] + 1
            final = d / f"v{n}.mp4"
            shutil.move(str(out), final)
            dur = editing.probe(final).duration
            proj["versions"].append({"n": n, "file": final.name, "label": label,
                                     "notes": notes, "duration": round(dur, 1), "ops": ops})
            summary = (reply + "\n" if reply else "") + "\n".join("• " + x for x in notes)
            history.append({"role": "assistant", "content": summary.strip() or "Tayyor."})
            proj.update(status="idle", progress=None, error=None, history=history[-30:])
            save(pid, proj)
    except (editing.EditError, ai.AIError) as e:
        log.warning("job %s xato: %s", pid, e)
        update(pid, status="error", progress=None, error=str(e))
    except HTTPException as e:
        update(pid, status="error", progress=None, error=str(e.detail))
    except Exception:
        log.exception("job %s kutilmagan xato", pid)
        update(pid, status="error", progress=None, error="Kutilmagan xato yuz berdi. Qayta urinib ko'ring.")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def cleanup_loop() -> None:
    while True:
        try:
            cutoff = time.time() - RETENTION_HOURS * 3600
            for d in DATA_DIR.iterdir():
                if d.is_dir() and ID_RE.match(d.name) and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
        except Exception:
            log.exception("tozalashda xato")
        time.sleep(1800)


@app.on_event("startup")
def _startup() -> None:
    # server qayta yonganda "processing"da qolib ketgan loyihalarni tuzatamiz
    for d in DATA_DIR.iterdir():
        f = d / "project.json"
        if f.exists():
            try:
                p = json.loads(f.read_text())
                if p.get("status") in ("queued", "processing"):
                    p.update(status="error", progress=None,
                             error="Server qayta ishga tushdi, montajni qaytadan boshlang.")
                    f.write_text(json.dumps(p, ensure_ascii=False))
            except Exception:
                pass
    threading.Thread(target=cleanup_loop, daemon=True).start()


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
