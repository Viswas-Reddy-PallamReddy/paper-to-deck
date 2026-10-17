#!/usr/bin/env bash
# Quick smoke test: build a narrated deck from a PDF, then serve voice Q&A.
set -e
PAPER="${1:-paper.pdf}"
LANG="${2:-en-IN}"

[ -n "$SARVAM_API_KEY" ] || { echo "export SARVAM_API_KEY first"; exit 1; }

echo "==> building deck from $PAPER ($LANG)"
python research_to_deck.py "$PAPER" --lang "$LANG" --outdir deck
echo "==> open deck/index.html in a browser"

echo "==> starting voice Q&A on http://localhost:8000"
python voice_qa.py --paper "$PAPER" --lang "$LANG" --serve
