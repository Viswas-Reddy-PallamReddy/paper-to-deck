"""
research_to_deck.py
===================
Turn a research paper (PDF) into a narrated, self-explaining slide deck,
built end-to-end on Sarvam APIs.

Pipeline
--------
    PDF  -> text (pypdf)
         -> structured outline           [Sarvam-105B chat]
         -> optional translation          [Mayura / Sarvam-Translate]
         -> per-slide narration audio     [Bulbul TTS]
         -> reveal.js HTML deck with inline SVG visuals + autoplay audio

Sarvam APIs used:
    POST /v1/chat/completions   sarvam-105b      outline + Q&A
    POST /text-to-speech        bulbul:v3        narration audio
    POST /translate             mayura:v1        optional localisation
    POST /text-lid              -                detect language of input

Run
---
    export SARVAM_API_KEY="sk_..."          # never hard-code the key
    pip install requests pypdf
    python research_to_deck.py paper.pdf --lang hi-IN --outdir deck
    # then open deck/index.html in a browser

Every network call is retried and every stage caches to disk, so a demo
never dies on a flaky minute.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import time
import zipfile
from string import Template

import requests

# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
BASE = "https://api.sarvam.ai"
API_KEY = os.environ.get("SARVAM_API_KEY", "")

CHAT_MODEL = "sarvam-105b"
TTS_MODEL_DEFAULT = "bulbul:v3"      # stable; "bulbul:v4-flash" is newer
TRANSLATE_MODEL = "mayura:v1"        # 11 langs; "sarvam-translate:v1" for 23

# Language codes we accept for narration (subset; extend as needed).
LANG_NAMES = {
    "en-IN": "English", "hi-IN": "Hindi", "bn-IN": "Bengali",
    "ta-IN": "Tamil", "te-IN": "Telugu", "mr-IN": "Marathi",
    "gu-IN": "Gujarati", "kn-IN": "Kannada", "ml-IN": "Malayalam",
    "pa-IN": "Punjabi", "od-IN": "Odia",
}


# ---------------------------------------------------------------------------
# tiny API client with retries
# ---------------------------------------------------------------------------
class SarvamError(RuntimeError):
    pass


def _post(path: str, *, json_body=None, files=None, data=None, headers=None,
          tries: int = 3, timeout: int = 120):
    url = f"{BASE}{path}"
    h = {"api-subscription-key": API_KEY}
    if headers:
        h.update(headers)
    last = None
    for attempt in range(1, tries + 1):
        try:
            r = requests.post(url, headers=h, json=json_body, files=files,
                              data=data, timeout=timeout)
            if r.status_code >= 500 and attempt < tries:
                last = f"{r.status_code}: {r.text[:200]}"
                time.sleep(1.5 * attempt)
                continue
            if r.status_code != 200:
                raise SarvamError(f"{path} -> {r.status_code}: {r.text[:300]}")
            return r.json()
        except (requests.RequestException, SarvamError) as e:
            last = str(e)
            if isinstance(e, SarvamError):
                raise
            if attempt < tries:
                time.sleep(1.5 * attempt)
                continue
            raise SarvamError(f"{path} failed after {tries} tries: {last}")
    raise SarvamError(f"{path} failed: {last}")


def chat(messages, *, temperature: float = 0.4, model: str = CHAT_MODEL,
         max_tokens: int = 16000) -> str:
    """Sarvam-105B chat completion -> assistant text.

    sarvam-105b is a REASONING model: it spends completion tokens thinking
    before it answers. Give it a generous max_tokens or it hits the ceiling
    mid-reasoning and returns content=None. We also fall back to
    reasoning_content and retry once if the answer comes back empty.
    """
    for attempt in range(2):
        out = _post("/v1/chat/completions", json_body={
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        })
        choice = out["choices"][0]
        msg = choice.get("message", {}) or {}
        content = msg.get("content") or msg.get("reasoning_content")
        if content:
            return content
        # empty -> likely truncated mid-reasoning; retry with more room
        max_tokens = min(max_tokens * 2, 32000)
        time.sleep(1.0)
    raise SarvamError(
        f"chat returned empty content (finish_reason={choice.get('finish_reason')}); "
        "raise max_tokens or shorten the prompt")


def translate(text: str, target: str, source: str = "en-IN") -> str:
    """Mayura translation for short strings."""
    if not text.strip():
        return text
    out = _post("/translate", json_body={
        "input": text,
        "source_language_code": source,
        "target_language_code": target,
        "model": TRANSLATE_MODEL,
    })
    return out.get("translated_text", text)


def detect_language(text: str) -> str:
    out = _post("/text-lid", json_body={"input": text[:500]})
    return out.get("language_code", "unknown")


def tts(text: str, lang: str, *, speaker: str | None = None,
        model: str = TTS_MODEL_DEFAULT) -> bytes:
    """Bulbul text-to-speech -> raw WAV bytes."""
    if not speaker:
        speaker = "shubh_enhi_ads" if model.startswith("bulbul:v4") else "shubh"
    out = _post("/text-to-speech", json_body={
        "text": text[:2400],                 # REST cap is 2500 chars
        "language_code": lang,
        "speaker": speaker,
        "model": model,
        "speech_sample_rate": 24000,
        "enable_preprocessing": True,        # normalises numbers/abbreviations
    })
    audios = out.get("audios") or []
    if not audios:
        raise SarvamError("TTS returned no audio")
    return base64.b64decode(audios[0])


def asr(audio_path: str, *, mode: str = "codemix",
        model: str = "saaras:v4") -> dict:
    """Saaras speech-to-text. Returns {transcript, language_code, ...}.

    NOTE: the multipart part MUST carry an explicit audio MIME type,
    otherwise the API rejects it with 'Invalid file type: None'.
    """
    with open(audio_path, "rb") as f:
        out = _post(
            "/speech-to-text",
            files={"file": (os.path.basename(audio_path), f, "audio/wav")},
            data={"model": model, "mode": mode},
        )
    return out


# ---------------------------------------------------------------------------
# 1. PDF -> text
# ---------------------------------------------------------------------------
def _condense(text: str, max_chars: int) -> str:
    """Fit a long paper into the context budget without losing the whole.

    Truncating a 40-page survey to its first pages produces a deck about the
    introduction only. Instead we keep the opening (title, abstract, start of
    the first section) and then sample evenly spaced passages across the rest,
    so every part of the paper is represented.
    """
    if len(text) <= max_chars:
        return text
    head = text[:3500]
    rest = text[3500:]
    budget = max(max_chars - len(head), 2000)
    n = 8
    seg = max(budget // n, 500)
    step = max(len(rest) // n, 1)
    parts = [head]
    for i in range(n):
        start = i * step
        parts.append("\n\n[...]\n\n" + rest[start:start + seg])
    return "".join(parts)[:max_chars]


# ---------------------------------------------------------------------------
# 1b. OCR / digitisation with Sarvam Vision (Document AI)
# ---------------------------------------------------------------------------
VISION_MAX_PAGES = 10        # Document AI caps a single job at 10 pages
_EMBEDDED_IMG = re.compile(r"!\[[^\]]*\]\(data:image/[^)]*\)", re.I)
_IMG_DESC = re.compile(r"^\*[^\n]*\bimage\b[^\n]*\*\s*$", re.I | re.M)


def _strip_embedded_images(text: str) -> str:
    """Drop the image payloads Vision returns: base64 data URIs and the
    fully-italic paragraphs it writes to describe a figure."""
    text = _EMBEDDED_IMG.sub("", text)
    text = _IMG_DESC.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def _digitise_bytes(data: bytes, filename: str, language: str,
                    output_format: str) -> str:
    """One Document AI job: submit -> poll -> download -> return the text."""
    key = {"api-subscription-key": API_KEY}
    r = requests.post(f"{BASE}/doc-ai/v1/job/digitise", headers=key,
                      files={"file": (filename, data, "application/pdf")},
                      data={"language": language, "output_format": output_format},
                      timeout=180)
    if r.status_code not in (200, 201):
        raise SarvamError(f"doc-ai create -> {r.status_code}: {r.text[:200]}")
    job_id = r.json()["job_id"]

    terminal = {"completed", "partially_completed", "failed", "rejected"}
    status = "pending"
    deadline = time.time() + 600
    while time.time() < deadline:
        s = requests.get(f"{BASE}/doc-ai/v1/job/{job_id}/status",
                         headers=key, timeout=60).json()
        status = str(s.get("status", "")).lower()
        if status in terminal:
            break
        time.sleep(3)
    if status not in ("completed", "partially_completed"):
        raise SarvamError(f"doc-ai job {job_id} ended as '{status}'")

    dl = requests.get(f"{BASE}/doc-ai/v1/job/{job_id}/download-url",
                      headers=key, timeout=60).json()
    z = requests.get(dl["url"], timeout=180)
    z.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    want = ".md" if output_format == "md" else ".html"
    for name in zf.namelist():
        if name.lower().endswith(want):
            return zf.read(name).decode("utf-8", "ignore")
    return ""


def extract_text_vision(pdf_path: str, *, language: str = "en-IN",
                        output_format: str = "md", progress=None) -> str:
    """Digitise a PDF with Sarvam Vision, chunked to respect the 10-page cap.

    Handles scans and digital PDFs alike, keeps reading order, and converts
    tables to Markdown - which is exactly what the downstream model needs.
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(pdf_path)
    total = len(reader.pages)
    out = []
    for start in range(0, total, VISION_MAX_PAGES):
        end = min(start + VISION_MAX_PAGES, total)
        if progress:
            progress("ocr", int(100 * start / max(total, 1)),
                     f"OCR: pages {start + 1}-{end} of {total}")
        writer = PdfWriter()
        for i in range(start, end):
            writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        out.append(_digitise_bytes(buf.getvalue(),
                                   f"pages_{start + 1}-{end}.pdf",
                                   language, output_format))
    return _strip_embedded_images("\n\n".join(out))


