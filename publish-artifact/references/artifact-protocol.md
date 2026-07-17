# Claude Artifact ("frame") protocol — reverse-engineering notes

Source: Claude Code CLI binary `~/.local/share/claude/versions/2.1.212` (a Bun
single-file ELF with the JS bundle embedded, minified but not stripped). All
identifiers below (`WOu`, `KOu`, `une`, `Bol`, …) are the binary's mangled names,
kept so the findings are re-checkable. Internally, artifacts are called **frames**.

## 1. Architecture

An artifact is created by the `Artifact` **tool** (built into the CLI), not by a
skill. The tool renders a page, wraps it in an HTML skeleton, uploads it to a
backend, and gets back a private URL. Two upload lanes exist:

| Lane | Endpoint(s) | When | Notes |
|---|---|---|---|
| **direct** | `POST /api/frame/deploy/direct` | gated by `CLAUDE_CODE_ARTIFACT_DIRECT_UPLOAD` / feature flag `tengu_cobalt_plinth_direct` | page sent inline in `content`; single request. **This skill uses this lane.** |
| **signed** | `POST /api/frame/deploy/init` → PUT to a GCS signed URL → `POST /api/frame/deploy/complete` | default | for larger pages; init returns a signed upload URL, success verified via the `x-goog-generation` response header |

Other frame endpoints seen in the binary: `/api/frame/frames` (list — `action:"list"`),
`/api/frame/read/` (fetch existing), `/api/frame/contract/latest` (capability
contract), `/api/frame/track` (telemetry).

## 2. Host / base URL resolution

Host selector `m3h(e)` maps logical hosts to base URLs from the active env config `_s()`:

```
case "api":       return t.BASE_API_URL
case "claude-ai": return t.CLAUDE_AI_ORIGIN
case "frame":     return t.BASE_API_URL      // <-- frame deploys go here
case "mcp-proxy": return t.MCP_PROXY_URL
```

Prod config object `Bol`:

```
BASE_API_URL:    "https://api.anthropic.com"      // frame API base
CLAUDE_AI_ORIGIN:"https://claude.ai"              // artifact view origin
TOKEN_URL:       "https://platform.claude.com/v1/oauth/token"
CLIENT_ID:       "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
```

Env override: `CLAUDE_CODE_ARTIFACTS_API_BASE_URL` replaces the frame base.
Staging view origin: `https://claude-ai.staging.ant.dev`; local: `http://localhost:4000`.

**View URL** (`zqe`): `new URL('/code/artifact/<slug>', CLAUDE_AI_ORIGIN)` →
`https://claude.ai/code/artifact/<slug>`.

## 3. The direct-lane request (function `WOu`)

```
POST https://api.anthropic.com/api/frame/deploy/direct
Headers:
  Authorization: Bearer <oauth access token>
  Content-Type: application/json
  X-Frame-CP: go            # from une()
  X-Frame-Surface: code     # from une()
  X-Frame-Platform: cli      # une(): "desktop" if the desktop app, else "cli"
  anthropic-beta: oauth-2025-04-20   # observed necessary for OAuth on api.anthropic.com
Body (JSON):
  {
    # from KOu(): identity/meta
    "slug":        <optional, present only when updating>,
    "title":       "...",
    "favicon":     "📄",           # one/two emoji
    "label":       <optional>,      # short version label
    "description": <optional>,
    "publish_context": <optional>,
    # from JOu(): entrypoint (lowercased, <=64 chars)  -- optional
    # from QOu(): session_id (remote sessions only)     -- optional
    # from PGg(): capability/contract fields            -- optional
    "contract":    <"<version>" | null>,
    "capabilities":<{name: config}>,
    # preconditions
    "force":       true,            # overwrite without base-version check
    "baseVersion": "<version>",     # optimistic-concurrency guard (mutually exclusive with force)
    # the page
    "content":     "<!doctype html>...full document..."
  }
```

Timeout 60s; `maxBodyLength = 2 * 16MB`. On `429` it honors `retry-after` and
retries once. On `400 unknown field force/baseVersion` it retries without those
(older control planes). Success response body:

```json
{ "slug": "...", "version": "...", "read": "owner", "shared": ..., "contract": "..." }
```

`{slug, version}` are required; missing → "deploy returned incomplete response".

