# Paper → Narrated Deck (Sarvam APIs)

Upload a research paper, get a **self-explaining slide deck**: each slide has
beginner-friendly bullets, an optional auto-generated infographic, and
**spoken narration in an Indian language**. Then let people **talk to the paper**.

Everything is built on Sarvam APIs and **verified end-to-end** against the live
API before delivery.

```
PDF ──► text ──► Sarvam-105B outline ──► Bulbul narration ──► reveal.js deck
                                       └► (optional) Mayura translation
                          voice:  mic ──► Saaras ASR ──► 105B answer ──► Bulbul
```

## Files

| File | What it is |
|------|------------|
| `research_to_deck.py` | The main pipeline. PDF in → narrated HTML deck out. |
| `voice_qa.py` | Per-slide voice Q&A: ask about the slide you are on; get a spoken answer plus the exact excerpt. |
| `sample_deck.zip` | A real deck this code produced from a test paper — open `index.html`. |

## Repo layout

```
research_to_deck.py   main pipeline (PDF -> narrated deck)
voice_qa.py           stretch: ask the paper by voice
sample_deck.zip       a real generated deck - unzip and open index.html
requirements.txt      requests, pypdf
.env.example          copy to .env, add your key
demo.sh               one-command smoke test
```

## Setup

```bash
git clone <your-repo-url> && cd paper-to-deck
pip install -r requirements.txt
cp .env.example .env          # then edit .env and paste your key
export SARVAM_API_KEY="sk-..."   # or: set -a; source .env; set +a
```

Never commit `.env` - it is already in `.gitignore`.

## Run

```bash
# core: build a narrated deck (English)
python research_to_deck.py paper.pdf --lang en-IN --outdir deck

# same, in Hindi (the LLM writes and Bulbul speaks in Hindi)
python research_to_deck.py paper.pdf --lang hi-IN --outdir deck

# build in English, then localise via the Translate API
python research_to_deck.py paper.pdf --lang ta-IN --translate --outdir deck

# open it
open deck/index.html        # or just double-click the file
```

## Ask about a slide (voice Q&A)

The deck has a **"Ask about this slide"** button on every slide. Hold it, ask a
question out loud, and you get three things:

- a short **spoken** answer (played aloud, in your language),
- the **exact excerpt** from the paper - verbatim wording, numbers, table rows -
  shown on screen (reading a table aloud makes bad TTS, so this part is visual),
- the **source** it came from (e.g. "Section 3.2", "Table 1").

The Q&A is scoped to the slide you are on, so "what does this mean?" resolves
to the right thing. It needs the server, which serves the deck and the API
from one origin:

```bash
python voice_qa.py --paper paper.pdf --deck deck --lang en-IN --serve
# open http://localhost:8000   (serve the deck this way, not via file://)
```

CLI, for testing without a browser:

```bash
python voice_qa.py --paper paper.pdf --audio question.wav
```

## Deploy to Vercel

Vercel hosts two things: the **deck** (static, from `public/`) and the **`/ask`**
Q&A as a Python serverless function. The generator does *not* run on Vercel - it
is a long batch job - so you build the deck locally and deploy the output.

```bash
npm i -g vercel                              # once
export SARVAM_API_KEY="sk-..."
bash prepare_vercel.sh paper.pdf en-IN       # builds public/, stages api/paper.txt
vercel --prod                                # deploy
```

Then set the env var in the Vercel dashboard (Settings -> Environment Variables):

| Variable | Required | Purpose |
|----------|----------|---------|
| `SARVAM_API_KEY` | yes | auth for every Sarvam call |
| `QA_LANG` | no | answer language (default `en-IN`) |
| `PAPER_TEXT` | no | overrides `api/paper.txt` if you would rather not commit the paper text |

What maps to what:

```
public/       -> static deck served at /        (vercel.json: outputDirectory)
api/ask.py    -> POST /api/ask, rewritten from /ask
vercel.json   -> the rewrite + maxDuration: 60
```

Two limits to know:

- **Function timeout.** `/ask` does ASR + LLM + TTS in a single request. Vercel
  Hobby caps a function at 60s; if it times out, move the backend to a host that
  runs a long-lived server (Render, Railway, Fly) - `voice_qa.py` runs there
  almost unchanged - and keep the static deck on Vercel.
- **Request size.** Audio is sent base64 in the body; keep clips short (a few
  seconds) to stay well under Vercel's body limit.

## Sarvam APIs used (all confirmed working)

| Call | Endpoint | Model | Role |
|------|----------|-------|------|
| Chat | `POST /v1/chat/completions` | `sarvam-105b` | outline + slide-scoped Q&A |
| TTS | `POST /text-to-speech` | `bulbul:v3` / `bulbul:v4-flash` | slide narration |
| STT | `POST /speech-to-text` | `saaras:v4` | transcribe spoken questions |
| Translate | `POST /translate` | `mayura:v1` | localise the deck |
| Lang ID | `POST /text-lid` | — | detect language of input |

## Gotchas found while building (save yourself the debugging)

1. **`sarvam-105b` is a reasoning model.** It spends thousands of *reasoning*
   tokens before it answers. A long prompt with the default token budget returns
   `content: null` with `finish_reason: "length"`. The outline task needed
   **~14,000** completion tokens. The code sets `max_tokens=16000` (retries up to
   32000) and adds a "output only JSON" system prompt to curb over-thinking.
   If you see empty answers, this is why.
2. **STT needs an explicit MIME type.** The multipart part must be
   `("file.wav", handle, "audio/wav")` or the API replies
   `Invalid file type: None`.
3. **REST caps.** STT REST handles ≤ 30 s (use the Batch API for longer);
   TTS REST caps at 2,500 characters per request. The code truncates narration
   at 2,400 to be safe.
4. **TTS quality drops on romanised Indic.** Write Hindi/Tamil etc. in **native
   script** (`आपका OTP`), not Latin letters — the model handles code-mixing fine
   as long as each language keeps its own script.
5. **`bulbul:v3` vs `bulbul:v4-flash`.** Same request shape. v4-flash is newer
   with a persona voice catalog — if you switch, pick a v4 speaker id (e.g.
   `shubh_enhi_ads`); the v3 name `shubh` is not valid on v4. The code handles
   this automatically.
6. **Scanned PDFs.** If `pypdf` extracts almost nothing, the paper is a scan —
   route it through **Sarvam Vision** (document intelligence / OCR) first.

## 40-minute build plan

| Time | Do |
|------|-----|
| 0–5 | `export` the key, `pip install`, run `research_to_deck.py` once on any PDF to prove the APIs work. |
| 5–15 | Swap in your own deck theme / branding. Tweak the outline prompt for your paper. |
| 15–25 | Pre-generate + cache the demo paper's deck so the live demo can't stall. |
| 25–35 | Wire `voice_qa.py --serve` into the same page as the deck. |
| 35–40 | Rehearse: open deck → narrate → ask one question by voice. |

**Insurance:** every stage caches. `outline.json` and the `.wav` files are reused
on re-run, so if the network hiccups on stage you still have a full deck.

## Where to extend (name these in the pitch as roadmap)

- Voice *editing* of the deck ("add a slide on the methodology") — constrain to a
  small intent set + always confirm by voice before committing.
- Live multilingual interpretation: streaming Saaras → Bulbul for real-time dubbing.
- Voice cloning (Bulbul Instant Voice Cloning) to narrate in a chosen voice.
- "Explain simpler / go deeper" as a voice command that re-prompts and re-narrates.
