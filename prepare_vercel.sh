#!/usr/bin/env bash
# Stage a Vercel deployment: build the deck into public/ and stage the paper
# text that the /ask serverless function reads.
set -e

PAPER="${1:-paper.pdf}"
LANG="${2:-en-IN}"

[ -n "$SARVAM_API_KEY" ] || { echo "export SARVAM_API_KEY first"; exit 1; }

echo "==> building deck into public/"
rm -rf public && mkdir -p public
python research_to_deck.py "$PAPER" --lang "$LANG" --outdir public

echo "==> staging paper text for the /ask function"
cp public/paper.txt api/paper.txt

cat <<'EOT'

Staged. Next:

  npm i -g vercel          # once
  vercel                   # preview deploy (first run links the project)
  vercel --prod            # production

Then set env vars in the Vercel dashboard (Settings -> Environment Variables):

  SARVAM_API_KEY = sk-...     # required
  QA_LANG        = en-IN      # optional; answer language
  PAPER_TEXT     = ...        # optional; overrides api/paper.txt if you
                              #   prefer not to commit the paper's text

Note: the deck is served from public/; /ask is rewritten to /api/ask.
EOT
