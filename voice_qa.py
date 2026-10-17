"""
voice_qa.py
===========
STRETCH FEATURE: talk to the paper.

    mic  ->  Saaras ASR  ->  Sarvam-105B answer  ->  Bulbul TTS  ->  speaker

Two ways to run it:

  1) Browser demo (recommended for the build-a-thon)
       export SARVAM_API_KEY="sk_..."
       python voice_qa.py --paper paper.pdf --lang hi-IN --serve
     Then open http://localhost:8000 and hold the button to speak.

  2) CLI (for testing without a browser)
       python voice_qa.py --paper paper.pdf --audio question.wav

Stdlib only (http.server) so there is nothing extra to install beyond
`requests` and `pypdf`, which the main script already needs.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# reuse the verified pipeline
from research_to_deck import (
    API_KEY, asr, chat, tts, extract_text,
)

PAPER = ""
LANG = "en-IN"


def answer(question: str) -> str:
    """Answer strictly from the paper, in plain language for a beginner."""
    system = (
        "You are a friendly tutor for a complete beginner. Answer ONLY using "
        "the paper below. If the answer is not in the paper, say so honestly. "
        "Keep it short and spoken-friendly - 2 to 4 sentences, no jargon."
    )
    user = f"PAPER:\n{PAPER}\n\nQUESTION: {question}"
    return chat([
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], temperature=0.3, max_tokens=16000)


# ---------------------------------------------------------------------------
# browser UI
# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Ask the paper</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 680px; margin: 60px auto;
         padding: 0 20px; color: #212529; }
  h1 { font-size: 22px; }
  button { font-size: 17px; padding: 14px 26px; border-radius: 10px;
           border: none; background: #3b5bdb; color: #fff; cursor: pointer; }
  button:disabled { background: #adb5bd; }
  #out { margin-top: 26px; line-height: 1.55; }
  .q { color: #868e96; font-style: italic; }
  .a { margin-top: 8px; }
  .status { color: #adb5bd; font-size: 14px; margin-left: 12px; }
</style></head>
<body>
  <h1>Ask the paper</h1>
  <p>Hold the button, ask your question out loud, then release.</p>
  <button id="btn">Hold to speak</button><span class="status" id="st"></span>
  <div id="out"></div>
<script>
let ctx, stream, src, node, chunks = [], recording = false;
const btn = document.getElementById('btn');
const st = document.getElementById('st');
const out = document.getElementById('out');

function setStatus(s){ st.textContent = s; }

async function start() {
  stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
  src = ctx.createMediaStreamSource(stream);
  node = ctx.createScriptProcessor(4096, 1, 1);
  chunks = [];
  node.onaudioprocess = e => chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  src.connect(node); node.connect(ctx.destination);
  recording = true;
  setStatus('listening...');
}

function encodeWav(samples, rate) {
  const len = samples.reduce((a, c) => a + c.length, 0);
  const buf = new ArrayBuffer(44 + len * 2);
  const v = new DataView(buf);
  const ws = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  ws(0, 'RIFF'); v.setUint32(4, 36 + len * 2, true); ws(8, 'WAVE');
  ws(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true);
  v.setUint16(22, 1, true); v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  ws(36, 'data'); v.setUint32(40, len * 2, true);
  let o = 44;
  for (const c of samples) for (const s of c) {
    const x = Math.max(-1, Math.min(1, s));
    v.setInt16(o, x < 0 ? x * 0x8000 : x * 0x7fff, true); o += 2;
  }
  return new Blob([buf], { type: 'audio/wav' });
}

async function stop() {
  if (!recording) return;
  recording = false;
  node.disconnect(); src.disconnect();
  stream.getTracks().forEach(t => t.stop());
  setStatus('thinking...');
  const wav = encodeWav(chunks, 16000);
  try {
    const res = await fetch('/ask', { method: 'POST', body: wav });
    const j = await res.json();
    if (j.error) { setStatus('error'); out.innerHTML = '<b>' + j.error + '</b>'; return; }
    out.innerHTML = '<div class="q">You asked: ' + j.transcript + '</div>' +
                    '<div class="a">' + j.answer + '</div>';
    setStatus('');
    const audio = new Audio('data:audio/wav;base64,' + j.audio);
    audio.play();
  } catch (e) { setStatus('failed'); out.textContent = String(e); }
}

btn.addEventListener('mousedown', start);
btn.addEventListener('mouseup', stop);
btn.addEventListener('mouseleave', stop);
btn.addEventListener('touchstart', e => { e.preventDefault(); start(); });
btn.addEventListener('touchend', e => { e.preventDefault(); stop(); });
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        if self.path != "/ask":
            return self._send(404, b'{"error":"not found"}')
        n = int(self.headers.get("Content-Length", 0))
        data = self.rfile.read(n)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(data)
            path = f.name
        try:
            q = asr(path, mode="codemix").get("transcript", "")
            if not q.strip():
                return self._send(200, json.dumps(
                    {"transcript": "", "answer": "I did not catch that - try again.",
                     "audio": ""}).encode())
            ans = answer(q)
            audio = tts(ans, LANG)
            self._send(200, json.dumps({
                "transcript": q, "answer": ans,
                "audio": base64.b64encode(audio).decode(),
            }).encode())
        except Exception as e:  # keep the demo alive on any single failure
            self._send(500, json.dumps({"error": str(e)}).encode())
        finally:
            os.unlink(path)

    def log_message(self, *args):  # quieter logs
        pass


def main():
    global PAPER, LANG
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", required=True, help="the PDF the questions are about")
    ap.add_argument("--lang", default="en-IN")
    ap.add_argument("--serve", action="store_true", help="start the web server")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--audio", help="CLI mode: a wav file to ask")
    args = ap.parse_args()

    if not API_KEY:
        sys.exit("Set SARVAM_API_KEY first.")

    PAPER = extract_text(args.paper)
    LANG = args.lang

    if args.audio:
        q = asr(args.audio, mode="codemix").get("transcript", "")
        print("Q:", q)
        ans = answer(q)
        print("A:", ans)
        with open("answer.wav", "wb") as f:
            f.write(tts(ans, LANG))
        print("wrote answer.wav")
        return

    if args.serve:
        print(f"serving on http://localhost:{args.port}  (Ctrl+C to stop)")
        ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()
        return

    ap.error("pass --serve (browser) or --audio file.wav (CLI)")


if __name__ == "__main__":
    main()