# ---------------------------------------------------------------------------
# 1. PDF -> text
# ---------------------------------------------------------------------------
def extract_text(pdf_path: str, max_chars: int = 24000, ocr: str = "auto",
                 progress=None) -> str:
    """Extract text from a PDF.

    ocr="auto"   - read the PDF text layer, falling back to Sarvam Vision when
                   there is almost none (i.e. the file is a scan)
    ocr="always" - always use Sarvam Vision (best for scans and heavy tables)
    ocr="never"  - read the text layer only
    """
    if pdf_path.lower().endswith(".txt"):
        with open(pdf_path, encoding="utf-8") as f:
            return _condense(f.read(), max_chars)

    if ocr == "always":
        return _condense(extract_text_vision(pdf_path, progress=progress),
                         max_chars)

    from pypdf import PdfReader

    reader = PdfReader(pdf_path)
    pages = [(p.extract_text() or "") for p in reader.pages]
    text = "\n".join(pages)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    if len(text.strip()) < 200 and ocr == "auto":
        print("  ! little text in the PDF layer - running Sarvam Vision OCR...")
        text = extract_text_vision(pdf_path, progress=progress)

    return _condense(text, max_chars)


# ---------------------------------------------------------------------------
# 2. text -> structured outline
# ---------------------------------------------------------------------------
OUTLINE_PROMPT = Template("""You are an expert science communicator making a slide deck
for a COMPLETE BEGINNER who has never heard of this topic.

Return ONLY valid JSON (no markdown fences) in exactly this shape:
{
  "deck_title": "short, catchy",
  "deck_subtitle": "one line on what this is about",
  "slides": [
    {
      "title": "...",
      "bullets": ["...", "...", "..."],
      "narration": "2-4 spoken sentences in plain language, no jargon. Written to be READ ALOUD. Define every technical term the first time it appears.",
      "visual": "none | chart | timeline | comparison | diagram",
      "visual_data": null
    }
  ]
}

For "visual_data", use the matching shape (or null when visual is "none"):
  chart      -> {"items": [{"label": "...", "value": 12}, ...]}   (3-6 items)
  timeline   -> {"steps": ["...", "...", "..."]}                  (3-5 steps)
  comparison -> {"left": {"title": "...", "points": ["..."]},
                 "right": {"title": "...", "points": ["..."]}}
  diagram    -> {"nodes": ["...", "...", "..."]}                  (2-4 nodes, a flow)

Rules:
- 6 to 8 slides.
- Slide 1 explains in one line what the paper is about.
- Prefer a concrete visual over walls of text; use charts only if the paper has real numbers.

PAPER:
---
$paper
---
""")


