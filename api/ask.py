"""
Vercel serverless function: POST /ask  (rewritten from /api/ask).

Self-contained on purpose - Vercel bundles this file plus api/paper.txt, and
keeping the Sarvam calls inline avoids cross-directory import problems.

The paper text is bundled at api/paper.txt (copied there by prepare_vercel.sh).
Set the key in the Vercel dashboard:  SARVAM_API_KEY = sk-...
"""

import base64
import json
import os
import re
import tempfile
from http.server import BaseHTTPRequestHandler

import requests

BASE = "https://api.sarvam.ai"
KEY = os.environ.get("SARVAM_API_KEY", "")
LANG = os.environ.get("QA_LANG", "en-IN")
ASR_MODEL = "saaras:v4"
CHAT_MODEL = "sarvam-105b"
TTS_MODEL = "bulbul:v3"

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_paper():
    path = os.path.join(HERE, "paper.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read()[:24000]
    return ""


# PAPER_TEXT env var (if set) wins, so a Git-based deploy can avoid
# committing the paper's text; otherwise fall back to the bundled file.
PAPER = os.environ.get("PAPER_TEXT") or _load_paper()

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


def _post(path, json_body=None, files=None, data=None):
    headers = {"api-subscription-key": KEY}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    r = requests.post(f"{BASE}{path}", headers=headers, json=json_body,
                      files=files, data=data, timeout=55)
    r.raise_for_status()
    return r.json()


def asr(audio_path):
    with open(audio_path, "rb") as f:
        out = _post("/speech-to-text",
                    files={"file": (os.path.basename(audio_path), f, "audio/wav")},
                    data={"model": ASR_MODEL, "mode": "codemix"})
    return out.get("transcript", "")


def answer(question, slide_title, slide_text):
    user = (f"FULL PAPER:\n{PAPER}\n\nCURRENT SLIDE:\nTitle: {slide_title or '(none)'}\n"
            f"Content: {slide_text or '(none)'}\n\nUSER QUESTION: {question}")
    out = _post("/v1/chat/completions", json_body={
        "model": CHAT_MODEL,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": user}],
        "temperature": 0.3,
        "max_tokens": 6000,          # kept modest to fit the function timeout
    })
    raw = out["choices"][0]["message"].get("content") or ""
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip()
    s, e = raw.find("{"), raw.rfind("}")
    if s != -1 and e != -1:
        raw = raw[s:e + 1]
    try:
        res = json.loads(raw)
    except Exception:
        res = {"spoken": raw.strip(), "detail": "", "source": ""}
    for k in ("spoken", "detail", "source"):
        res.setdefault(k, "")
    return res


def tts(text):
    out = _post("/text-to-speech", json_body={
        "text": (text or "")[:2400], "language_code": LANG,
        "speaker": "shubh", "model": TTS_MODEL,
        "speech_sample_rate": 24000, "enable_preprocessing": True,
    })
    audios = out.get("audios") or []
    return base64.b64decode(audios[0]) if audios else b""


class handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(base64.b64decode(req.get("audio", "")))
                path = f.name
            try:
                q = asr(path)
            finally:
                os.unlink(path)

            if not q.strip():
                return self._send(200, {"transcript": "",
                                        "spoken": "I did not catch that - try again.",
                                        "detail": "", "source": "", "audio": ""})

            res = answer(q, req.get("slide_title", ""), req.get("slide_text", ""))
            audio = tts(res["spoken"])
            return self._send(200, {
                "transcript": q, "spoken": res["spoken"],
                "detail": res["detail"], "source": res["source"],
                "audio": base64.b64encode(audio).decode(),
            })
        except Exception as e:
            return self._send(500, {"error": str(e)})
