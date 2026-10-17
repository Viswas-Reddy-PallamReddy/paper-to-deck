"""
Paper -> Deck : full-stack web app backend (FastAPI).

Endpoints
---------
  GET  /                          the single-page app
  POST /api/generate              upload a PDF + language (+ ocr) -> {job_id}
  GET  /api/jobs/{job_id}         poll progress: {status, pct, message, stage}
  GET  /api/decks/{job_id}/deck.json   the built deck (slides + audio urls)
  GET  /api/decks/{job_id}/audio/{name}  narration wav
  POST /api/ask                   spoken question -> spoken answer + exact excerpt

Generation takes a minute or two, so it runs in a background thread and the
browser polls the job. State is in memory (fine for a demo); decks are written
under DECKS_DIR.

Run:
    export SARVAM_API_KEY="sk-..."
    uvicorn app.main:app --reload --port 8000
    # open http://localhost:8000
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import threading
import uuid

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# make the repo root importable so we reuse the exact tested pipeline
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import research_to_deck as rtd  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
DECKS = os.path.abspath(os.environ.get("DECKS_DIR", "data/decks"))
os.makedirs(DECKS, exist_ok=True)

app = FastAPI(title="Paper to Deck")

JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


def _job(job_id: str) -> dict | None:
    with LOCK:
        return JOBS.get(job_id)


def _set(job_id: str, **kw):
    with LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kw)


# ---------------------------------------------------------------------------
# generation (background thread)
# ---------------------------------------------------------------------------
def _run_job(job_id: str, pdf_path: str, lang: str, ocr: str):
    def progress(stage: str, pct: int, msg: str):
        _set(job_id, status="running", stage=stage, pct=pct, message=msg)

    try:
        outdir = os.path.join(DECKS, job_id)
        result = rtd.generate_deck(pdf_path, outdir, lang=lang, ocr=ocr,
                                   progress=progress)
        outline = result["outline"]

        slides = []
        for slide, audio in zip(outline["slides"], result["audio_files"]):
            slides.append({
                "title": slide.get("title", ""),
                "bullets": slide.get("bullets", []),
                "narration": slide.get("narration", ""),
                "visual": rtd.render_visual(slide),
                "audio": f"/api/decks/{job_id}/audio/{audio}",
            })
        deck = {
            "deck_title": outline.get("deck_title", ""),
            "deck_subtitle": outline.get("deck_subtitle", ""),
            "slides": slides,
        }
        with open(os.path.join(outdir, "deck.json"), "w", encoding="utf-8") as f:
            json.dump(deck, f, ensure_ascii=False)
        _set(job_id, status="done", pct=100, message="Ready", stage="done", deck=deck)
    except Exception as e:  # surface the failure to the browser
        _set(job_id, status="error", error=str(e), message=str(e), stage="error")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@app.post("/api/generate")
async def generate(file: UploadFile = File(...), lang: str = Form("en-IN"),
                   ocr: str = Form("auto")):
    if not rtd.API_KEY:
        raise HTTPException(500, "SARVAM_API_KEY is not set on the server")
    if lang not in rtd.LANG_NAMES:
        raise HTTPException(400, f"unsupported language: {lang}")
    if ocr not in ("auto", "always", "never"):
        raise HTTPException(400, f"unsupported ocr mode: {ocr}")

    job_id = uuid.uuid4().hex[:12]
    workdir = os.path.join(DECKS, job_id)
    os.makedirs(workdir, exist_ok=True)
    pdf_path = os.path.join(workdir, "paper.pdf")
    with open(pdf_path, "wb") as f:
        f.write(await file.read())

    with LOCK:
        JOBS[job_id] = {"status": "queued", "pct": 0, "message": "Queued",
                        "stage": "queued", "lang": lang, "ocr": ocr}
    threading.Thread(target=_run_job, args=(job_id, pdf_path, lang, ocr),
                     daemon=True).start()
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    j = _job(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    return {k: j.get(k) for k in ("status", "pct", "message", "stage", "error")}


@app.get("/api/decks/{job_id}/deck.json")
def get_deck(job_id: str):
    j = _job(job_id)
    if not j:
        raise HTTPException(404, "unknown job")
    if j.get("status") != "done":
        raise HTTPException(409, "deck not ready")
    return JSONResponse(j["deck"])


@app.get("/api/decks/{job_id}/audio/{name}")
def get_audio(job_id: str, name: str):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "bad file name")
    path = os.path.join(DECKS, job_id, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "no such audio")
    return FileResponse(path, media_type="audio/wav")


@app.post("/api/ask")
async def ask(payload: dict):
    job_id = payload.get("deck_id", "")
    j = _job(job_id)
    if not j or j.get("status") != "done":
        raise HTTPException(400, "no deck context for this question")

    paper_path = os.path.join(DECKS, job_id, "paper.txt")
    if not os.path.isfile(paper_path):
        raise HTTPException(400, "paper text missing for this deck")
    with open(paper_path, encoding="utf-8") as f:
        paper = f.read()

    lang = j.get("lang", "en-IN")
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(base64.b64decode(payload.get("audio", "")))
        tmp = f.name
    try:
        question = rtd.asr(tmp, mode="codemix").get("transcript", "")
    finally:
        os.unlink(tmp)

    if not question.strip():
        return {"transcript": "", "spoken": "I did not catch that - try again.",
                "detail": "", "source": "", "audio": ""}

    res = rtd.answer_from_paper(paper, question,
                                payload.get("slide_title", ""),
                                payload.get("slide_text", ""))
    audio = rtd.tts(res["spoken"], lang)
    return {
        "transcript": question,
        "spoken": res["spoken"],
        "detail": res["detail"],
        "source": res["source"],
        "audio": base64.b64encode(audio).decode(),
    }


@app.get("/api/languages")
def languages():
    return rtd.LANG_NAMES


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")