def _loads_json(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()
    # keep only the outermost object if the model added prose
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start:end + 1]
    return json.loads(raw)


def build_outline(paper_text: str, lang: str) -> dict:
    lang_name = LANG_NAMES.get(lang, "English")
    prompt = OUTLINE_PROMPT.substitute(paper=paper_text)
    if lang != "en-IN":
        prompt += (f"\nIMPORTANT: write ALL human-readable text (titles, bullets, "
                   f"narration, visual labels) in {lang_name}. Keep JSON keys in English.")
    raw = chat([
        {"role": "system", "content":
         "You are a JSON generator. Output only valid JSON. Do not write "
         "explanations or reasoning in the answer."},
        {"role": "user", "content": prompt},
    ], temperature=0.4)
    outline = _loads_json(raw)
    outline.setdefault("deck_title", "Research Paper Explained")
    outline.setdefault("deck_subtitle", "")
    outline.setdefault("slides", [])
    return outline


# ---------------------------------------------------------------------------
# 3. optional localisation via the Translate API
# ---------------------------------------------------------------------------
def localise(outline: dict, target: str) -> dict:
    """Translate an English outline into `target` using Mayura, field by field."""
    def tr(s):
        return translate(s, target) if isinstance(s, str) and s.strip() else s

    outline["deck_title"] = tr(outline.get("deck_title", ""))
    outline["deck_subtitle"] = tr(outline.get("deck_subtitle", ""))
    for slide in outline.get("slides", []):
        slide["title"] = tr(slide.get("title", ""))
        slide["bullets"] = [tr(b) for b in slide.get("bullets", [])]
        slide["narration"] = tr(slide.get("narration", ""))
        vd = slide.get("visual_data")
        if isinstance(vd, dict):
            if "items" in vd:
                for it in vd["items"]:
                    it["label"] = tr(it.get("label", ""))
            if "steps" in vd:
                vd["steps"] = [tr(s) for s in vd["steps"]]
            if "nodes" in vd:
                vd["nodes"] = [tr(s) for s in vd["nodes"]]
            for side in ("left", "right"):
                if side in vd:
                    vd[side]["title"] = tr(vd[side].get("title", ""))
                    vd[side]["points"] = [tr(p) for p in vd[side].get("points", [])]
    return outline