### Observed live responses (this session's two test publishes)
```
create:   {contract:"0.0.0", minContract:"0.0.0", preferredContract:"0.1.12",
           read:"owner", slug:"<uuid>", version:"<epoch>-<hex>"}
redeploy: same slug, new version   (with slug=<uuid> + force:true)
```

## 4. HTML skeleton (built in `UOu`, head `LGg`)

```html
<!doctype html><html[ lang="xx"]><head><meta charset=utf8>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>:root{color-scheme:light}body{margin:0;padding:0;
font:14px -apple-system,BlinkMacSystemFont,sans-serif;
background:#faf9f5;color:#141413}img{max-width:100%}</style>
</head><body>
{BODY}
</body></html>
```

`content` = this **full document** string (not just the body). For `.md` inputs the
app renders markdown server-side and may inject a mermaid runtime (~3.3MB) and/or a
highlight.js runtime (~1MB) when the body contains `<pre class="mermaid">` or fenced
code (`YOu`). This skill does a lighter client-side markdown render instead.

## 5. Limits & content sanitizer

- Max rendered page: `Yye = 16777216` bytes (**16 MB**). Over → publish rejected.
- Sanitizer `Ast` rejects a bundle that contains `</script`, or that mixes `<!--`
  with `<script` (would break the inline bundle). This skill enforces both.
- Served pages run under a strict CSP that blocks every external host — inline all
  CSS/JS, embed images as `data:` URIs.

## 6. Auth — comprehensive

The frame API is authenticated with the **OAuth access token** (`auth:"required"`,
`refreshOAuth:true`). Token storage/refresh:

- **Credential store**: `~/.claude/.credentials.json` (Linux/file) or macOS Keychain.
  JSON shape:
  ```json
  { "claudeAiOauth": {
      "accessToken": "...", "refreshToken": "...",
      "expiresAt": <epoch ms>, "refreshTokenExpiresAt": <epoch ms>,
      "scopes": [...], "subscriptionType": "...", "rateLimitTier": "..." } }
  ```
- **Env token**: `CLAUDE_CODE_OAUTH_TOKEN` (generate via `claude setup-token`).
  Highest priority; ideal for CI/headless where the file/Keychain isn't present.
- **Refresh**: when `accessToken` is expired (`now >= expiresAt`), POST to
  `https://platform.claude.com/v1/oauth/token`, `application/x-www-form-urlencoded`,
  body `grant_type=refresh_token&refresh_token=<rt>&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e`.
  Response `{access_token, refresh_token, expires_in}`. The rotated tokens should be
  written back to the credential store so the interactive CLI stays valid.
- **Scopes** granted at login include (binary `KXe`): `user:sessions:claude_code`,
  `user:mcp_servers`, `user:file_upload`, plus profile/inference scopes. The frame
  deploy accepts the standard Claude Code token — no extra scope step is needed
  (confirmed by live publish).

`publish_artifact.py` implements this exact order: env token → stored accessToken →
refresh-if-expired → clear error telling the user to `/login`.

## 7. Built-in artifact skills

Two skills back the Artifact tool. The tool's own instructions require loading
`artifact-design` before writing a page, and `artifact-capabilities` before declaring
any runtime capability.

| Skill | Ships files? | Where it lives | Extracted copy |
|---|---|---|---|
| `artifact-design` | no | embedded as a string in the CLI binary; injected inline | `references/artifact-design.md` |
| `artifact-capabilities` | yes (`*.d.ts`) | binary; extracted at load to `/tmp/claude-1000/bundled-skills/<ver>/<hash>/artifact-capabilities/` | `references/artifact-capabilities/` |

Skill registry names found in the binary (`e$e`/`rne`/…): `artifact-design`,
`artifact-capabilities`, `dataviz`, `code-review`, `code-walkthrough`, `pr-explainer`,
`verify`, `simplify`, `commit`, `pr`, `commit-push-pr`.

## 8. Re-derivation crib (how these were found)

```bash
BIN=~/.local/share/claude/versions/2.1.212
# endpoints & config
strings -n 6 "$BIN" | grep -oE '/api/frame/[a-z/]+' | sort -u
# the prod config object:
python3 -c "d=open('$BIN','rb').read(); i=d.find(b'BASE_API_URL:\"https://api.anthropic.com\"'); print(d[i-40:i+400].decode('utf-8','replace'))"
# the direct-upload function:  search for  async function WOu(
# the head skeleton:           search for  LGg=\"<style>
```
