"""
voice_qa.py
===========
Ask the paper - scoped to the slide you are looking at.

    mic -> Saaras ASR -> Sarvam-105B (whole paper + current slide)
        -> Bulbul TTS  (a short SPOKEN answer)
        -> on-screen   (the EXACT excerpt / numbers / table rows + source)

Because the deck is a consolidation, people often want the original wording
back: exact tables, exact figures, exact phrasing. Reading a table aloud is
bad TTS, so the answer is split in two:

    spoken  - short, plain, read aloud
    detail  - verbatim text from the paper, shown on screen (not read aloud)
    source  - where it came from, e.g. "Section 3.2" or "Table 1"

Run
---
    export SARVAM_API_KEY="sk-..."
    python voice_qa.py --paper paper.pdf --deck deck --lang en-IN --serve
    # open http://localhost:8000  (this serves the deck AND the /ask API)

CLI
---
    python voice_qa.py --paper paper.pdf --audio question.wav
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from research_to_deck import (
    API_KEY, _loads_json, asr, chat, extract_text, tts,
)

PAPER = ""
LANG = "en-IN"
DECK_DIR = os.path.abspath("deck")

SYSTEM = """You are a tutor helping a beginner understand a research paper.
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

USER_TMPL = """FULL PAPER:
$paper

CURRENT SLIDE:
Title: $slide_title
Content: $slide_text

USER QUESTION: $question
"""


def answer(question: str, slide_title: str, slide_text: str) -> dict:
    user = USER_TMPL
    for k, v in (("$paper", PAPER), ("$slide_title", slide_title or "(none)"),
                 ("$slide_text", slide_text or "(none)"), ("$question", question)):
        user = user.replace(k, v)
    raw = chat([
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": user},
    ], temperature=0.3, max_tokens=16000)
    try:
        out = _loads_json(raw)
    except Exception:
        out = {"spoken": raw.strip(), "detail": "", "source": ""}
    out.setdefault("spoken", "")
    out.setdefault("detail", "")
    out.setdefault("source", "")
    return out


# ---------------------------------------------------------------------------
# HTTP: serves the deck statically + the /ask API
# ---------------------------------------------------------------------------
CTYPES = {
    ".html": "text/html; charset=utf-8", ".wav": "audio/wav",
    ".mp3": "audio/mpeg", ".json": "application/json",
    ".js": "text/javascript", ".css": "text/css",
    ".png": "image/png", ".svg": "image/svg+xml",
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        rel = self.path.split("?", 1)[0].lstrip("/") or "index.html"
        full = os.path.abspath(os.path.join(DECK_DIR, rel))
        if not full.startswith(DECK_DIR) or not os.path.isfile(full):
            return self._send(404, b"not found", "text/plain")
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            self._send(200, f.read(), CTYPES.get(ext, "application/octet-stream"))

    def do_POST(self):
        if self.path != "/ask":
            return self._send(404, b'{"error":"not found"}')
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            audio_b64 = req.get("audio", "")
            slide_title = req.get("slide_title", "")
            slide_text = req.get("slide_text", "")

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(base64.b64decode(audio_b64))
                path = f.name
            try:
                q = asr(path, mode="codemix").get("transcript", "")
            finally:
                os.unlink(path)

            if not q.strip():
                return self._send(200, json.dumps({
                    "transcript": "",
                    "spoken": "I did not catch that - try again.",
                    "detail": "", "source": "", "audio": ""}).encode())

            res = answer(q, slide_title, slide_text)
            audio = tts(res["spoken"], LANG)
            return self._send(200, json.dumps({
                "transcript": q,
                "spoken": res["spoken"],
                "detail": res["detail"],
                "source": res["source"],
                "audio": base64.b64encode(audio).decode(),
            }).encode())
        except Exception as e:  # keep the demo alive on any single failure
            return self._send(500, json.dumps({"error": str(e)}).encode())

    def log_message(self, *args):
        pass


def main():
    global PAPER, LANG, DECK_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", required=True, help="the PDF the questions are about")
    ap.add_argument("--deck", default="deck", help="folder with index.html + audio")
    ap.add_argument("--lang", default="en-IN")
    ap.add_argument("--serve", action="store_true", help="serve the deck + /ask API")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--audio", help="CLI mode: a wav file to ask")
    args = ap.parse_args()

    if not API_KEY:
        sys.exit("Set SARVAM_API_KEY first.")

    PAPER = extract_text(args.paper)
    LANG = args.lang
    DECK_DIR = os.path.abspath(args.deck)

    if args.audio:
        q = asr(args.audio, mode="codemix").get("transcript", "")
        print("Q:", q)
        res = answer(q, "", "")
        print("A (spoken):", res["spoken"])
        if res["detail"]:
            print("  detail:", res["detail"][:300])
        if res["source"]:
            print("  source:", res["source"])
        with open("answer.wav", "wb") as f:
            f.write(tts(res["spoken"], LANG))
        print("wrote answer.wav")
        return

    if args.serve:
        if not os.path.isfile(os.path.join(DECK_DIR, "index.html")):
            sys.exit(f"no index.html in {DECK_DIR} - build the deck first "
                     "(python research_to_deck.py paper.pdf --outdir deck)")
        print(f"deck + Q&A on http://localhost:{args.port}  (Ctrl+C to stop)")
        ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
        return

    ap.error("pass --serve (deck + browser Q&A) or --audio file.wav (CLI)")


if __name__ == "__main__":
    main()
