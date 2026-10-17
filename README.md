# Paper → Deck

**Turn a research paper into a narrated, multilingual slide deck — then let people ask it questions out loud.**

Paper → Deck reads a PDF research paper and produces a presentation that explains
itself: every slide is written for a beginner, illustrated with an auto-generated
visual, and narrated aloud in one of eleven Indian languages. A reader who wants
more can hold a button on any slide and ask a question by voice — the answer is
spoken back, with the exact excerpt from the original paper shown on screen.

Built on [Sarvam AI](https://www.sarvam.ai)'s language stack.

---

## Why

Research papers are written for specialists. The people who need the findings —
students, journalists, policymakers, practitioners in an adjacent field, anyone
not fluent in English — are usually the ones least able to get through them.
Most tools translate the words; they do not make the paper *understandable*.

Paper → Deck closes that gap in three ways:

- **Simplifies** — restructures the paper into beginner-friendly slides
- **Speaks** — narrates every slide in the reader's own language
- **Answers** — turns the deck into a conversation, backed by verbatim evidence

## Features

- **Narrated deck** — 6–8 slides, each with plain-language bullets and a spoken
  explanation generated with natural Indian-language voices.
- **Multilingual** — narration and answers in Hindi, Bengali, Tamil, Telugu,
  Marathi, Gujarati, Kannada, Malayalam, Punjabi, Odia, and English.
- **Automatic visuals** — the model selects a chart, timeline, comparison, or
  flow diagram for each slide and supplies its data.
- **Per-slide voice Q&A** — hold to ask a question; the answer is scoped to the
  slide you are viewing.
- **Answers with evidence** — a short spoken answer, plus the exact excerpt
  (figures, table rows, wording) and the section it came from.
- **Two interfaces** — an interactive web app, and a command-line tool for
  batch generation.

## How it works

Two loops, both built on Sarvam APIs.

**Build the deck**

```
PDF ──► extract text ──► structure into slides ──► narrate each slide ──► HTML deck
                         (Sarvam-105B)              (Bulbul TTS)
```

The model reads the paper and returns a structured outline: for each slide, a
title, beginner-level bullets, a spoken narration script, and a suggested visual
with its data. Each narration is synthesised to audio, and the deck is rendered
as a self-contained HTML page with the visuals and audio embedded.

**Talk to the deck**

```
mic ──► transcribe ──► answer from paper + current slide ──► spoken answer
        (Saaras)        (Sarvam-105B)                        (Bulbul)
                                             + exact excerpt shown on screen
```

A spoken question is transcribed, answered using the full paper and the slide in
view, and the reply is spoken back. Because the deck is a summary, the answer
also returns the verbatim supporting text from the original — so a reader can
check the exact figure or wording without leaving the slide.

## Who it helps

- **Students and newcomers** meeting a field for the first time
- **Non-English readers** who need the content in their own language
- **Researchers** skimming work outside their speciality
- **Educators** turning a paper into teaching material
- **Accessibility** — the deck can be listened to and navigated entirely by voice

## Tech stack

| Layer | Choice |
|-------|--------|
| Language, speech & reasoning | [Sarvam AI](https://www.sarvam.ai) — Sarvam-105B, Bulbul (TTS), Saaras (ASR), Mayura (translation) |
| Backend | Python, FastAPI, Uvicorn |
| Frontend | Vanilla HTML / CSS / JavaScript single-page app |
| Deck rendering | HTML with inline SVG visuals |
| PDF parsing | pypdf |

### Sarvam APIs used

| Capability | Endpoint | Model |
|------------|----------|-------|
| Chat & reasoning | `POST /v1/chat/completions` | `sarvam-105b` |
| Text to speech | `POST /text-to-speech` | `bulbul:v3` |
| Speech to text | `POST /speech-to-text` | `saaras:v4` |
| Translation | `POST /translate` | `mayura:v1` |
| Language identification | `POST /text-lid` | — |

## Getting started

**Prerequisites**

- Python 3.10 or newer
- A Sarvam AI API key from [dashboard.sarvam.ai](https://dashboard.sarvam.ai)

```bash
git clone <repo-url> && cd paper-to-deck
pip install -r requirements.txt
cp .env.example .env          # then paste your key into .env
export SARVAM_API_KEY="sk-..."
```

### Run the web app

```bash
uvicorn app.main:app --reload --port 8000
# open http://localhost:8000
```

Drop in a PDF, choose a narration language, and press **Generate deck**.
Generation runs in the background; the page shows progress and then the
finished deck, ready to read, listen to, and question.

### Run from the command line

```bash
python research_to_deck.py paper.pdf --lang en-IN --outdir deck
open deck/index.html
```

`--lang` sets the narration language, and `--translate` builds in English first
and then localises the deck through the translation API.

## API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/generate` | Upload a PDF and a language; returns a job id |
| `GET` | `/api/jobs/{id}` | Job status and progress |
| `GET` | `/api/decks/{id}/deck.json` | The generated deck |
| `GET` | `/api/decks/{id}/audio/{name}` | Slide narration audio |
| `POST` | `/api/ask` | Spoken question → spoken answer with excerpt |

## Project structure

```
app/                  full-stack web app
  main.py             FastAPI backend
  static/             single-page front-end
research_to_deck.py   generation pipeline (CLI and library)
voice_qa.py           standalone local server for Q&A
api/ask.py            serverless Q&A function
vercel.json           deployment configuration
sample_deck.zip       an example generated deck
```

## Deployment

The deck is static and the Q&A is a single request, so both run on Vercel; the
generator runs locally, or anywhere that supports longer-running jobs.

```bash
npm i -g vercel
bash prepare_vercel.sh paper.pdf en-IN
vercel --prod
```

Set `SARVAM_API_KEY` in the project's environment variables.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `SARVAM_API_KEY` | — | Sarvam API key (required) |
| `QA_LANG` | `en-IN` | Answer language for the serverless Q&A function |
| `DECKS_DIR` | `data/decks` | Where generated decks are written |

## License

Released under the MIT License — see [LICENSE](LICENSE).
