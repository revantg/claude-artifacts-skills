# Auth & portability — getting a token in any harness

This skill is **stateless**: it stores nothing and needs no local Claude Code
install or login *as long as you hand it an OAuth token*. That token is the only
piece of state, and you supply it however your environment prefers. Everything the
frame API needs is derived from it.

## Auth priority (what the script tries, in order)

1. **`--token <tok>`** — explicit CLI argument. Highest priority. Most portable.
2. **`$CLAUDE_CODE_OAUTH_TOKEN`** — environment variable. Ideal for CI/containers.
3. **`~/.claude/.credentials.json` → `claudeAiOauth.accessToken`** — read from a
   local interactive `claude` login; auto-refreshed if expired. Convenience only;
   not required for portability.

If you want zero dependence on this machine's state, use 1 or 2 and the file in 3
is never touched.

## How to obtain a token (pick one)

### A. `claude setup-token` — best for scripting / other machines
```bash
claude setup-token            # interactive; prints a long-lived OAuth token
export CLAUDE_CODE_OAUTH_TOKEN="<printed token>"
```
Purpose-built for non-interactive use and doesn't expire like the login access
token. Copy the value to any other harness/CI secret store.

### B. From the local credentials file (Linux/Windows, file-based login)
```bash
jq -r '.claudeAiOauth.accessToken'  ~/.claude/.credentials.json   # the token
jq -r '.claudeAiOauth.expiresAt'    ~/.claude/.credentials.json   # epoch MS; short-lived
jq -r '.claudeAiOauth.refreshToken' ~/.claude/.credentials.json   # for refresh
```
This `accessToken` is short-lived — see "Refresh" below, or just let the skill read
and refresh it for you on this machine.

### C. From the macOS Keychain (Mac login)
```bash
security find-generic-password -s "Claude Code-credentials" -w \
  | jq -r '.claudeAiOauth.accessToken'
```
The Keychain entry holds the same JSON blob as the file on other platforms.

## Refresh (when a short-lived access token has expired)

`expiresAt` is epoch **milliseconds**. When `now_ms >= expiresAt`, exchange the
refresh token for a new access token:
```bash
RT=$(jq -r '.claudeAiOauth.refreshToken' ~/.claude/.credentials.json)
curl -s -X POST https://platform.claude.com/v1/oauth/token \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode grant_type=refresh_token \
  --data-urlencode "refresh_token=$RT" \
  --data-urlencode client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e
# -> { "access_token": "...", "refresh_token": "...", "expires_in": <seconds> }
```
The skill does exactly this automatically (and writes the rotated tokens back to the
credentials file so an interactive `claude` on the same machine stays valid). A token
from `setup-token` (option A) avoids needing this at all.

## Using it in another harness / CI

Because the token is the only state, portability is just "pass the token":
```bash
# one file, no install, no login:
export CLAUDE_CODE_OAUTH_TOKEN="…"           # from `claude setup-token`
python3 publish_artifact.py page.html --title "…" --favicon "📊"

# or fully inline, nothing in the environment:
python3 publish_artifact.py page.html --token "$MY_SECRET"
```
GitHub Actions / GitLab CI: store the `setup-token` output as a secret
(`CLAUDE_CODE_OAUTH_TOKEN`) and the step above runs unchanged. The only files needed
are `publish_artifact.py` and the page you're publishing.

## What the token authorizes

The frame API (`https://api.anthropic.com/api/frame/*`) accepts the standard Claude
Code OAuth token as `Authorization: Bearer <token>` alongside the frame headers
(`X-Frame-CP: go`, `X-Frame-Surface: code`, `X-Frame-Platform: cli`,
`anthropic-beta: oauth-2025-04-20`). No extra scope grant is needed — the login
scopes cover it. Full protocol in `artifact-protocol.md`.

## Handling it safely

It's a real credential — treat it like a password: don't commit it, log it, or paste
it into shared/persistent places. Prefer `setup-token` + a secret/env var over
hardcoding the short-lived file token. `--token` values may be visible in the process
list (`ps`) and shell history on a shared host; prefer the env var there.