# ---------------------------------------------------------------------------
# 4. narration -> audio
# ---------------------------------------------------------------------------
def synthesise(outline: dict, lang: str, outdir: str, *,
               speaker: str | None, model: str) -> list[str]:
    files = []
    for i, slide in enumerate(outline["slides"], 1):
        name = f"slide{i:02d}.wav"
        path = os.path.join(outdir, name)
        if not os.path.exists(path):
            audio = tts(slide.get("narration", slide.get("title", "")),
                        lang, speaker=speaker, model=model)
            with open(path, "wb") as f:
                f.write(audio)
        files.append(name)
        print(f"    audio {i}/{len(outline['slides'])} -> {name}")
    return files


# ---------------------------------------------------------------------------
# 5. outline -> reveal.js HTML (with inline SVG visuals)
# ---------------------------------------------------------------------------
def _svg_chart(items: list) -> str:
    items = [it for it in items if isinstance(it, dict)][:6]
    if not items:
        return ""
    w, row_h, pad = 720, 56, 8
    mx = max((abs(it.get("value", 0)) or 1) for it in items)
    h = row_h * len(items) + pad
    parts = [f'<svg viewBox="0 0 {w} {h}" class="viz">']
    for i, it in enumerate(items):
        val = abs(it.get("value", 0))
        bw = int((val / mx) * (w - 260))
        y = i * row_h + pad
        parts.append(f'<text x="0" y="{y + 22}" class="viz-label">'
                     f'{_esc(str(it.get("label", "")))[:22]}</text>')
        parts.append(f'<rect x="200" y="{y}" width="{bw}" height="30" rx="4" '
                     f'fill="#3b5bdb"/>')
        parts.append(f'<text x="{200 + bw + 8}" y="{y + 21}" '
                     f'class="viz-val">{_esc(str(it.get("value", "")))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _svg_timeline(steps: list) -> str:
    steps = [s for s in steps][:5]
    if not steps:
        return ""
    w, h = 900, 140
    n = len(steps)
    gap = w // (n + 1)
    parts = [f'<svg viewBox="0 0 {w} {h}" class="viz">',
             f'<line x1="{gap}" y1="50" x2="{w - gap}" y2="50" '
             f'stroke="#adb5bd" stroke-width="3"/>']
    for i, s in enumerate(steps):
        cx = gap * (i + 1)
        parts.append(f'<circle cx="{cx}" cy="50" r="14" fill="#3b5bdb"/>')
        parts.append(f'<text x="{cx}" y="55" class="viz-step" '
                     f'text-anchor="middle">{i + 1}</text>')
        parts.append(f'<text x="{cx}" y="90" class="viz-label" '
                     f'text-anchor="middle">{_esc(str(s))[:18]}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _svg_comparison(vd: dict) -> str:
    def col(side, x):
        d = vd.get(side, {}) or {}
        pts = "".join(f'<text x="{x + 16}" y="{120 + i * 34}" class="viz-label">'
                      f'- {_esc(str(p))[:26]}</text>'
                      for i, p in enumerate(d.get("points", [])[:4]))
        return (f'<rect x="{x}" y="40" width="400" height="260" rx="10" '
                f'fill="#f1f3f5" stroke="#ced4da"/>'
                f'<text x="{x + 16}" y="85" class="viz-title">'
                f'{_esc(str(d.get("title", "")))[:24]}</text>{pts}')

    return (f'<svg viewBox="0 0 900 320" class="viz">'
            f'{col("left", 20)}{col("right", 480)}</svg>')


def _svg_diagram(nodes: list) -> str:
    nodes = [n for n in nodes][:4]
    if not nodes:
        return ""
    w, h = 900, 120
    n = len(nodes)
    gap = w // (n + 1)
    parts = [f'<svg viewBox="0 0 {w} {h}" class="viz">']
    for i, node in enumerate(nodes):
        cx = gap * (i + 1)
        parts.append(f'<rect x="{cx - 80}" y="35" width="160" height="50" rx="8" '
                     f'fill="#e7f5ff" stroke="#3b5bdb"/>')
        parts.append(f'<text x="{cx}" y="65" class="viz-label" '
                     f'text-anchor="middle">{_esc(str(node))[:20]}</text>')
        if i < n - 1:
            parts.append(f'<text x="{cx + gap // 2}" y="66" class="viz-val" '
                         f'text-anchor="middle">&#8594;</text>')
    parts.append("</svg>")
    return "".join(parts)


def render_visual(slide: dict) -> str:
    kind = (slide.get("visual") or "none").lower()
    vd = slide.get("visual_data") or {}
    try:
        if kind == "chart" and isinstance(vd, dict):
            return _svg_chart(vd.get("items", []))
        if kind == "timeline" and isinstance(vd, dict):
            return _svg_timeline(vd.get("steps", []))
        if kind == "comparison" and isinstance(vd, dict):
            return _svg_comparison(vd)
        if kind == "diagram" and isinstance(vd, dict):
            return _svg_diagram(vd.get("nodes", []))
    except Exception:
        pass
    return ""


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


DECK_HTML = Template("""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>$title</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/reveal.js@5/dist/reveal.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/reveal.js@5/dist/theme/white.css">
<style>
  .reveal h2 { font-size: 1.5em; color: #212529; }
  .reveal .subtitle { font-size: .7em; color: #868e96; margin-top: -.4em; }
  .reveal ul { font-size: .75em; }
  .viz { width: 100%; max-height: 340px; margin-top: 18px; }
  .viz-label { font-size: 15px; fill: #343a40; font-family: sans-serif; }
  .viz-val { font-size: 15px; fill: #3b5bdb; font-weight: 600; font-family: sans-serif; }
  .viz-title { font-size: 18px; fill: #212529; font-weight: 700; font-family: sans-serif; }
  .viz-step { font-size: 14px; fill: #fff; font-weight: 700; font-family: sans-serif; }
  .narr-bar { display: flex; align-items: center; gap: 14px; margin-top: 16px; }
  .narr-bar audio { flex: 1; }
  .badge { font-size: .45em; color: #adb5bd; }
  .notes { font-size: .5em; color: #868e96; margin-top: 14px; }
  .qa { margin-top: 18px; display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
  .qa-btn { font-size: .42em; padding: 10px 18px; border-radius: 8px; border: none; background: #3b5bdb; color: #fff; cursor: pointer; }
  .qa-btn.rec { background: #e03131; }
  .qa-status { font-size: .42em; color: #868e96; }
  .qa-out { flex-basis: 100%; font-size: .42em; color: #343a40; line-height: 1.5; }
  .qa-out .q { color: #868e96; font-style: italic; }
  .qa-out .detail { margin-top: 6px; padding: 10px 12px; background: #f8f9fa; border-left: 3px solid #3b5bdb; white-space: pre-wrap; }
  .qa-out .src { color: #adb5bd; margin-top: 4px; }
</style></head>
<body>
<div class="reveal"><div class="slides">
$slides
</div></div>
<script src="https://cdn.jsdelivr.net/npm/reveal.js@5/dist/reveal.js"></script>
<script>
  Reveal.initialize({ hash: true, controls: true, progress: true });
  function playCurrent() {
    document.querySelectorAll('audio').forEach(a => { a.pause(); a.currentTime = 0; });
    var a = Reveal.getCurrentSlide().querySelector('audio');
    if (a) a.play().catch(function(){});
  }
  Reveal.on('slidechanged', playCurrent);
  Reveal.on('ready', playCurrent);
  document.addEventListener('keydown', function(e){ if (e.key === 'p') playCurrent(); });
</script>
<script>window.SLIDE_CTX = $ctx;</script>
<script>
(function(){
  var SLIDE_CTX = window.SLIDE_CTX || [];
  var rec = null, chunks = [];
  function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
  function encodeWav(samples, rate){
    var len = 0, i, j;
    for (i = 0; i < samples.length; i++) len += samples[i].length;
    var buf = new ArrayBuffer(44 + len*2), v = new DataView(buf);
    function ws(o,s){ for (var k=0;k<s.length;k++) v.setUint8(o+k, s.charCodeAt(k)); }
    ws(0,'RIFF'); v.setUint32(4,36+len*2,true); ws(8,'WAVE');
    ws(12,'fmt '); v.setUint32(16,16,true); v.setUint16(20,1,true);
    v.setUint16(22,1,true); v.setUint32(24,rate,true);
    v.setUint32(28,rate*2,true); v.setUint16(32,2,true); v.setUint16(34,16,true);
    ws(36,'data'); v.setUint32(40,len*2,true);
    var o = 44;
    for (i=0;i<samples.length;i++){ var c=samples[i];
      for (j=0;j<c.length;j++){ var x=Math.max(-1,Math.min(1,c[j]));
        v.setInt16(o, x<0 ? x*0x8000 : x*0x7fff, true); o+=2; } }
    return new Blob([buf], {type:'audio/wav'});
  }
  function b64(blob){
    return new Promise(function(res,rej){ var r=new FileReader();
      r.onload=function(){ res(r.result.split(',')[1]); }; r.onerror=rej; r.readAsDataURL(blob); });
  }
  function start(btn){
    if (rec) return;
    var status = btn.parentNode.querySelector('.qa-status');
    var out = btn.parentNode.querySelector('.qa-out');
    out.innerHTML = ''; status.textContent = 'listening...'; btn.classList.add('rec');
    navigator.mediaDevices.getUserMedia({audio:true}).then(function(stream){
      var ctx = new (window.AudioContext||window.webkitAudioContext)({sampleRate:16000});
      var src = ctx.createMediaStreamSource(stream);
      var node = ctx.createScriptProcessor(4096,1,1);
      chunks = [];
      node.onaudioprocess = function(e){ chunks.push(new Float32Array(e.inputBuffer.getChannelData(0))); };
      src.connect(node); node.connect(ctx.destination);
      rec = {stream:stream, src:src, node:node};
    }).catch(function(){ status.textContent='mic blocked'; btn.classList.remove('rec'); });
  }
  function stop(btn){
    if (!rec) return;
    var r = rec; rec = null;
    r.node.disconnect(); r.src.disconnect();
    r.stream.getTracks().forEach(function(t){ t.stop(); });
    btn.classList.remove('rec');
    var status = btn.parentNode.querySelector('.qa-status');
    var out = btn.parentNode.querySelector('.qa-out');
    status.textContent = 'thinking...';
    var sec = btn.closest('section');
    var idx = parseInt(sec.getAttribute('data-idx'), 10);
    var c = (idx >= 0 && SLIDE_CTX[idx]) ? SLIDE_CTX[idx] : {title:'', text:''};
    var wav = encodeWav(chunks, 16000);
    b64(wav).then(function(audio){
      return fetch('/ask', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({audio:audio, slide_index:idx, slide_title:c.title, slide_text:c.text})});
    }).then(function(res){ return res.json(); }).then(function(j){
      if (j.error){ status.textContent='error'; out.textContent=j.error; return; }
      status.textContent='';
      var html = '<div class="q">You asked: ' + esc(j.transcript) + '</div>';
      html += '<div class="a">' + esc(j.spoken) + '</div>';
      if (j.detail) html += '<div class="detail">' + esc(j.detail) + '</div>';
      if (j.source) html += '<div class="src">Source: ' + esc(j.source) + '</div>';
      out.innerHTML = html;
      if (j.audio) new Audio('data:audio/wav;base64,' + j.audio).play();
    }).catch(function(e){ status.textContent='failed'; out.textContent=String(e); });
  }
  function wire(){
    var btns = document.querySelectorAll('.qa-btn');
    for (var i=0;i<btns.length;i++){ (function(btn){
      btn.addEventListener('mousedown', function(){ start(btn); });
      btn.addEventListener('mouseup', function(){ stop(btn); });
      btn.addEventListener('mouseleave', function(){ stop(btn); });
      btn.addEventListener('touchstart', function(e){ e.preventDefault(); start(btn); });
      btn.addEventListener('touchend', function(e){ e.preventDefault(); stop(btn); });
    })(btns[i]); }
  }
  if (document.readyState !== 'loading') wire();
  else document.addEventListener('DOMContentLoaded', wire);
})();
</script>
</body></html>
""")

SLIDE_HTML = Template("""  <section data-idx="$idx">
    <h2>$title</h2>
    <ul>$bullets</ul>
    $visual
    <div class="narr-bar">
      <audio controls src="$audio"></audio>
      <span class="badge">narrated</span>
    </div>
    <div class="qa">
      <button class="qa-btn" type="button">Ask about this slide</button>
      <span class="qa-status"></span>
      <div class="qa-out"></div>
    </div>
  </section>
""")

TITLE_SLIDE = Template("""  <section data-idx="-1">
    <h2>$title</h2>
    <p class="subtitle">$subtitle</p>
    <div class="narr-bar">
      <audio controls src="$audio"></audio>
      <span class="badge">narrated</span>
    </div>
  </section>
""")


def render_html(outline: dict, audio_files: list[str], out_path: str):
    blocks = []
    ctx = []  # per-slide context handed to the voice Q&A widget

    # title slide (reuses slide 1's audio if there is one)
    first_audio = audio_files[0] if audio_files else ""
    blocks.append(TITLE_SLIDE.substitute(
        title=_esc(outline.get("deck_title", "")),
        subtitle=_esc(outline.get("deck_subtitle", "")),
        audio=first_audio,
    ))

    for i, (slide, audio) in enumerate(zip(outline["slides"], audio_files)):
        bullets = "".join(f"<li>{_esc(b)}</li>" for b in slide.get("bullets", []))
        blocks.append(SLIDE_HTML.substitute(
            idx=str(i),
            title=_esc(slide.get("title", "")),
            bullets=bullets,
            visual=render_visual(slide),
            audio=audio,
        ))
        ctx.append({
            "title": slide.get("title", ""),
            "text": (slide.get("title", "") + "\n"
                     + " ".join(slide.get("bullets", [])) + "\n"
                     + slide.get("narration", "")),
        })

    ctx_json = json.dumps(ctx, ensure_ascii=False).replace("</", "<\\/")
    html = DECK_HTML.substitute(
        title=_esc(outline.get("deck_title", "Deck")),
        slides="\n".join(blocks),
        ctx=ctx_json,
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)


# ---------------------------------------------------------------------------
# Q&A over the paper (shared by the web app and the local server)
# ---------------------------------------------------------------------------
QA_SYSTEM = """You are a tutor helping a beginner understand a research paper.
You are given the FULL PAPER and the SLIDE the user is currently looking at.

Answer the user's question using ONLY the paper. Return ONLY valid JSON:
{
  "spoken": "2 to 4 short sentences in plain language, no jargon. This is READ ALOUD to the user.",
  "detail": "the EXACT supporting text from the paper: verbatim sentences, exact numbers, table rows, or figure values. Plain text, no markdown. Use \\n between lines. Empty string if there is nothing exact.",
  "source": "where in the paper this came from, e.g. 'Abstract', 'Section 3.2', 'Table 1'. Empty string if unknown."
}

If the user asks for an exact table, phrase, or number, put it verbatim in
"detail" and keep "spoken" a one-line pointer to it.
If the answer is not in the paper, say so honestly in "spoken"."""


def answer_from_paper(paper: str, question: str, slide_title: str = "",
                      slide_text: str = "") -> dict:
    """Answer a question grounded in the paper + the current slide.

    Returns {spoken, detail, source}: a short answer to read aloud, the exact
    excerpt to show on screen, and where it came from.
    """
    user = (f"FULL PAPER:\n{paper}\n\nCURRENT SLIDE:\n"
            f"Title: {slide_title or '(none)'}\n"
            f"Content: {slide_text or '(none)'}\n\n"
            f"USER QUESTION: {question}")
    raw = chat([
        {"role": "system", "content": QA_SYSTEM},
        {"role": "user", "content": user},
    ], temperature=0.3, max_tokens=16000)
    try:
        out = _loads_json(raw)
    except Exception:
        out = {"spoken": raw.strip(), "detail": "", "source": ""}
    for k in ("spoken", "detail", "source"):
        out.setdefault(k, "")
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def generate_deck(paper_path: str, outdir: str, *, lang: str = "en-IN",
                  speaker: str | None = None, model: str = TTS_MODEL_DEFAULT,
                  translate: bool = False, ocr: str = "auto",
                  max_chars: int = 12000, progress=None) -> dict:
    """Run the full pipeline, optionally reporting progress.

    progress(stage: str, pct: int, message: str) is called at each step, so a
    web backend can stream status to the browser. Returns the outline plus the
    audio file names.
    """
    def say(stage, pct, msg):
        if progress:
            progress(stage, pct, msg)

    os.makedirs(outdir, exist_ok=True)

    say("extract", 5, "Reading the PDF...")
    paper = extract_text(paper_path, max_chars=max_chars, ocr=ocr, progress=progress)
    # keep the raw text next to the deck - the Q&A endpoint uses it as context
    with open(os.path.join(outdir, "paper.txt"), "w", encoding="utf-8") as f:
        f.write(paper)
    say("extract", 15, f"{len(paper)} characters of text")

    say("outline", 25, "Structuring the deck with Sarvam-105B...")
    outline = build_outline(paper, "en-IN" if translate else lang)

    if translate and lang != "en-IN":
        say("translate", 40, f"Localising to {lang}...")
        outline = localise(outline, lang)

    with open(os.path.join(outdir, "outline.json"), "w", encoding="utf-8") as f:
        json.dump(outline, f, ensure_ascii=False, indent=2)

    slides = outline["slides"]
    total = max(len(slides), 1)
    say("narrate", 45, f"Narrating {len(slides)} slides with Bulbul...")
    audio_files = []
    for i, slide in enumerate(slides, 1):
        name = f"slide{i:02d}.wav"
        path = os.path.join(outdir, name)
        if not os.path.exists(path):
            audio = tts(slide.get("narration", slide.get("title", "")),
                        lang, speaker=speaker, model=model)
            with open(path, "wb") as f:
                f.write(audio)
        audio_files.append(name)
        say("narrate", 45 + int(45 * i / total), f"Narrated slide {i} of {len(slides)}")

    say("render", 92, "Rendering the deck...")
    render_html(outline, audio_files, os.path.join(outdir, "index.html"))
    say("done", 100, "Ready")
    return {"outline": outline, "audio_files": audio_files, "paper": paper}


def main():
    ap = argparse.ArgumentParser(description="Research paper -> narrated deck")
    ap.add_argument("pdf", help="path to the paper (.pdf or .txt)")
    ap.add_argument("--lang", default="en-IN", choices=sorted(LANG_NAMES),
                    help="narration language (default en-IN)")
    ap.add_argument("--speaker", default=None, help="Bulbul speaker/voice id")
    ap.add_argument("--model", default=TTS_MODEL_DEFAULT,
                    help="TTS model (bulbul:v3 or bulbul:v4-flash)")
    ap.add_argument("--outdir", default="deck")
    ap.add_argument("--translate", action="store_true",
                    help="build in English, then localise via the Translate API")
    ap.add_argument("--ocr", choices=["auto", "always", "never"], default="auto",
                    help="Sarvam Vision OCR: auto falls back on scanned PDFs")
    ap.add_argument("--max-chars", type=int, default=12000,
                    help="how much paper text to feed the model (default 12000)")
    args = ap.parse_args()

    if not API_KEY:
        sys.exit("Set SARVAM_API_KEY first (get one at dashboard.sarvam.ai).")

    def _p(stage, pct, msg):
        print(f"[{pct:3d}%] {msg}")

    generate_deck(args.pdf, args.outdir, lang=args.lang, speaker=args.speaker,
                  model=args.model, translate=args.translate, ocr=args.ocr,
                  max_chars=args.max_chars, progress=_p)
    print(f"      open: {os.path.join(args.outdir, 'index.html')}")


if __name__ == "__main__":
    main()
