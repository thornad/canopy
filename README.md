# Canopy

**Cache-aware branching chat client for [oMLX](https://github.com/jundot/omlx).**

Canopy is a macOS menubar + web chat client designed around the way oMLX caches
prompts. Every turn, every branch, every regenerate — you can see at a glance
whether the prefill will hit the SSD cache or pay the full cost. Branching is
cheap because the shared prefix stays cached, and Canopy's tree view makes that
tangible instead of abstract.

> Built for local oMLX development. Pairs with oMLX ≥ 0.3.5.dev2 (requires the
> `/admin/api/cache/probe` endpoint).

## Features

- **Prompt-tree conversations** — every send, regenerate, or edit creates a
  branch. Navigate them in a live tree panel that mirrors the underlying
  message graph.
- **Per-node cache visibility** — each node in the tree shows a cache dot:
  blue if its prefix is retrievable from oMLX's SSD cache, hollow if it
  would require a full prefill. Hover for a block/token breakdown.
- **Cache-aware branching** — a new branch inherits the cached prefix up to
  the fork point for free. Canopy makes that obvious so you can branch
  aggressively without fear of expensive prefills.
- **Model-aware probing** — cache hashes are per-model. Switching models in
  the dropdown re-probes every chat so the dots reflect *what would be
  cached if you sent right now with this model*.
- **No surprise auto-loads** — Canopy never picks a model for you on
  startup. On send, it asks whether to load the chat's last-used model.
- **Branches and chats with timestamps** — sidebar entries and tree nodes
  show relative times (`2m ago`, `3h ago`, `Mar 21`) so you can tell at a
  glance what's stale.
- **Document ingestion** — drop PDFs, DOCX, or markdown into a chat to
  add them to the context.

## Install

### Option A — release DMG (recommended)

Grab `Canopy-<version>.dmg` from the [latest release](https://github.com/thornad/canopy/releases),
open it, and drag **Canopy.app** to `/Applications`.

First launch: right-click the app and choose **Open** (the build is ad-hoc
signed for development, so Gatekeeper asks once).

### Option B — run from source

```bash
git clone https://github.com/thornad/canopy
cd canopy
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

python -m canopy --port 8100 --host 127.0.0.1 \
    --db-path ~/.canopy/canopy.db
```

Then open <http://localhost:8100/chat>.

## First run

1. Start oMLX (`omlx serve --port 8000` or the oMLX.app). Canopy talks to it
   over `http://localhost:8000` by default; change that in Settings.
2. Launch Canopy. If you installed from the DMG, a tree icon appears in
   your menu bar.
3. Open **Settings** (bottom-left gear) and paste your oMLX API key if you
   have one set.
4. Click **New Chat**, type something, hit Send. Canopy will ask which
   model to use — pick one that's listed in oMLX. Subsequent sends will
   remember the choice per-chat.

## The cache dots — what they mean

Every tree node and every sidebar entry has a small colored dot next to it:

| Dot | Meaning |
|-----|---------|
| 🔵 Solid blue | Prefix is retrievable from oMLX's SSD cache — prefill will be fast |
| ⚪ Hollow | Not in the cache — full prefill cost |

Hover for the exact breakdown: `X/Y blocks cached (Z tokens)`. The dots
re-probe automatically when you load a chat, send a message, switch
branches, or change the model dropdown.

### When is something "cached"?

oMLX hashes the prompt into fixed-size blocks (default 256 tokens) using a
chain hash: each block's hash depends on the previous block plus the model
name. The cache is therefore a **contiguous prefix** — if you're 5000
tokens deep and the first 4000 are cached, your prefill is 80% free.

This has two useful implications in Canopy:

1. **Branching is cheap.** A new branch shares the entire prefix up to the
   fork point with the old branch, so the cached portion is reused 1:1.
2. **Model switching invalidates everything.** The model name is part of
   every block hash, so switching models drops the cache tier for every
   chat back to hollow until you build it up again.

## Branching

- **New branch** — click the **Branch** button under any message and type
  your next prompt. Everything before that point is shared with the
  original branch.
- **Regenerate** — click the ♻️ icon on an assistant message. Canopy
  creates a sibling answer (new branch) and switches the view to it.
- **Edit** — click ✏️ on any message to create a branch with an edited
  version of that turn.
- The tree panel auto-opens the moment you create your first branch in
  a chat. Toggle it manually with the tree icon in the header.

## Requirements

- macOS 15+ (Sequoia)
- Apple Silicon (M-series)
- Python 3.11+ if running from source
- [oMLX](https://github.com/jundot/omlx) ≥ 0.3.5.dev2 running locally

## Troubleshooting

**Dots are all hollow even for chats I just sent.** Your oMLX is older
than 0.3.5.dev2 and doesn't have the `/admin/api/cache/probe` endpoint.
Update oMLX or the probe will silently no-op.

**Dots stay "cached" after I cleared the oMLX cache.** Fixed in oMLX
≥ 0.3.5.dev2 — earlier builds used a different cache tier that survived
manual clears, giving stale results.

**Chat creation fails with "unhashable type: 'dict'".** Starlette /
Jinja2 version mismatch in an older Canopy build. Update to the latest
release.

## License

Apache 2.0. See [LICENSE](LICENSE).
