# AI Visibility Analyzer v2 (React frontend + Advisor)

## What's in this folder
```
backend/app/
  llm_writer.py, query_generator.py, citation_gap.py, content_advisor.py   # advisor
  blog_writer.py               # Content Studio: multi-step, streamed article writer
  listing_finder.py            # where to get listed: web search + AI + niche/region matching
  result_builder.py            # turns raw AI answers into the JSON the React app reads
  routes/analyze_v2.py         # complete /api/analyze (4 assistants, crawl, scoring, advisor)
  routes/advisor.py            # /api/queries/generate, /api/advisor/blog, /api/advisor/status ...
frontend/                      # React + Vite + TypeScript + Tailwind (replaces Streamlit)
  src/pages/NewAnalysis.tsx    # form, prompt-depth controls, prompt preview, progress
  src/pages/Dashboard.tsx      # scores, per-assistant chart, GEO breakdown, 5 tabs
  src/pages/Competitors.tsx    # side-by-side scores, ranking, share of voice
  src/pages/ContentStudio.tsx  # AI blog writer
  src/lib/types.ts             # the API contract (matches result_builder.py)
```

## Setup
1. Copy `backend/app/*` into your existing `backend/app/` (keep your crawler, models, etc.).
2. In `backend/app/main.py`, replace the old analyze router:
   ```python
   from app.routes import analyze_v2, advisor
   app.include_router(analyze_v2.router)   # instead of routes.analyze
   app.include_router(advisor.router)
   ```
3. Delete the old `frontend/streamlit_app.py` and use this `frontend/` folder.

## Run
```bash
# terminal 1 - backend
cd backend
export OPENAI_API_KEY=...        # any you have; missing ones fall back to simulated answers
export GEMINI_API_KEY=...
export PERPLEXITY_API_KEY=...    # important: Perplexity returns citations for "Where to get listed"
export ANTHROPIC_API_KEY=...     # optional, enables Claude
export WRITER_PROVIDER=openai    # which model writes recommendations and blogs (openai | gemini | anthropic)
uvicorn app.main:app --reload --port 8000

# terminal 2 - frontend (Node 18+)
cd frontend
npm install
npm run dev                      # http://localhost:5173  (/api is proxied to :8000)
```
On Windows use `set OPENAI_API_KEY=...` (cmd) or `$env:OPENAI_API_KEY="..."` (PowerShell).
Model names are env vars (OPENAI_MODEL, GEMINI_MODEL, PERPLEXITY_MODEL, CLAUDE_MODEL, WRITER_MODEL).
Check each provider's docs for current model names.

## Data modes (always labelled in the UI)
| Situation | What you see |
|---|---|
| Backend running with keys | Live results |
| Some keys missing | Live results + banner saying how many answers were simulated |
| No keys | Full pipeline on simulated answers, banner says so |
| Backend not running | Frontend demo data, banner says so |

For the review, run with real keys so the panel sees live answers.

## Using your own modules instead
`analyze_v2.py` has its own lightweight crawler and REST clients so it runs out of the box.
To use your existing `llm_clients.py`, `crawler.py` and `geo_scorer.py`, replace
`ask_provider()` and `crawl_signals()` in `analyze_v2.py`. Nothing else needs to change.

## Scoring (matches the deck)
AI Visibility = 70% mention rate + 30% citation rate + sentiment bonus (±8), computed on
discovery prompts only (prompts that never name the brand). Branded prompts are used for
sentiment only. With repeats > 1, each answer uses the majority outcome and the dashboard
shows a stability percentage.

## Content Studio (blog writer v2)
Writes a real article in five streamed steps, shown live in the UI:
1. **Reads your website** (homepage + up to 3 about/product/pricing pages) and extracts brand facts.
2. **Researches the topic** with Perplexity and keeps the source URLs (needs `PERPLEXITY_API_KEY`).
3. **Plans** an outline for the chosen article type: listicle, honest assessment, comparison, how-to or buyer's guide
   ("Auto" picks one from the question).
4. **Writes** the intro, every section and the conclusion in parallel; each section appears as soon as it's ready.
5. **Checks** keyword placement, the GEO checklist, unverified facts, and builds the FAQ JSON-LD.

The writer may only state facts found on your site or in the research; anything else becomes a highlighted
`[STAT: ...]` placeholder. Hover any section to rewrite it ("make it shorter", "add an example", or your own
instruction) and undo if needed.

A real article needs a writer key (`WRITER_PROVIDER` + its key). Without one the studio shows a labelled offline
template. Endpoints: `POST /api/advisor/blog/stream` (NDJSON events), `POST /api/advisor/blog/rewrite`.
Tune with `WRITER_MODEL` and `WRITER_WORKERS` (parallel section writers, default 4).

## Where to get listed (input-driven)
Two lists, both built from the analysis input:
- **Observed**: sites the AI assistants actually cited in this run when competitors won (live answers only;
  simulated answers never invent citations).
- **Where else to get listed**: ranked from three sources: a live Perplexity web search about where people
  research your keywords in your region, AI suggestions from the writer model (domains validated), and an
  offline niche match (baby products, food, software, health, education, travel, fashion and beauty, finance,
  local services) with India-specific or global platforms. Sites found by more than one method rank higher, and
  each card shows which methods found it and why it matters.

Brand matching also accepts spacing variants (slurrpfarm.com matches "Slurrp Farm").
