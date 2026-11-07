# Claude Artifact ("frame") protocol: reverse-engineering notes

Unofficial. Mapped from the Claude Code binary `~/.local/share/claude/versions/2.1.319`
(Mach-O, Bun single-file executable). The JS bundle is embedded as plain text after
the bytecode, minified but not stripped, so each endpoint can be read in context. The
mangled names quoted below (`Ru`, `xu`, `yvn`, `Z`, …) are from 2.1.319 and change
between builds. Internally, artifacts are called **frames**. Everything under
`/api/frame/` is a private API that can change without notice.

`artifact.py` implements the table in section 3. Section 5 explains how to re-derive it.

## 1. Transport, auth and headers

- **Base URL**: `https://api.anthropic.com` (host `"frame"` resolves to `BASE_API_URL`).
  `CLAUDE_CODE_ARTIFACTS_API_BASE_URL` overrides it. Staging uses the same API host and
  the view origin `https://claude-ai.staging.ant.dev`.
- **Auth**: `Authorization: Bearer <user OAuth access token>` (`auth:"claude-ai-oauth"`).
  The db door refuses org service keys ("db verbs require a user-scoped OAuth token, not
  an org service key"). `artifact.py` reads the token only from `CLAUDE_CODE_OAUTH_TOKEN`.
- **Headers** (`xu()` in the binary):
  `Accept: application/json, application/vnd.ant.frame-refusal+json`,
  `X-Frame-CP: go`, `X-Frame-Surface: code`, `X-Frame-Platform: cli` (`desktop` in the app),
  `X-Frame-Client-Version: <claude code version>`, optional
  `X-Frame-Session-Id: session_…|cse_…` (remote sessions only). Earlier live publishes
  also needed `anthropic-beta: oauth-2025-04-20`. `artifact.py` sends all of these except
  the client version (opt in with `ARTIFACT_CLIENT_VERSION`) and the session id.
- **Cloud relay**: inside Claude Code cloud sessions most calls go through a session
  gateway (`postRelayBound`, `getRelayOnly`, …). Locally they are plain HTTPS to the
  base URL, which is what `artifact.py` does.
- **Redirects**: the CLI uses `maxRedirects:0` for content; `artifact.py` never follows a
  redirect, so the bearer token cannot be forwarded elsewhere.
- **Retries**: the CLI retries once on 429/503 when `Retry-After` is short; `delete` also
  retries on 409.

## 2. Identifiers and URLs

- **slug**: a UUID (`gr = /^<uuid>$/`). Every endpoint takes the UUID.
- **short id**: 22 characters, the UUID as a 128-bit integer in base58 (Bitcoin
  alphabet `123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz`). For example
  `45Gkk9LXNCBdzFokZrkz6Q` is `18e41995-3c7f-4fdb-bb3e-3517d7093ec5`.
- **View URLs** the parser accepts: `https://(sub.)claude.ai/artifact/<id>`,
  `/code/artifact/<id>`, `/code/frame/<id>`, each with an optional `<vanity>-` prefix
  before the id and an optional `?sk=<share key>`; also `https://<uuid>.frame.claudeusercontent.com`.
  The app currently prints `https://claude.ai/artifact/<short id>`; older builds print
  `https://claude.ai/code/artifact/<uuid>`. Both open the same artifact.
- **Content host**: `https://<uuid>.frame.claudeusercontent.com` (staging:
  `…frame.staging.claudeusercontent.com`). A published file is served at
  `/_f/<ver>/<path>?__frame_t=<assetToken>`; the stored source (owner/writer) at
  `/_f/<ver>/_src/<path or index.html>`; an asset at `/_f/<ver>/_blob/<asset id>`.
- **Asset id**: 32 hex characters; a page references it as `/_blob/<id>`.
- **Version id** (`ver`): `/^[A-Za-z0-9_-]{1,64}$/`, e.g. `<epoch>-<hex>`. Deploy
  returns it as `version`; reads return it as `ver`.

## 3. Endpoint table

Confidence: **live** = verified with a real token (earlier session, CLI 2.1.212);
**binary** = inferred from the 2.1.319 code that builds the request and parses the
answer; **unknown** = seen, but the shape could not be pinned down. No call in this table
was re-verified live for 2.1.319: no token was available when this table was written.

### Pages

| Method + path | Body / query | Response | Conf. | CLI |
|---|---|---|---|---|
| `POST /api/frame/deploy/direct` (page lane) | `{title, favicon?, icon?, label?, description?, note?, publish_context?, slug? (update), force? or baseVersion?, contract?, capabilities?, entrypoint?, session_id?, request_id?, content: "<!doctype html>…"}`. Optional query `attempt_id=…&resend=1&create_confirmed=1` | `{slug, version, contract, minContract?, preferredContract?, capabilities?, read, shared?, type?, typeInstance?, kind?, seq?, unchanged?, replayed?}`. 409/412 = base version stale | live (page lane, 2.1.212); binary for the newer fields | `publish FILE` |
| `POST /api/frame/deploy/direct` (files lane) | same meta + `manifest: {"index.html": {content, contentType:"text/html"}, "<path>": {content, contentType} \| {sha256, contentType} \| null (remove) \| {live:false}}`, `mode: "replace" \| "patch"` (patch needs `baseVersion`) | as above | binary | `publish --file/--remove/--copy` |
| `POST /api/frame/deploy/prepare` | `{slug?, shas: [sha256 hex…], entrypoint?}`; no `slug` reserves one | `{slug, missing: [sha256…]}` | binary | staged files |
| `POST /api/frame/upload` | `{slug, files: [{path, content, contentType}]}` (text types as UTF-8 text, others base64), or copies `{slug, files: [{path, from: {slug, path, ver?}}], leave_out_missing?, cross_org_ok?}` | copies: `{files: [{sha256, contentType, size?, leftOut?}]}` | binary | staged files, `--copy` |
| `GET /api/frame/read/<slug>` | none | `{contract, capabilities?, type?}`: the stored pin and declaration (the "read-back") | binary | `info`, publish |
| `GET /api/frame/contract/latest` | none | `{version, capabilities: [names], claude?, core?, features?}` | binary | `contract`, publish |
| `GET /api/frame/contract/<ver>` | none | same shape, for that version | binary | `contract <ver>` |
| `GET /api/frame/contract/<ver>/prompt` | none | text | binary | `contract --prompt` |
| `GET /api/frame/contract/<ver>/<name>.d.ts` | none | TypeScript text | binary | `contract --dts` |
| `POST /api/frame/types/<type slug>/create?idempotency_key=<k>[&create_confirmed=1]` | `{target_type_version: null, title?, entrypoint?, session_id?, request_id?}` | `{slug, version, title?, favicon?, contract?, type?, provisioned?, replayed?, read?, shared?}` | binary | `publish --type-url` |

Publish semantics (mirrors the CLI's `zi`/`$o`/`Rr`):
- Capabilities: omitted keeps the stored declaration; `{}` clears it; a declaration
  needs a contract, so the CLI sends the stored pin (from `/read/`) or, with none stored,
  `/contract/latest`'s version. `contract: "latest"` re-pins to the current contract.
- On an update with no `contract`, the CLI echoes the stored pin.
- `force: true` overwrites; `baseVersion` makes the publish fail on a newer version.
  Older control planes refused both together (`force and baseVersion are mutually
  exclusive`) or as unknown fields; the CLI retries without them.
- Unknown `icon`, `deadline` or `request_id` fields are retried without the field.
- Limits: page 16 MB; 256 manifest entries per publish (page included); 512 files per
  version; one `/upload` call holds at most 15 MiB of file content; the CLI inlines files
  up to a 12 MiB budget and stages the rest. `artifact.py` inlines up to 1 MiB, then stages.
- Supporting-file types: text/html, text/css, text/plain, text/markdown, text/csv,
  text/javascript, application/javascript, application/json,
  application/manifest+json, application/xml, text/xml, image/svg+xml,
  application/wasm, image/png, image/jpeg, image/gif, image/webp, image/avif,
  image/x-icon, font/woff, font/woff2, font/ttf, font/otf, audio/mpeg, audio/ogg,
  audio/wav, audio/webm, video/mp4, video/webm, video/ogg, application/pdf.

### Discovery and reads

| Method + path | Body / query | Response | Conf. | CLI |
|---|---|---|---|---|
| `GET /api/frame/artifacts` | `rel=mine\|shared`, `kind=page,document`, `sort=last_viewed`, `limit`, `page_token?` | `{artifacts: [{kind, id, title, rel, favicon?, updated_at, source, soft_deleted, outside_organization?, starred?, role?}], next_page_token?, starsEnabled?}` | binary | `list` |
| `GET /api/frame/frames?limit=N[&rel=shared]` | none | `{frames: [{slug, title, favicon, rel, updatedAt, softDeleted, starred}], starsEnabled?}` (legacy, unpaged, max 200) | binary (old script used it) | `list` fallback |
| `GET /api/frame/frames/external` | none | `{frames: [{slug, title?, role?}]}`: shared from other orgs | binary | `list --scope shared/all` |
| `GET /api/frame/<slug>?via=model_read[&ver=<ver>][&sk=…][&vanity=…]` | none | the "boot": `{ver, assetToken, title, favicon, perm: {role, mode, …}, mode, kind, shared, live, files: [{path, …}], fileCount, dbUsage, contentStore, contentVia?, type, typeInstance, artifactKind, cowritten, authoredByOthers, outsideWriter, subscriptionToken, syncToken, …}`. 404 body `"no version"` = never published; 400 with `ver` = version not kept | binary | `read`, `list --scope files` |
| content host `GET /_f/<ver>/<path>?__frame_t=<assetToken>` | none | the file bytes | binary | `read`, `assets read` |
| `GET /api/frame/content/<slug>/_f/<ver>/<path>` | header `x-frame-asset-token: <assetToken>` | bytes; valid only with `x-frame-content-route: served`; type in `x-frame-asset-content-type`. Used when boot has `contentVia:"frame"` | binary | `read` (auto) |
| `GET /api/frame/versions/<slug>` | none | `{live, history: [ver…], versions: {ver: {created_at}}, degraded?}`; 403/404 if not owner | binary | `versions` |
| `GET /api/frame/diag/<slug>/<ver>` | none | runtime diagnostics for that version; 404 = none recorded | binary (shape unknown) | `diag` |

`artifact.py` never prints `assetToken`, `subscriptionToken` or `syncToken`; it redacts
any key that looks like a token or secret.

### Lifecycle

| Method + path | Body | Response | Conf. | CLI |
|---|---|---|---|---|
| `DELETE /api/frame/<slug>` | none | 2xx deleted; 404 body `not found` / `{error:"not_found"}` = already gone. Permanent; the slug can't be republished | binary | `delete --yes` |
| `POST /api/frame/favorite/<slug>` | none | 2xx pinned (404 if pinning unavailable) | binary | `pin` |
| `DELETE /api/frame/favorite/<slug>` | none | 2xx unpinned | binary | `unpin` |
| `POST /api/frame/retitle/<slug>` | `{title}` | `{title}`; 404 not owner; 400/409 refused | binary | `retitle` |
| `GET /api/frame/perm/<slug>[?org=<uuid>]` | none | sharing settings `{version, read: {mode, …}, …}` | binary | **not implemented** |
| `PATCH /api/frame/perm/<slug>` | sharing plan; header `If-Match: <version>` | new settings; 412 changed meanwhile | binary | **not implemented (deliberately)** |

### Assets (`/api/frame/blob/…`)

| Method + path | Body | Response | Conf. | CLI |
|---|---|---|---|---|
| `POST /api/frame/blob/<slug>/agent-upload` | raw file bytes; `Content-Type` = the file's type | `{opaque_id, url?, size_bytes, content_type, sha256?}` | binary | `assets upload` |
| `POST /api/frame/blob/<slug>/agent-list` | `{limit: 50, after?}` | `{assets: [{opaque_id, url?, content_type, size_bytes, sha256?, created_at}], usage: {files, bytes, max_files, max_bytes}, next?}` | binary | `assets list`, `list --scope assets` |
| `POST /api/frame/blob/<slug>/<id>/agent-delete` | `{}` | `{deleted: bool}` | binary | `assets delete --yes` |
| `POST /api/frame/blob/<slug>/agent-copy` | `{from: <source slug>, ids: [1-10 ids], cross_org_ok?}` | `{assets: [{from_id, opaque_id, url?, size_bytes, content_type, sha256?}]}` | binary | `assets copy` |

Asset rules: the page must declare the `assets` capability (`capability_disabled`
otherwise); 20 MiB per asset, 2 MiB for SVG; accepted extensions png jpg jpeg gif webp
svg mp4 webm pdf woff2 woff ttf otf csv md markdown json txt ts css js mjs cjs. Error
codes arrive as `{error: {code, message?}}` with codes `invalid_request`, `too_large`,
`unsupported_type`, `quota_or_state`, `rate_limited`, `capability_disabled`,
`store_unavailable`, `upstream_error`, `credential_rejected`, `not_found`,
`asset_not_found`, `not_copyable`; a bare 403 `not a writer` means no write access.

### Database (`POST /api/frame/db/agent`)

One endpoint backs the whole ArtifactData tool. Confidence: binary (module with
`var ye="/api/frame/db/agent"`, functions `Qir` read, `ami`/`he` write, `lmi` batch, `Z` send).

Request: `{slug, verb, args, asLevel?: "view"|"interact"|"admin", requireVersion?: true, entrypoint?, session_id?, request_id?}`.

| verb | args | 200 response |
|---|---|---|
| `get` | `{path: "<collection>/<doc_id>"}` | `{exists, data?, version?, updatedAt?, authoredByOthers?, outside_writer?}` |
| `list` | `{collection, limit?, cursor?}` | `{docs: [{id, data, version?, updatedAt?}], nextCursor?}` |
| `query` | `list` args + `where?: [{field, op, value}]`, `orderBy?: {field, direction?: "asc"\|"desc"}` | as `list`; with `orderBy` a single page, no cursor |
| `set` | `{path, data, replace: true, ifVersion?}` | `{version?, usage?: {documents, maxDocuments}, warnings?}` |
| `update` | `{path, data, ifVersion?}`; `{"__delete__": true}` as a value removes that field (any depth, not inside arrays; `set` rejects it) | as `set` |
| `str_replace` | `{path, field, old_str, new_str, replace_all?, idem: <16 random bytes, base64url>, ifVersion?}` (gated in Claude Code by a flag; the server may refuse it) | as `set` |
| `delete` | `{path, ifVersion?}` | as `set` |
| `batch` | `{ops: [{op: "set"\|"update"\|"delete", path, data?, replace? (set), ifVersion?}]}` (1-50 ops, each path once, body at most 1 MiB, atomic) | `{results: [{version?}], usage?, warnings?}` |
| `whoami` | `{}` | `{id}`: resolves `data/users/me` to `data/users/<id>` |

Rules enforced client-side by the CLI (and by `artifact.py`):
- `collection` is 1-15 `/`-separated segments, each `[A-Za-z0-9_\-.~:@+]{1,200}`, not `.`
  or `..`; it must have an odd number of segments (`collection/doc/collection…`).
  `doc_id` is one segment. The composed path is at most 1000 bytes.
- `where` operators: `eq ne in not-in lt lte gt gte array-contains`; the CLI maps
  `== != < <= > >=` to those. At most 10 clauses. `limit` 1-1000 (default 100).
- `ifVersion` (tool parameter `if_version`): the write applies only at that version.
  With `requireVersion: true`, the server refuses a write to an existing document that
  carries no `ifVersion` (`version_mismatch` with `required: true`).

Errors: `{error: {code, reason?, kind?, limit?, version?, index?, required?, message?}}`.
Codes: `not_found` (also "no access"; deliberately indistinguishable), `invalid_argument`,
`quota_or_rate`, `quota_exceeded`, `busy`, `upstream_auth`, `store_unavailable`,
`not_declared` (page lacks the `db` capability), `too_large`, `version_mismatch` (carries
the current `version`, and `index` for a batch), `edit_no_match`, `edit_ambiguous`,
`edit_field_missing`, `edit_field_not_string`. Hyphenated variants (`not-found`,
`resource-exhausted`, …) are normalised. `kind` refines quota errors
(`docs_per_collection`, `docs_per_database`, `bytes_per_database`, `databases_per_org`,
`active_leases`, `rate_limited`, `write_capacity`, `scan_budget`, `response_budget`,
`refused_writes`). Without a JSON body, HTTP status maps 404/403 → not_found,
400/422 → invalid_argument, 413 → too_large, 429 → quota_or_rate, 409/412 → busy,
401 → upstream_auth.

### Users and comments

| Method + path | Body | Response | Conf. | CLI |
|---|---|---|---|---|
| `POST /api/frame/user/profiles/<slug>` | `{ids: [1-64 ids, "u_" + 22 chars]}` | `{profiles: {id: {name?, guest?}}}`; 403 `{error:"not_declared"}` if the page lacks the `user` capability | binary | `profiles` |
| `GET /api/frame/comments/<slug>[?names=1]` | none | `{threads: [{id, created_at, resolved_at?, resolved_by?, claude_activated_at?, claude_activated_by?, anchor?: {path, span, label, detail, region, …}, carried?, comments: [{id, text, created_at, author: {role, account, access?, outside?}, to_claude_at?, source?, sent_by_viewer?, presence?}]}], names?}` | binary | `comments read` |
| `POST /api/frame/comments/<slug>/<thread id>` | `{text, id?, answers_summon?, continues_reply_id?}` | 201 `{thread_id, comment_id, replayed?}`. 403 `not activated on this thread` = Claude was not summoned there; 409 `id_in_use` = replay | binary | `comments reply` |
| `POST /api/frame/comments/<slug>/<thread id>/session-resolve` | `{resolved: true}` | 200; 404 → fall back to `/resolve` | binary | `comments resolve` |
| `POST /api/frame/comments/<slug>/<thread id>/resolve` | `{resolved: true, after_version?}` | 200 | binary | `comments resolve` (fallback) |
| `POST /api/frame/comments/<slug>/<thread id>/summon-status` | status object | 2xx accepted | unknown | not implemented |

### Types (templates)

| Method + path | Response | Conf. | CLI |
|---|---|---|---|
| `GET /api/frame/types[?scopes=anthropic][&q=…]` | `{types: [{slug, title?, description?, listings?, release?: {version}}], next_page_token?}` | binary | `types list`, `list --scope types` |
| `GET /api/frame/types/<slug>` | type + `files: [{path, size?, content_type?}], capabilities?, creatable?` | binary | `types describe` |
| `GET /api/frame/types/<slug>/files/<path>` | `{path, chars?, truncated?, text}` | binary | `types file` |
| `GET /api/frame/types/<slug>/instances?limit=200[&rel=mine\|shared]` | artifacts made from the type | unknown (shape) | `types instances`, `list --type` |

### Seen but not implemented

| Endpoint | What it is | Why skipped |
|---|---|---|
| `POST /api/frame/subscribe/<slug>` `{url, sealed_secret, events}` and `POST /api/frame/unsubscribe/<slug>` `{}` | durable watch: the server calls a webhook trigger minted through Claude's MCP session system | needs the trigger minting flow; not simple |
| `POST /api/frame/watch-token/<slug>?vn=…` | renews a live-watch subscription token | only useful with the live socket |
| `/api/frame/sync?slug=…[&comment_hint=1][&vn=…]` | WebSocket, protocol `frame-sync.v1`, live replica of the page/db | streaming protocol; out of scope |
| `POST /api/frame/track` `{event_name, slug?, via?, mode?}` | telemetry | not a feature |
| `GET/PATCH /api/frame/perm/<slug>` | sharing | deliberately not called (no permission changes) |

## 4. HTML skeleton

When the page body isn't a full document, Claude Code 2.1.319 wraps it like this (`Ao`):

```html
<!doctype html><html[ lang="xx"]><head><meta charset=utf8>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>:root{color-scheme:light}body{margin:0;padding:0;
font:14px -apple-system,BlinkMacSystemFont,sans-serif;background:#fff;color:#000}
img{max-width:100%}[hidden]:not([hidden=until-found i]){display:none!important}</style>
</head><body>
{BODY}
</body></html>
```

`artifact.py` sends a file that already starts with `<!doctype html` unchanged (or any
file with `--raw`), and wraps everything else. The served page runs under a strict
CSP: inline CSS/JS, images as `data:` URIs, supporting files, or assets.

## 5. Re-deriving this table

```bash
BIN=~/.local/share/claude/versions/2.1.319
strings -n 6 "$BIN" | grep -oE '/api/frame[a-zA-Z0-9_/${}.:-]*' | sort | uniq -c
```

The plain-text JS copy sits after the bytecode (offsets near 2.6-3.1e8 in 2.1.319).
Pull the module around an endpoint and split it into lines:

```python
import re
d = open(BIN, "rb").read()
i = d.find(b'var ye="/api/frame/db/agent"')          # any endpoint string
s = d.rfind(b"// @bun @bytecode", 0, i); e = d.find(b"// @bun @bytecode", i)
js = d[s:e].decode("utf-8", "replace")
js = re.sub(r"(;|\})(async function |function |var |let |class )", r"\1\n\2", js)
```

Useful anchors: `var Ru={get(` (HTTP client verbs), `function xu(){return{Accept:` (frame
headers), `function is(e){return{...e.slug` (publish metadata), `async function yvn(`
(boot read), `function De(e){return\`/api/frame/blob/` (assets), `Ge=new Map([["not_found"`
(db error codes), `async function fe(e,t,n,r,o)` in the comments module.

## 6. Built-in artifact skills

Two skills back the Artifact tool. The tool's own instructions require loading
`artifact-design` before writing a page, and `artifact-capabilities` before declaring
any runtime capability. Extracted copies (from 2.1.212) live in `references/`:
`artifact-design.md` and `artifact-capabilities/`.
