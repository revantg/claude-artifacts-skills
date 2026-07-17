---
name: publish-artifact
description: Publish a Claude Artifact (an "frame") to claude.ai from the command line, without the built-in Artifact tool. Use when the user wants to create/update/list artifacts via script, CI, or outside a Claude Code Artifact-tool turn. Wraps a stdlib-only Python CLI that calls the frame deploy API directly with the user's existing OAuth token.
---

# publish-artifact

Publish an HTML/Markdown page as a Claude Artifact straight from a shell, using the
same backend the Claude Code Artifact tool uses internally. No LLM turn required.

This was reverse-engineered from the Claude Code CLI binary (v2.1.212). See
`references/artifact-protocol.md` for the full protocol + auth writeup, and
`references/artifact-design.md` / `references/artifact-capabilities/` for the exact
contents of Anthropic's built-in artifact skills.

## Quick start

```bash
PY=~/.claude/skills/publish-artifact/publish_artifact.py

# create
python3 "$PY" page.html --title "My Page" --favicon "📊" --description "A demo"

# markdown (light client-side render; needs `pip install markdown` for full fidelity)
python3 "$PY" notes.md --title "Notes"

# update an existing artifact in place (keeps the same URL)
python3 "$PY" page.html --slug <slug-from-url> --force

# list your artifacts
python3 "$PY" --list
```

It prints the artifact URL (`https://claude.ai/code/artifact/<slug>`) to stdout.
Add `--json` to also dump the raw API response to stderr.

## How it works (one paragraph)

The CLI builds the exact HTML skeleton the app uses, then does a single
`POST https://api.anthropic.com/api/frame/deploy/direct` with the page in a
`content` field and headers `X-Frame-CP: go`, `X-Frame-Surface: code`,
`X-Frame-Platform: cli`, plus `Authorization: Bearer <oauth-token>`. The response
`{slug, version}` yields the view URL `https://claude.ai/code/artifact/<slug>`.
(The app also has a "signed"/GCS lane via `/api/frame/deploy/init` + `/complete`
for larger pages; this skill uses only the simpler direct lane, capped at 16 MB.)

## Stateless / bring-your-own-harness

This skill keeps **no state**. The only thing it needs is an OAuth token, and it
does **not** require a local Claude Code install or login as long as you supply one.
Copy just `publish_artifact.py` (stdlib-only) into any machine, container, or CI and
it works:

```bash
export CLAUDE_CODE_OAUTH_TOKEN="$(claude setup-token)"   # once, anywhere
python3 publish_artifact.py page.html --title "…" --favicon "📊"
# or fully inline, nothing in the environment:
python3 publish_artifact.py page.html --token "$MY_SECRET"
```

Only two files are ever needed at runtime: `publish_artifact.py` and the page you're
publishing. Nothing else in this skill directory is required to *run* it (the
`references/` are documentation).

## Auth — all scenarios

Full guide in **`references/auth.md`** (how to obtain/extract a token on Linux,
macOS Keychain, and CI; refresh flow; safe handling). Resolution order in
`get_token()`:

1. **`--token <tok>`** — explicit argument. Highest priority, most portable.
2. **`$CLAUDE_CODE_OAUTH_TOKEN`** env var. Best for CI/headless. Get one with
   `claude setup-token` (long-lived, purpose-built for scripting).
3. **`~/.claude/.credentials.json`** → `claudeAiOauth.accessToken` — the token an
   interactive `claude` login already stored.
4. **Expired access token** (scenario 3) → auto-refresh via
   `POST https://platform.claude.com/v1/oauth/token`
   (`grant_type=refresh_token`, `client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e`),
   rotated tokens written back so the local CLI stays valid too.
5. **No token anywhere** → exits telling you to supply `--token`/env or run `/login`.

macOS: if login stored creds in the Keychain, either export
`CLAUDE_CODE_OAUTH_TOKEN` (scenario 2) or
`security find-generic-password -s "Claude Code-credentials" -w | jq -r .claudeAiOauth.accessToken`.

## Environment overrides

| Variable | Effect |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | Bearer token used directly (highest priority) |
| `CLAUDE_CODE_ARTIFACTS_API_BASE_URL` | Override the frame API base (default `https://api.anthropic.com`) |
| `ARTIFACT_ENV=staging` | Use the staging view origin `claude-ai.staging.ant.dev` |

## Constraints (parity with the app)

- Page must be self-contained; a strict CSP on the served page blocks all external
  hosts (inline CSS/JS, data-URI images only).
- Max rendered page size **16 MB**.
- Content may not contain `</script` or mix `<!--` comments with `<script>`
  (breaks the inline bundle); the script rejects these before upload.
- Publishing declares nothing shared — artifacts start **private** to the owner.

## When to use vs. the built-in Artifact tool

Use this skill for scripted/automated/out-of-band publishing (CI, cron, a Makefile,
or from another program). For normal in-conversation artifact creation, the built-in
`Artifact` tool is better — it loads design guidance and handles capabilities/contracts.
