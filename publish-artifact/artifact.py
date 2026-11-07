#!/usr/bin/env python3
"""
artifact.py: a stdlib-only CLI for Claude Artifacts (internally "frames").

It covers what Claude Code's built-in Artifact, ArtifactData and ArtifactComments
tools do, from any shell or agent harness: publish (page, supporting files,
capabilities, contract), read, list, delete, pin, retitle, versions, assets, the
artifact database, user profiles, comments, artifact types and the capability
contract. Every command prints JSON on stdout; errors print JSON on stderr and
exit non-zero.

UNOFFICIAL. The endpoints were reverse-engineered from the Claude Code binary
(2.1.319); see references/artifact-protocol.md for each endpoint's confidence.
The private API may change without notice.

Auth: the OAuth bearer token is read ONLY from $CLAUDE_CODE_OAUTH_TOKEN at runtime.
It is sent only to the frame API base (api.anthropic.com, or a loopback host for
tests), never on a redirect, and never printed.

Run `artifact.py -h` or `artifact.py <command> -h` for usage.

Exit codes: 0 ok, 1 API/HTTP/network error, 2 bad input (nothing sent),
3 no token, 4 conflict (version mismatch / stale base version).
"""
import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "0.2.0"

PRESETS = {
    "prod": {
        "api": "https://api.anthropic.com",
        "origin": "https://claude.ai",
        "content": "https://{slug}.frame.claudeusercontent.com",
    },
    "staging": {
        "api": "https://api.anthropic.com",
        "origin": "https://claude-ai.staging.ant.dev",
        "content": "https://{slug}.frame.staging.claudeusercontent.com",
    },
}

MAX_PAGE_BYTES = 16 * 1024 * 1024          # rendered page cap
INLINE_FILES_BUDGET = 1024 * 1024          # above this, supporting files are staged
UPLOAD_CHUNK_BYTES = 15 * 1024 * 1024      # max bytes of file content per /upload call
MAX_PUBLISH_ENTRIES = 255                  # files + removals per publish (page excluded)
ASSET_MAX_BYTES = 20 * 1024 * 1024
ASSET_SVG_MAX_BYTES = 2 * 1024 * 1024
DB_BATCH_MAX = 50
DB_BATCH_MAX_BYTES = 1024 * 1024
DB_PATH_MAX = 1000
DB_QUERY_LIMIT_MAX = 1000
DB_WHERE_MAX = 10
RETRY_AFTER_CAP_S = 10.0

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
SHORT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{22}$")
ASSET_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SEGMENT = r"(?!\.\.?(?:/|$))[A-Za-z0-9_\-.~:@+]{1,200}"
SEGMENT_RE = re.compile(rf"^{SEGMENT}$")
COLLECTION_RE = re.compile(rf"^{SEGMENT}(?:/{SEGMENT}){{0,14}}$")
DB_LEVELS = ("view", "interact", "admin")
DB_OPS = ("eq", "ne", "in", "not-in", "lt", "lte", "gt", "gte", "array-contains")
DB_OP_ALIASES = {"==": "eq", "!=": "ne", "<": "lt", "<=": "lte", ">": "gt", ">=": "gte"}
ME_PREFIX = "data/users/me"
# keys whose values are never printed (boot assetToken, subscription/sync tokens, ...);
# paging tokens (next_page_token, page_token) are not secrets and are kept.
SECRET_KEY_RE = re.compile(r"(?<!page_)token|secret|password|credential", re.I)

TEXT_TYPES = {
    "text/html", "text/css", "text/plain", "text/markdown", "text/csv", "text/javascript",
    "application/javascript", "application/json", "application/manifest+json",
    "application/xml", "text/xml", "image/svg+xml",
}
FILE_TYPES = {  # extension -> content type accepted for supporting files
    ".html": "text/html", ".htm": "text/html", ".css": "text/css", ".txt": "text/plain",
    ".md": "text/markdown", ".markdown": "text/markdown", ".csv": "text/csv",
    ".js": "text/javascript", ".mjs": "text/javascript", ".cjs": "text/javascript",
    ".json": "application/json", ".webmanifest": "application/manifest+json",
    ".xml": "application/xml", ".svg": "image/svg+xml", ".wasm": "application/wasm",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".avif": "image/avif", ".ico": "image/x-icon",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf",
    ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".wav": "audio/wav", ".weba": "audio/webm",
    ".mp4": "video/mp4", ".webm": "video/webm", ".ogv": "video/ogg", ".pdf": "application/pdf",
}
ALLOWED_FILE_TYPES = set(FILE_TYPES.values()) | {
    "application/javascript", "text/xml", "image/vnd.microsoft.icon",
    "application/font-woff", "application/font-woff2",
}
ASSET_TYPES = {  # extension -> content type accepted by the asset store
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".svg": "image/svg+xml", ".mp4": "video/mp4", ".webm": "video/webm",
    ".pdf": "application/pdf", ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
    ".otf": "font/otf", ".csv": "text/csv", ".md": "text/markdown", ".markdown": "text/markdown",
    ".json": "application/json", ".txt": "text/plain", ".ts": "text/plain", ".css": "text/css",
    ".js": "text/javascript", ".mjs": "text/javascript", ".cjs": "text/javascript",
}
EXT_FOR_TYPE = {}
for _ext, _ct in list(ASSET_TYPES.items()) + list(FILE_TYPES.items()):
    EXT_FOR_TYPE.setdefault(_ct, _ext)

# The <head> Claude Code 2.1.319 wraps a page body in (var `Ao` in the binary).
PAGE_HEAD = (
    '<meta charset=utf8><meta name=viewport content="width=device-width,initial-scale=1">'
    "<style>:root{color-scheme:light}body{margin:0;padding:0;"
    "font:14px -apple-system,BlinkMacSystemFont,sans-serif;background:#fff;color:#000}"
    "img{max-width:100%}[hidden]:not([hidden=until-found i]){display:none!important}</style>"
)


# --------------------------------------------------------------------------- errors
class CliError(Exception):
    """An error reported as JSON on stderr. exit: 1 api, 2 input, 3 auth, 4 conflict."""

    def __init__(self, code, message, *, exit_code=1, status=None, body=None, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.status = status
        self.body = body
        self.extra = extra

    def as_dict(self):
        d = {"code": self.code, "message": self.message}
        if self.status is not None:
            d["http_status"] = self.status
        if self.body is not None:
            body = self.body
            if isinstance(body, str) and len(body) > 2000:
                body = body[:2000] + "...[truncated]"
            d["server"] = redact(body)
        d.update(self.extra)
        return {"error": d}


def bad_input(message, code="invalid_argument"):
    return CliError(code, message, exit_code=2)


def redact(obj):
    """Drop any key that looks like a token/secret before output (e.g. boot assetToken)."""
    if isinstance(obj, dict):
        return {k: ("[redacted]" if SECRET_KEY_RE.search(k) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


# --------------------------------------------------------------------------- config
def _is_loopback(host):
    return host in ("localhost", "127.0.0.1", "::1") or (host or "").endswith(".localhost")


def cfg():
    env = os.environ.get("ARTIFACT_ENV", "prod")
    if env not in PRESETS:
        raise bad_input(f"ARTIFACT_ENV must be one of {sorted(PRESETS)}")
    c = dict(PRESETS[env])
    allow_custom = os.environ.get("ARTIFACT_ALLOW_CUSTOM_BASE") == "1"
    base = os.environ.get("CLAUDE_CODE_ARTIFACTS_API_BASE_URL")
    if base:
        base = base.rstrip("/")
        u = urllib.parse.urlsplit(base)
        ok = (u.scheme == "https" and u.hostname == "api.anthropic.com") or (
            u.scheme in ("http", "https") and _is_loopback(u.hostname))
        if not ok and not allow_custom:
            raise bad_input(
                "CLAUDE_CODE_ARTIFACTS_API_BASE_URL must be https://api.anthropic.com or a loopback "
                "host; the OAuth token is sent there. Set ARTIFACT_ALLOW_CUSTOM_BASE=1 to override.")
        c["api"] = base
    content = os.environ.get("ARTIFACT_CONTENT_ORIGIN")
    if content:
        u = urllib.parse.urlsplit(content.replace("{slug}", "x"))
        ok = (u.scheme == "https" and (u.hostname or "").endswith(".claudeusercontent.com")) or (
            u.scheme in ("http", "https") and _is_loopback(u.hostname))
        if not ok and not allow_custom:
            raise bad_input("ARTIFACT_CONTENT_ORIGIN must be a *.claudeusercontent.com or loopback "
                            "origin. Set ARTIFACT_ALLOW_CUSTOM_BASE=1 to override.")
        c["content"] = content.rstrip("/")
    origin = os.environ.get("ARTIFACT_VIEW_ORIGIN")
    if origin:
        c["origin"] = origin.rstrip("/")
    return c


def get_token():
    tok = (os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
    if not tok:
        raise CliError("no_token", "CLAUDE_CODE_OAUTH_TOKEN is not set. Generate a token with "
                       "`claude setup-token` and export it; this tool reads no other source.",
                       exit_code=3)
    return tok


# --------------------------------------------------------------------------- HTTP
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects: the bearer token must not travel to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class Resp:
    def __init__(self, status, headers, raw):
        self.status = status
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.raw = raw
        self._data = None

    @property
    def data(self):
        if self._data is None:
            text = self.raw.decode("utf-8", "replace") if self.raw else ""
            try:
                self._data = json.loads(text) if text else {}
            except ValueError:
                self._data = text
        return self._data

    @property
    def ok(self):
        return 200 <= self.status < 300


def frame_headers():
    h = {
        "Accept": "application/json, application/vnd.ant.frame-refusal+json",
        "X-Frame-CP": "go",
        "X-Frame-Surface": "code",
        "X-Frame-Platform": "cli",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": f"artifact-cli/{VERSION}",
    }
    cv = os.environ.get("ARTIFACT_CLIENT_VERSION")
    if cv:
        h["X-Frame-Client-Version"] = cv
    return h


def _retry_after_s(resp):
    v = resp.headers.get("retry-after")
    if v is None:
        return None
    try:
        s = float(v)
    except ValueError:
        return None
    return s if 0 <= s <= RETRY_AFTER_CAP_S else None


def http(method, url, *, token=None, json_body=None, raw_body=None, content_type=None,
         headers=None, timeout=60, retry_statuses=(429, 503)):
    """One HTTP exchange. Returns Resp for any status; raises CliError on transport errors.
    Retries once when the server answers a retry status with a short Retry-After."""
    body = None
    hdrs = dict(headers or {})
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    elif raw_body is not None:
        body = raw_body
        hdrs["Content-Type"] = content_type or "application/octet-stream"
    for attempt in (0, 1):
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        if token is not None:
            req.add_unredirected_header("Authorization", f"Bearer {token}")
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                resp = Resp(r.status, dict(r.headers), r.read())
        except urllib.error.HTTPError as e:
            try:
                resp = Resp(e.code, dict(e.headers or {}), e.read() if e.fp else b"")
            finally:
                e.close()
        except urllib.error.URLError as e:
            raise CliError("network_error", f"{method} {_safe_url(url)} failed: {e.reason}")
        except (TimeoutError, OSError) as e:
            raise CliError("network_error", f"{method} {_safe_url(url)} failed: {e}")
        if attempt == 0 and resp.status in retry_statuses:
            wait = _retry_after_s(resp)
            if wait is not None:
                time.sleep(wait)
                continue
        return resp
    return resp


def _safe_url(url):
    """URL for messages, with token-bearing query params stripped."""
    u = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query) if k != "__frame_t"]
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(q), ""))


class Api:
    """Frame API client bound to the configured base URL and the env token."""

    def __init__(self, c=None, token=None):
        self.c = c or cfg()
        self._token = token

    @property
    def token(self):
        if self._token is None:
            self._token = get_token()
        return self._token

    def url(self, path, query=None):
        u = self.c["api"] + path
        if query:
            u += ("&" if "?" in u else "?") + urllib.parse.urlencode(
                {k: v for k, v in query.items() if v is not None})
        return u

    def call(self, method, path, *, query=None, json_body=None, raw_body=None,
             content_type=None, extra_headers=None, timeout=60, retry_statuses=(429, 503)):
        h = frame_headers()
        if extra_headers:
            h.update(extra_headers)
        return http(method, self.url(path, query), token=self.token, json_body=json_body,
                    raw_body=raw_body, content_type=content_type, headers=h, timeout=timeout,
                    retry_statuses=retry_statuses)


def fail(resp, what, *, code=None):
    """Turn a non-2xx Resp into a CliError with the server's answer attached."""
    status = resp.status
    data = resp.data
    if code is None:
        code = {400: "invalid_argument", 401: "upstream_auth", 403: "forbidden",
                404: "not_found", 409: "conflict", 412: "conflict", 413: "too_large",
                422: "unprocessable", 429: "rate_limited"}.get(status, "http_error")
    msg = f"{what} failed (HTTP {status})"
    if status == 404:
        msg += ": not found, or not visible to this account (the two are indistinguishable)"
    elif status == 401:
        msg += ": the token was rejected; generate a fresh one with `claude setup-token`"
    elif status == 429:
        ra = resp.headers.get("retry-after")
        msg += f": rate limited{f' (retry after {ra}s)' if ra else ''}"
    detail = _server_message(data)
    if detail:
        msg += f": {detail}"
    return CliError(code, msg, status=status, body=data,
                    exit_code=4 if status in (409, 412) else 1)


def _server_message(data):
    if isinstance(data, str):
        return data.strip()[:300]
    if isinstance(data, dict):
        e = data.get("error")
        if isinstance(e, str):
            return e[:300] + (f" [{data['reason']}]" if isinstance(data.get("reason"), str) else "")
        if isinstance(e, dict) and isinstance(e.get("message"), str):
            return e["message"][:300]
        if isinstance(data.get("message"), str):
            return data["message"][:300]
    return ""


# --------------------------------------------------------------------------- refs & URLs
def b58_from_uuid(u):
    n = int(u.replace("-", ""), 16)
    out = ""
    for _ in range(22):
        n, r = divmod(n, 58)
        out = B58[r] + out
    return out


def uuid_from_b58(s):
    if not SHORT_RE.match(s):
        return None
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    if n >= 1 << 128:
        return None
    h = f"{n:032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


_ID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[1-9A-HJ-NP-Za-km-z]{22}"
_URL_RE = re.compile(
    rf"^https?://[^/?#]+/(?:artifact|code/(?:artifact|frame))/(?:([A-Za-z0-9_-]*)-)?({_ID})(?:[/?#]|$)")
_HOST_RE = re.compile(r"^https://([0-9a-f-]{36})\.frame\.(?:staging\.)?claudeusercontent\.com(?:[/?#]|$)")


class Ref:
    def __init__(self, slug, sk=None, vanity=None):
        self.slug = slug
        self.sk = sk
        self.vanity = vanity


def parse_ref(s):
    """Accept a claude.ai artifact URL (/artifact/<id>, /code/artifact/<id>), a bare UUID, a
    bare 22-character short id, or a <uuid>.frame.claudeusercontent.com URL."""
    if not isinstance(s, str) or not s.strip():
        raise bad_input("an artifact URL or id is required")
    s = s.strip()
    if UUID_RE.match(s):
        return Ref(s)
    u = uuid_from_b58(s)
    if u:
        return Ref(u)
    m = _URL_RE.match(s)
    if m:
        ident = m.group(2)
        slug = ident if UUID_RE.match(ident) else uuid_from_b58(ident)
        if slug:
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(s).query)
            sk = (q.get("sk") or [None])[0]
            vanity = m.group(1) if m.group(1) and re.match(r"^[a-z0-9][a-z0-9-]{0,59}$", m.group(1)) else None
            return Ref(slug, sk=sk if sk and re.match(r"^[A-Za-z0-9_-]{1,64}$", sk) else None,
                       vanity=vanity)
    m = _HOST_RE.match(s)
    if m and UUID_RE.match(m.group(1)):
        return Ref(m.group(1))
    raise bad_input(f"not an artifact URL or id: {s[:120]}")


def view_urls(c, slug):
    return {"url": f"{c['origin']}/artifact/{b58_from_uuid(slug)}",
            "code_url": f"{c['origin']}/code/artifact/{slug}"}


def clean_rel_path(p, *, what="path"):
    """Validate a published file path (same rules as the CLI's `rn`). Returns it URL-encoded."""
    if not isinstance(p, str) or not p or p.startswith("/") or len(p.encode()) > 512:
        raise bad_input(f"{what} must be a relative path (no leading /, at most 512 bytes): {p!r}")
    segs = p.split("/")
    for seg in segs:
        if seg in ("", ".", "..") or re.search(r"[\\%\x00-\x1f\x7f]", seg):
            raise bad_input(f"{what} is not a clean relative path: {p!r}")
    return "/".join(urllib.parse.quote(s, safe="") for s in segs)


def read_json_arg(value, what):
    """Parse a JSON argument given inline or as @path."""
    if value is None:
        return None
    if value.startswith("@"):
        try:
            value = Path(value[1:]).read_text(encoding="utf-8")
        except OSError as e:
            raise bad_input(f"cannot read {what} file: {e}")
    try:
        return json.loads(value)
    except ValueError as e:
        raise bad_input(f"{what} is not valid JSON: {e}")


# --------------------------------------------------------------------------- boot / content
def boot(api, ref, ver=None):
    """GET /api/frame/<slug>?via=model_read: the read 'boot' (version, perms, asset token)."""
    q = {"via": "model_read", "ver": ver, "sk": ref.sk, "vanity": ref.vanity}
    r = api.call("GET", f"/api/frame/{ref.slug}", query=q, timeout=15)
    if r.status == 404:
        if isinstance(r.data, str) and r.data.strip() == "no version":
            raise CliError("never_published", "this artifact exists but nothing has been published to it",
                           status=404)
        raise fail(r, "artifact read", code="not_found")
    if r.status == 400 and ver is not None:
        raise fail(r, f"artifact read of version {ver} (not among the versions kept?)")
    if not r.ok:
        raise fail(r, "artifact read")
    if not isinstance(r.data, dict) or not r.data.get("ver"):
        raise CliError("boot_incomplete", "artifact read failed: incomplete boot response",
                       status=r.status)
    if ver is not None and r.data.get("ver") != ver:
        raise CliError("past_ver_ignored", "the server served a different version than the one "
                       "asked for (it does that for accounts that cannot edit the artifact)")
    return r.data


def boot_summary(c, slug, b):
    perm = b.get("perm") if isinstance(b.get("perm"), dict) else {}
    out = {"slug": slug, **view_urls(c, slug), "ver": b.get("ver"), "title": b.get("title"),
           "favicon": b.get("favicon"), "role": perm.get("role"),
           "mode": perm.get("mode") or b.get("mode"), "shared": b.get("shared"),
           "public_read": "assetToken" not in b}
    for k in ("type", "typeInstance", "artifactKind", "fileCount", "dbUsage", "cowritten",
              "authoredByOthers", "outsideWriter", "live", "contentVia"):
        if k in b:
            out[k] = b[k]
    if isinstance(b.get("files"), list):
        out["files"] = b["files"]
    return redact(out)


def fetch_content(api, slug, b, rel_path, *, prefer_source=True):
    """Fetch a published page/file/asset of the booted version. rel_path is '' for the page,
    a clean relative path, or '_blob/<id>'. Returns (bytes, content_type)."""
    ver = b["ver"]
    tok = b.get("assetToken")
    perm = b.get("perm") if isinstance(b.get("perm"), dict) else {}
    writer = tok is not None and (b.get("mode") != "external" or perm.get("role") == "writer")
    paths = []
    if prefer_source and writer and not rel_path.startswith("_blob/"):
        paths.append(f"/_f/{ver}/_src/{rel_path or 'index.html'}")
    paths.append(f"/_f/{ver}/{rel_path}")
    last = None
    for p in paths:
        if b.get("contentVia") == "frame" and tok:
            r = api.call("GET", f"/api/frame/content/{urllib.parse.quote(slug)}{p}",
                         extra_headers={"x-frame-asset-token": tok}, timeout=30)
            if r.headers.get("x-frame-content-route") != "served":
                last = r
                continue
            ctype = r.headers.get("x-frame-asset-content-type") or r.headers.get("content-type")
        else:
            base = api.c["content"].replace("{slug}", slug)
            url = base + p + (f"?__frame_t={urllib.parse.quote(tok, safe='')}" if tok else "")
            r = http("GET", url, headers={"User-Agent": f"artifact-cli/{VERSION}"}, timeout=30)
            ctype = r.headers.get("content-type")
        if r.status == 200:
            return r.raw, (ctype or "").split(";")[0].strip().lower()
        last = r
    if last is not None and last.status == 404:
        raise CliError("missing_file", f"this version has no file at {rel_path or 'index.html'!r}",
                       status=404)
    raise CliError("content_fetch_failed",
                   f"artifact content fetch failed (HTTP {last.status if last else 'n/a'})",
                   status=last.status if last else None)


# --------------------------------------------------------------------------- publish
def build_page(path, lang=None, raw=False):
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise bad_input(f"cannot read page file: {e}")
    if p.suffix.lower() in (".md", ".markdown"):
        text = _md_to_html(text)
    if raw or re.match(r"^\s*<!doctype html", text, re.I):
        html = text
    else:
        lang_attr = f' lang="{lang}"' if lang else ""
        html = f"<!doctype html><html{lang_attr}><head>{PAGE_HEAD}</head><body>\n{text}\n</body></html>"
    size = len(html.encode("utf-8"))
    if size > MAX_PAGE_BYTES:
        raise bad_input(f"page too large: {size / 1048576:.1f}MB (max 16MB)", "too_large")
    return html


def _md_to_html(md):
    try:
        import markdown  # type: ignore  # optional, not required
        return markdown.markdown(md, extensions=["fenced_code", "tables"])
    except Exception:
        import html as _h
        return "<pre style='white-space:pre-wrap;padding:2rem'>" + _h.escape(md) + "</pre>"


def contract_latest(api):
    r = api.call("GET", "/api/frame/contract/latest", timeout=15)
    if not r.ok or not isinstance(r.data, dict) or not isinstance(r.data.get("version"), str):
        raise fail(r, "capability contract fetch") if not r.ok else CliError(
            "malformed", "capability contract response had no version")
    return r.data


def read_back(api, slug):
    """GET /api/frame/read/<slug>: the stored contract pin, capabilities and type lock."""
    r = api.call("GET", f"/api/frame/read/{urllib.parse.quote(slug)}", timeout=15)
    if r.status == 404:
        return None
    if not r.ok:
        raise fail(r, "stored declaration read")
    return r.data if isinstance(r.data, dict) else {}


def _file_specs(args):
    """Parse --file/--file-type/--remove/--copy into entries."""
    types = {}
    for spec in args.file_type or []:
        pub, _, ct = spec.partition("=")
        if not ct:
            raise bad_input(f"--file-type wants PUBLISHED=TYPE: {spec!r}")
        types[pub] = ct.strip().lower()
    files, seen = [], set()
    for spec in args.file or []:
        pub, sep, local = spec.partition("=")
        if not sep:
            pub, local = Path(spec).name, spec
        clean_rel_path(pub, what="published path")
        if pub == "index.html":
            raise bad_input("index.html is the page itself; pass it as FILE instead of --file")
        if pub in seen:
            raise bad_input(f"published path given twice: {pub}")
        seen.add(pub)
        ct = types.get(pub) or FILE_TYPES.get(Path(pub).suffix.lower())
        if ct not in ALLOWED_FILE_TYPES:
            raise bad_input(f"{pub}: unsupported or unknown content type {ct!r}; use --file-type "
                            f"{pub}=<type> with one of: {', '.join(sorted(ALLOWED_FILE_TYPES))}")
        try:
            content = Path(local).read_bytes()
        except OSError as e:
            raise bad_input(f"cannot read {local}: {e}")
        if not content:
            raise bad_input(f"{local} is empty")
        if ct in TEXT_TYPES:
            try:
                wire = content.decode("utf-8")
            except UnicodeDecodeError:
                raise bad_input(f"{local} is {ct} but not valid UTF-8")
        else:
            wire = base64.b64encode(content).decode("ascii")
        files.append({"path": pub, "contentType": ct, "wire": wire,
                      "sha256": hashlib.sha256(content).hexdigest()})
    removes = []
    for pub in args.remove or []:
        clean_rel_path(pub, what="--remove path")
        if pub == "index.html":
            raise bad_input("index.html is the page itself and can't be removed")
        if pub in seen:
            raise bad_input(f"{pub} is both written and removed")
        seen.add(pub)
        removes.append(pub)
    copies = []
    for spec in args.copy or []:
        pub, sep, src = spec.partition("=")
        m = re.match(r"^(.+):([^:@]+)(?:@([A-Za-z0-9_-]{1,64}))?$", src) if sep else None
        if not m:
            raise bad_input(f"--copy wants PUBLISHED=SOURCE_URL:SOURCE_PATH[@VERSION]: {spec!r}")
        clean_rel_path(pub, what="published path")
        clean_rel_path(m.group(2), what="source path")
        if pub in seen or pub == "index.html":
            raise bad_input(f"--copy target {pub} is already used")
        seen.add(pub)
        frm = {"slug": parse_ref(m.group(1)).slug, "path": m.group(2)}
        if m.group(3):
            frm["ver"] = m.group(3)
        copies.append({"path": pub, "from": frm})
    if len(files) + len(removes) + len(copies) > MAX_PUBLISH_ENTRIES:
        raise bad_input(f"at most {MAX_PUBLISH_ENTRIES} files/removals/copies per publish; "
                        "send the rest in another publish to the same url")
    return files, removes, copies


def _deploy(api, body, what="publish"):
    """POST /api/frame/deploy/direct, dropping fields an older control plane refuses."""
    for _ in range(4):
        r = api.call("POST", "/api/frame/deploy/direct", json_body=body, timeout=120)
        if r.status == 400:
            msg = _server_message(r.data) or (r.data if isinstance(r.data, str) else "")
            unknown = re.search(r"unknown field|not a recognized|not allowlisted", msg, re.I)
            dropped = False
            for field in ("icon", "label", "description"):
                if unknown and field in body and re.search(rf"['\"]{field}['\"]|\b{field}\b", msg):
                    body = {k: v for k, v in body.items() if k != field}
                    dropped = True
            if unknown and ("force" in body or "baseVersion" in body) and re.search(
                    r"\bforce\b|\bbaseVersion\b", msg):
                body = {k: v for k, v in body.items() if k not in ("force", "baseVersion")}
                dropped = True
            if "force" in body and "baseVersion" in body and re.search(
                    r"force and baseVersion are mutually exclusive", msg):
                body = {k: v for k, v in body.items() if k != "baseVersion"}
                dropped = True
            if dropped:
                continue
        return r
    return r


def cmd_publish(api, args):
    c = api.c
    if args.type_url:
        return create_from_type(api, args)
    slug = parse_ref(args.url).slug if args.url else None
    files, removes, copies = _file_specs(args)
    has_files = bool(files or removes or copies)
    if args.file_path is None and not (slug and has_files):
        raise bad_input("FILE is required (only an update that changes supporting files may omit it)")
    if removes and not slug:
        raise bad_input("--remove applies to an existing artifact; pass --url")
    html = build_page(args.file_path, args.lang, args.raw) if args.file_path else None

    # Title: --title, else the page's <title>, else (update) the current title, else file stem.
    title = args.title
    if not title and html is not None:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        if m and m.group(1).strip():
            title = re.sub(r"\s+", " ", m.group(1)).strip()[:4000]
    current = None
    need_base = slug and not args.base_version and (not args.force or has_files)
    if slug and (need_base or not title):
        current = boot(api, Ref(slug))
    if not title:
        title = (current or {}).get("title") or (Path(args.file_path).stem if args.file_path else None)
    meta = {"title": title} if title else {}
    for k in ("favicon", "icon", "label", "description"):
        v = getattr(args, k)
        if v:
            meta[k] = v
    if slug:
        meta["slug"] = slug

    # capabilities + contract (mirrors the CLI's `$o`)
    caps = read_json_arg(args.capabilities, "--capabilities")
    if caps is not None and not isinstance(caps, dict):
        raise bad_input("--capabilities must be a JSON object ({} clears the stored declaration)")
    stored = None
    if slug and (args.contract is None):
        stored = read_back(api, slug)
    contract = None
    if args.contract == "latest":
        contract = contract_latest(api)["version"]
    elif args.contract:
        contract = args.contract
    elif stored and isinstance(stored.get("contract"), str):
        contract = stored["contract"]
    elif caps is not None:
        contract = contract_latest(api)["version"]
    if contract is not None:
        meta["contract"] = contract
    if caps is not None:
        meta["capabilities"] = caps

    # optimistic concurrency: build on the version read (as Claude Code does), unless --force.
    # A files publish to an existing artifact always names its base (patch mode).
    base_version = args.base_version or (current["ver"] if need_base else None)
    pre = {}
    if args.force:
        pre["force"] = True
    if base_version:
        pre["baseVersion"] = base_version

    if args.dry_run:
        preview = {**meta, **pre}
        if files or removes or copies:
            preview["files"] = [{"path": f["path"], "contentType": f["contentType"],
                                 "sha256": f["sha256"]} for f in files]
            preview["remove"] = removes
            preview["copy"] = copies
        if html is not None:
            preview["page_bytes"] = len(html.encode("utf-8"))
        return {"dry_run": True, "request": preview}

    if not has_files:
        r = _deploy(api, {**meta, **pre, "content": html})
    else:
        r = _publish_manifest(api, meta, pre, html, files, removes, copies, slug, base_version)
    if not r.ok:
        if r.status in (409, 412):
            raise fail(r, "publish (the artifact changed since --base-version; re-read it, merge, "
                          "and publish again, or pass --force to overwrite)", code="conflict")
        raise fail(r, "publish")
    data = r.data if isinstance(r.data, dict) else {}
    if not data.get("slug") or not data.get("version"):
        raise CliError("deploy_incomplete", "deploy returned an incomplete response",
                       status=r.status, body=r.data)
    return redact({**data, **view_urls(c, data["slug"])})


def _publish_manifest(api, meta, pre, html, files, removes, copies, slug, base_version):
    mode = "patch" if slug and base_version else "replace"
    if slug and mode != "patch":
        raise bad_input("an update with supporting files needs the artifact's current version "
                        "(pass --base-version, or let the tool read it)")
    manifest = {}
    if html is not None:
        manifest["index.html"] = {"content": html, "contentType": "text/html"}
    for p in removes:
        manifest[p] = None
    inline_bytes = sum(len(f["wire"].encode("utf-8")) for f in files)
    staged = bool(copies) or inline_bytes > INLINE_FILES_BUDGET
    if not staged:
        for f in files:
            manifest[f["path"]] = {"content": f["wire"], "contentType": f["contentType"]}
        return _deploy(api, {**meta, **pre, "manifest": manifest, "mode": mode})

    # staged lane: prepare (reserves a slug, reports missing blobs) -> upload -> deploy
    prep_body = {"shas": sorted({f["sha256"] for f in files})}
    if slug:
        prep_body["slug"] = slug
    r = api.call("POST", "/api/frame/deploy/prepare", json_body=prep_body, timeout=30)
    if not r.ok:
        raise fail(r, "upload preflight (/deploy/prepare)")
    got_slug = r.data.get("slug") if isinstance(r.data, dict) else None
    missing = r.data.get("missing") if isinstance(r.data, dict) else None
    if not isinstance(got_slug, str) or not UUID_RE.match(got_slug) or not isinstance(missing, list):
        raise CliError("malformed", "upload preflight answered in an unexpected shape", body=r.data)
    if slug and got_slug != slug:
        raise CliError("slug_mismatch", "the preflight answered for a different artifact; not publishing")
    slug = got_slug
    need = [f for f in files if f["sha256"] in set(missing)]
    batch, size = [], 0
    batches = []
    for f in need:
        n = len(f["wire"].encode("utf-8"))
        if batch and size + n > UPLOAD_CHUNK_BYTES:
            batches.append(batch)
            batch, size = [], 0
        batch.append(f)
        size += n
    if batch:
        batches.append(batch)
    for b in batches:
        r = api.call("POST", "/api/frame/upload", json_body={
            "slug": slug,
            "files": [{"path": f["path"], "content": f["wire"], "contentType": f["contentType"]}
                      for f in b]}, timeout=300)
        if not r.ok:
            raise fail(r, f"file upload (publish not completed; retry with --url {slug})")
    for f in files:
        manifest[f["path"]] = {"sha256": f["sha256"], "contentType": f["contentType"]}
    if copies:
        r = api.call("POST", "/api/frame/upload", json_body={"slug": slug, "files": copies},
                     timeout=300)
        if not r.ok:
            raise fail(r, f"copying files from another artifact (publish not completed; retry with --url {slug})")
        echo = r.data.get("files") if isinstance(r.data, dict) else None
        if not isinstance(echo, list) or len(echo) != len(copies):
            raise CliError("malformed", "copy staging answered in an unexpected shape", body=r.data)
        for cp, e in zip(copies, echo):
            if not isinstance(e, dict) or e.get("leftOut") is not None or not isinstance(e.get("sha256"), str):
                raise CliError("copy_left_out", f"the server did not stage the copy of {cp['path']}",
                               body=e)
            manifest[cp["path"]] = {"sha256": e["sha256"], "contentType": e.get("contentType")}
    return _deploy(api, {**meta, **pre, "slug": slug, "manifest": manifest, "mode": mode})


def create_from_type(api, args):
    tslug = parse_ref(args.type_url).slug
    key = args.idempotency_key or secrets.token_urlsafe(16)
    body = {"target_type_version": None}
    if args.title:
        body["title"] = args.title
    r = api.call("POST", f"/api/frame/types/{urllib.parse.quote(tslug)}/create",
                 query={"idempotency_key": key}, json_body=body, timeout=60)
    if not r.ok:
        raise fail(r, "create from artifact type")
    data = r.data if isinstance(r.data, dict) else {}
    if not isinstance(data.get("slug"), str):
        raise CliError("malformed", "create-from-type answered without a slug", body=r.data)
    return redact({**data, **view_urls(api.c, data["slug"]), "idempotency_key": key})


# --------------------------------------------------------------------------- list
def _list_paged(api, rel, limit, page_token):
    r = api.call("GET", "/api/frame/artifacts", query={
        "rel": rel, "kind": "page,document", "sort": "last_viewed", "limit": str(limit),
        "page_token": page_token}, timeout=15)
    if r.status == 404:
        return None
    if not r.ok:
        raise fail(r, f"artifact listing ({rel})")
    rows = []
    for a in (r.data.get("artifacts") or []) if isinstance(r.data, dict) else []:
        if not isinstance(a, dict) or a.get("kind") not in ("page", "document"):
            continue
        if a.get("soft_deleted"):
            continue
        slug = a.get("id")
        if not isinstance(slug, str) or not UUID_RE.match(slug):
            continue
        row = {"slug": slug, **view_urls(api.c, slug), "title": a.get("title") or "Untitled",
               "rel": a.get("rel", rel), "kind": a.get("kind"), "updatedAt": a.get("updated_at")}
        for k in ("favicon", "starred", "role", "source"):
            if k in a:
                row["pinned" if k == "starred" else k] = a[k]
        if a.get("outside_organization"):
            row["external"] = True
        rows.append(row)
    return rows, (r.data.get("next_page_token") or None)


def _list_legacy(api, scope, limit):
    r = api.call("GET", "/api/frame/frames", query={"limit": str(min(max(limit, 1), 200))}, timeout=15)
    if not r.ok:
        raise fail(r, "artifact listing (legacy)")
    rows = []
    for f in (r.data.get("frames") or []) if isinstance(r.data, dict) else []:
        if not isinstance(f, dict) or f.get("softDeleted") or not UUID_RE.match(str(f.get("slug", ""))):
            continue
        if scope != "all" and f.get("rel") != scope:
            continue
        rows.append({"slug": f["slug"], **view_urls(api.c, f["slug"]),
                     "title": f.get("title") or "Untitled", "rel": f.get("rel"),
                     "updatedAt": f.get("updatedAt"), "favicon": f.get("favicon"),
                     "pinned": f.get("starred")})
    return rows[:limit]


def cmd_list(api, args):
    scope = args.scope
    if args.type:
        return list_type_instances(api, parse_ref(args.type).slug, scope if scope in ("mine", "shared") else "all",
                                   args.limit)
    if scope == "types":
        return types_list(api, args.query, args.anthropic)
    if scope == "files":
        if not args.url:
            raise bad_input("--scope files needs --url")
        ref = parse_ref(args.url)
        b = boot(api, ref)
        return {"slug": ref.slug, "ver": b.get("ver"), "fileCount": b.get("fileCount"),
                "files": redact(b.get("files") or [])}
    if scope == "assets":
        if not args.url:
            raise bad_input("--scope assets needs --url")
        return assets_list(api, parse_ref(args.url).slug, args.page_token, args.all)
    if scope not in ("mine", "shared", "all"):
        raise bad_input("--scope must be mine, shared, all, types, files or assets")
    limit = max(1, min(args.limit, 200))
    rows, tokens = [], {}
    if scope == "all":
        mine_tok, _, shared_tok = (args.page_token or "~").partition("~")
        lanes = []
        if not args.page_token or mine_tok:
            lanes.append(("mine", mine_tok or None))
        if not args.page_token or shared_tok:
            lanes.append(("shared", shared_tok or None))
    else:
        lanes = [(scope, args.page_token)]
    legacy = False
    for rel, tok in lanes:
        want = limit if scope != "all" else max(1, -(-limit // len(lanes)))
        got = []
        while True:
            res = _list_paged(api, rel, want - len(got) if not args.all else 200, tok)
            if res is None:
                legacy = True
                break
            page, tok = res
            got.extend(page)
            if tok is None or (not args.all and len(got) >= want):
                break
        if legacy:
            break
        rows.extend(got)
        tokens[rel] = tok
    if legacy:
        return {"rows": _list_legacy(api, scope, limit), "legacy": True}
    out = {"rows": rows, "total": len(rows)}
    if scope == "all":
        if tokens.get("mine") or tokens.get("shared"):
            out["next_page_token"] = f"{tokens.get('mine') or ''}~{tokens.get('shared') or ''}"
    elif tokens.get(scope):
        out["next_page_token"] = tokens[scope]
    if scope != "mine" and not args.page_token:
        ext = api.call("GET", "/api/frame/frames/external", timeout=5)
        if ext.ok and isinstance(ext.data, dict):
            have = {r["slug"] for r in rows}
            for f in ext.data.get("frames") or []:
                if isinstance(f, dict) and UUID_RE.match(str(f.get("slug", ""))) and f["slug"] not in have:
                    rows.append({"slug": f["slug"], **view_urls(api.c, f["slug"]),
                                 "title": f.get("title") or "Untitled", "rel": "shared",
                                 "external": True, **({"role": f["role"]} if "role" in f else {})})
            out["total"] = len(rows)
    return out


# --------------------------------------------------------------------------- read / info
def cmd_read(api, args):
    ref = parse_ref(args.url)
    b = boot(api, ref, args.version)
    out = boot_summary(api.c, ref.slug, b)
    if args.meta:
        return out
    if b.get("assetToken") is None:
        out["note"] = "public (tokenless) read: content is fetched from the content host without a token"
    paths = args.path or [""]
    results = []
    for p in paths:
        rel = clean_rel_path(p, what="--path") if p else ""
        raw, ctype = fetch_content(api, ref.slug, b, rel, prefer_source=not args.served)
        entry = {"path": p or "index.html", "contentType": ctype or None, "bytes": len(raw)}
        if args.out_dir:
            dest = _safe_dest(args.out_dir, p or "index.html")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
            entry["saved"] = str(dest)
        elif args.out and len(paths) == 1:
            Path(args.out).write_bytes(raw)
            entry["saved"] = str(Path(args.out))
        else:
            _attach_content(entry, raw, ctype)
        results.append(entry)
    if args.path:
        out["files_read"] = results
    else:
        out["page"] = results[0]
    return out


def _attach_content(entry, raw, ctype):
    if (ctype or "").startswith("text/") or ctype in TEXT_TYPES or not ctype:
        try:
            entry["content"] = raw.decode("utf-8")
            return
        except UnicodeDecodeError:
            pass
    entry["encoding"] = "base64"
    entry["content"] = base64.b64encode(raw).decode("ascii")


def _safe_dest(out_dir, rel):
    root = Path(out_dir).resolve()
    dest = (root / rel).resolve()
    if root != dest and root not in dest.parents:
        raise bad_input(f"refusing to write outside --out-dir: {rel}")
    return dest


def cmd_info(api, args):
    ref = parse_ref(args.url)
    stored = read_back(api, ref.slug)
    if stored is None:
        raise CliError("not_found", "no stored declaration (artifact missing, not yours, or never published)",
                       status=404)
    return redact({"slug": ref.slug, **view_urls(api.c, ref.slug), **stored})


# --------------------------------------------------------------------------- lifecycle
def cmd_delete(api, args):
    ref = parse_ref(args.url)
    if not args.yes:
        raise bad_input("deleting an artifact is permanent: its link stops working for everyone and it "
                        "cannot be restored or republished. Re-run with --yes to confirm.",
                        "confirmation_required")
    r = api.call("DELETE", f"/api/frame/{urllib.parse.quote(ref.slug)}", timeout=15,
                 retry_statuses=(409, 429, 503))
    if r.status == 404:
        d = r.data
        if (isinstance(d, str) and d.strip() == "not found") or (isinstance(d, dict) and d.get("error") == "not_found"):
            return {"slug": ref.slug, "deleted": False, "alreadyGone": True}
        raise fail(r, "delete (404 not from the artifact service; check `list` before assuming it is gone)")
    if not r.ok:
        raise fail(r, "delete")
    return {"slug": ref.slug, "deleted": True}


def cmd_pin(api, args, pin=True):
    ref = parse_ref(args.url)
    path = f"/api/frame/favorite/{urllib.parse.quote(ref.slug)}"
    r = api.call("POST" if pin else "DELETE", path, timeout=15)
    if not r.ok:
        raise fail(r, "pin" if pin else "unpin")
    return {"slug": ref.slug, "pinned": pin}


def cmd_retitle(api, args):
    ref = parse_ref(args.url)
    if not args.title.strip():
        raise bad_input("title must not be empty")
    r = api.call("POST", f"/api/frame/retitle/{urllib.parse.quote(ref.slug)}",
                 json_body={"title": args.title}, timeout=15)
    if not r.ok:
        raise fail(r, "retitle")
    title = r.data.get("title") if isinstance(r.data, dict) else None
    return {"slug": ref.slug, "title": title if isinstance(title, str) else args.title}


def cmd_versions(api, args):
    ref = parse_ref(args.url)
    r = api.call("GET", f"/api/frame/versions/{urllib.parse.quote(ref.slug)}", timeout=15)
    if r.status in (403, 404):
        raise fail(r, "listing versions (only the owner can list versions)", code="not_owner")
    if not r.ok:
        raise fail(r, "listing versions")
    d = r.data if isinstance(r.data, dict) else {}
    live, history, meta = d.get("live"), d.get("history") or [], d.get("versions") or {}
    rows = []
    for vid in history:
        if not isinstance(vid, str):
            continue
        m = meta.get(vid) if isinstance(meta, dict) else None
        row = {"id": vid}
        if isinstance(m, dict) and m.get("created_at"):
            row["createdAt"] = m["created_at"]
        if vid == live:
            row["current"] = True
        rows.append(row)
    out = {"slug": ref.slug, "live": live, "versions": rows}
    if d.get("degraded") or not meta:
        out["degraded"] = True
    return out


def cmd_diag(api, args):
    ref = parse_ref(args.url)
    ver = args.version or boot(api, ref)["ver"]
    r = api.call("GET", f"/api/frame/diag/{ref.slug}/{urllib.parse.quote(ver)}", timeout=15)
    if r.status == 404:
        return {"slug": ref.slug, "ver": ver, "state": "no_row"}
    if not r.ok:
        raise fail(r, "diagnostics read")
    return redact({"slug": ref.slug, "ver": ver, "diag": r.data})


# --------------------------------------------------------------------------- assets
def _asset_err(r, what):
    d = r.data
    code = None
    if isinstance(d, dict) and isinstance(d.get("error"), dict):
        code = d["error"].get("code")
    elif r.status == 403 and isinstance(d, str) and d.strip() == "not a writer":
        code = "not_writer"
    hint = {"capability_disabled": "the artifact's published version must declare the `assets` capability",
            "quota_or_state": "the artifact cannot take assets right now (unpublished, live document, or over quota)",
            "unsupported_type": f"accepted extensions: {', '.join(sorted(ASSET_TYPES))}"}.get(code)
    e = fail(r, what, code=code or None)
    if hint:
        e.message += f" ({hint})"
    return e


def cmd_assets(api, args):
    ref = parse_ref(args.url)
    slug = ref.slug
    if args.assets_cmd == "upload":
        out = []
        for fp in args.files:
            ext = Path(fp).suffix.lower()
            ct = ASSET_TYPES.get(ext)
            if ct is None:
                raise bad_input(f"{fp}: unsupported asset type; accepted: {', '.join(sorted(ASSET_TYPES))}")
            try:
                data = Path(fp).read_bytes()
            except OSError as e:
                raise bad_input(f"cannot read {fp}: {e}")
            cap = ASSET_SVG_MAX_BYTES if ct == "image/svg+xml" else ASSET_MAX_BYTES
            if not data or len(data) > cap:
                raise bad_input(f"{fp}: {'empty' if not data else f'over the {cap >> 20} MiB limit'}",
                                "too_large" if data else "invalid_argument")
            r = api.call("POST", f"/api/frame/blob/{slug}/agent-upload", raw_body=data,
                         content_type=ct, timeout=300)
            if not r.ok:
                raise _asset_err(r, f"asset upload of {fp}")
            d = r.data if isinstance(r.data, dict) else {}
            aid = d.get("opaque_id")
            out.append({"file": fp, "id": aid, "url": _asset_url(aid, d.get("url")),
                        "sizeBytes": d.get("size_bytes"), "contentType": d.get("content_type"),
                        **({"sha256": d["sha256"]} if "sha256" in d else {})})
        return {"slug": slug, "uploaded": out}
    if args.assets_cmd == "list":
        return assets_list(api, slug, args.after, args.all)
    if args.assets_cmd == "delete":
        if not ASSET_ID_RE.match(args.id):
            raise bad_input("not a valid asset id (32 hex characters)")
        if not args.yes:
            raise bad_input("deleting an asset is permanent; re-run with --yes", "confirmation_required")
        r = api.call("POST", f"/api/frame/blob/{slug}/{args.id}/agent-delete", json_body={}, timeout=30)
        if not r.ok:
            raise _asset_err(r, "asset delete")
        return {"slug": slug, "id": args.id,
                "deleted": bool(r.data.get("deleted")) if isinstance(r.data, dict) else None}
    if args.assets_cmd == "copy":
        src = parse_ref(args.from_url).slug
        ids = args.ids
        if src == slug:
            raise bad_input("--from names the destination itself")
        if not 1 <= len(ids) <= 10 or len(set(ids)) != len(ids) or not all(ASSET_ID_RE.match(i) for i in ids):
            raise bad_input("give 1-10 distinct asset ids (32 hex characters each)")
        body = {"from": src, "ids": ids}
        if args.cross_org_ok:
            body["cross_org_ok"] = True
        r = api.call("POST", f"/api/frame/blob/{slug}/agent-copy", json_body=body, timeout=120)
        if not r.ok:
            raise _asset_err(r, "asset copy")
        rows = r.data.get("assets") if isinstance(r.data, dict) else None
        if not isinstance(rows, list):
            raise CliError("malformed", "asset copy reply was unreadable; list the assets before retrying",
                           body=r.data)
        return {"slug": slug, "copied": [{"fromId": a.get("from_id"), "id": a.get("opaque_id"),
                                          "url": _asset_url(a.get("opaque_id"), a.get("url")),
                                          "sizeBytes": a.get("size_bytes"),
                                          "contentType": a.get("content_type")} for a in rows]}
    if args.assets_cmd == "read":
        if not ASSET_ID_RE.match(args.id):
            raise bad_input("not a valid asset id (32 hex characters)")
        b = boot(api, ref)
        raw, ctype = fetch_content(api, slug, b, f"_blob/{args.id}", prefer_source=False)
        entry = {"slug": slug, "id": args.id, "contentType": ctype, "bytes": len(raw)}
        if args.out:
            Path(args.out).write_bytes(raw)
            entry["saved"] = args.out
        else:
            _attach_content(entry, raw, ctype)
        return entry
    raise bad_input("unknown assets command")


def _asset_url(aid, url):
    if isinstance(url, str) and aid and url in (f"_blob/{aid}", f"/_blob/{aid}"):
        return url
    return f"/_blob/{aid}" if aid else url


def assets_list(api, slug, after=None, all_pages=False):
    assets, usage, nxt, pages = [], None, after, 0
    while True:
        body = {"limit": 50}
        if nxt:
            body["after"] = nxt
        r = api.call("POST", f"/api/frame/blob/{slug}/agent-list", json_body=body, timeout=30)
        if not r.ok:
            raise _asset_err(r, "asset listing")
        d = r.data if isinstance(r.data, dict) else {}
        for a in d.get("assets") or []:
            assets.append({"id": a.get("opaque_id"), "url": _asset_url(a.get("opaque_id"), a.get("url")),
                           "contentType": a.get("content_type"), "sizeBytes": a.get("size_bytes"),
                           "createdAt": a.get("created_at"),
                           **({"sha256": a["sha256"]} if "sha256" in a else {})})
        usage = d.get("usage", usage)
        nxt = d.get("next") or None
        pages += 1
        if not all_pages or not nxt or pages >= 100:
            break
    out = {"slug": slug, "assets": assets, "usage": usage}
    if nxt:
        out["next"] = nxt
    return out


# --------------------------------------------------------------------------- database
def _db_check_collection(col):
    if not COLLECTION_RE.match(col):
        raise bad_input('collection must be 1-15 "/"-separated segments (letters, digits, _ - . ~ : @ + '
                        'per segment; "." and ".." reserved)')
    n = col.count("/") + 1
    if n % 2 == 0:
        segs = col.split("/")
        raise bad_input(f"collection '{col}' has {n} segments, which makes it a document path: use "
                        f"collection '{'/'.join(segs[:-1])}' with doc_id '{segs[-1]}'", "parity")


def _db_check_doc(doc):
    if not SEGMENT_RE.match(doc):
        raise bad_input("doc_id must be one segment (letters, digits, _ - . ~ : @ +; '.' and '..' reserved)")


def _check_data(data, op):
    if not isinstance(data, dict):
        raise bad_input("document data must be a JSON object")

    def walk(v, in_array):
        if isinstance(v, dict):
            if v == {"__delete__": True}:
                if op != "update":
                    raise bad_input('{"__delete__": true} removes a field and is accepted only in update')
                if in_array:
                    raise bad_input('{"__delete__": true} is rejected inside arrays')
            for x in v.values():
                walk(x, in_array)
        elif isinstance(v, list):
            for x in v:
                walk(x, True)

    for v in data.values():
        walk(v, False)


class Db:
    def __init__(self, api, slug, as_level=None, require_version=False):
        self.api = api
        self.slug = slug
        self.as_level = as_level
        self.require_version = require_version
        self._me = None

    def call(self, verb, args, *, write=False, timeout=30):
        body = {"slug": self.slug, "verb": verb, "args": args}
        if self.as_level and verb != "whoami":
            body["asLevel"] = self.as_level
        if self.require_version and write and verb != "str_replace":
            body["requireVersion"] = True
        r = self.api.call("POST", "/api/frame/db/agent", json_body=body, timeout=timeout)
        return r

    def me(self):
        if self._me is None:
            r = self.call("whoami", {}, timeout=15)
            if not r.ok:
                raise db_error(r, "resolving 'me'")
            uid = r.data.get("id") if isinstance(r.data, dict) else None
            if not isinstance(uid, str) or not SEGMENT_RE.match(uid):
                raise CliError("malformed", "whoami returned an unreadable response", body=r.data)
            self._me = uid
        return self._me

    def resolve(self, collection):
        if collection == ME_PREFIX or collection.startswith(ME_PREFIX + "/"):
            return "data/users/" + self.me() + collection[len(ME_PREFIX):]
        return collection

    def address(self, collection, doc_id):
        _db_check_collection(collection)
        _db_check_doc(doc_id)
        col = self.resolve(collection)
        path = f"{col}/{doc_id}"
        if len(path) > DB_PATH_MAX:
            raise bad_input(f"the composed document path is {len(path)} bytes; the limit is {DB_PATH_MAX}")
        return path


DB_HINTS = {
    "not_found": "no such artifact, collection or document, or no access (deliberately indistinguishable)",
    "not_declared": "the artifact's published version does not declare the `db` capability",
    "version_mismatch": "the document changed since that version; re-read it and redo the write",
    "quota_exceeded": "a storage limit was reached; delete documents (retrying won't help)",
    "quota_or_rate": "over a rate or storage limit; retry later or write less",
    "edit_no_match": "old_str does not occur in the field",
    "edit_ambiguous": "old_str occurs more than once; make it unique or pass --replace-all",
    "edit_field_missing": "the field does not exist",
    "edit_field_not_string": "the field is not a string",
    "credential_rejected": "db verbs need a user-scoped OAuth token (not an org service key)",
    "busy": "lost to a concurrent change; re-read and retry deliberately",
}
_DB_CODE_MAP = {"invalid-argument": "invalid_argument", "not-found": "not_found",
                "resource-exhausted": "quota_or_rate", "resource_exhausted": "quota_or_rate",
                "data-unavailable": "store_unavailable", "unavailable": "store_unavailable",
                "upstream-error": "store_unavailable"}


def db_error(r, what):
    d = r.data
    err = d.get("error") if isinstance(d, dict) else None
    if r.status == 403 and isinstance(d, str) and "user-scoped OAuth token" in d:
        code = "credential_rejected"
    elif isinstance(err, dict) and isinstance(err.get("code"), str):
        code = _DB_CODE_MAP.get(err["code"], err["code"])
    else:
        code = {404: "not_found", 403: "not_found", 400: "invalid_argument", 422: "invalid_argument",
                413: "too_large", 429: "quota_or_rate", 409: "busy", 412: "busy",
                401: "upstream_auth"}.get(r.status, "store_unavailable")
    msg = f"db {what} failed ({code})"
    hint = DB_HINTS.get(code)
    extra = {}
    if isinstance(err, dict):
        for k in ("reason", "kind", "limit", "version", "index", "required"):
            if k in err:
                extra[k] = err[k]
        if code == "version_mismatch" and err.get("required") is True:
            hint = "the document already exists: read it and pass --if-version with its version"
        elif code == "version_mismatch" and "version" in err:
            hint = f"the document is now at version {err['version']}; re-read it and redo the write"
    if hint:
        msg += f": {hint}"
    return CliError(code, msg, status=r.status, body=d,
                    exit_code=4 if code in ("version_mismatch", "busy") else 1, **extra)


def _parse_value(s):
    try:
        return json.loads(s)
    except ValueError:
        return s


def _db_query_args(args):
    q = read_json_arg(args.query, "--query") if getattr(args, "query", None) else {}
    if not isinstance(q, dict):
        raise bad_input("--query must be a JSON object")
    where = list(q.get("where") or [])
    for f, op, v in getattr(args, "where", None) or []:
        where.append([f, op, _parse_value(v)])
    order_by = q.get("order_by")
    if getattr(args, "order_by", None):
        field, _, direction = args.order_by.partition(":")
        order_by = {"field": field, **({"direction": direction} if direction else {})}
    limit = args.limit if getattr(args, "limit", None) is not None else q.get("limit")
    cursor = args.cursor if getattr(args, "cursor", None) else q.get("cursor")
    if len(where) > DB_WHERE_MAX:
        raise bad_input(f"at most {DB_WHERE_MAX} where clauses")
    wire_where = []
    for clause in where:
        if not (isinstance(clause, list) and len(clause) == 3 and isinstance(clause[0], str)):
            raise bad_input("each where clause is [field, operator, value]")
        op = DB_OP_ALIASES.get(clause[1], clause[1])
        if op not in DB_OPS:
            raise bad_input(f"unknown operator {clause[1]!r}; use one of {', '.join(DB_OPS)} or == != < <= > >=")
        if op in ("in", "not-in") and not isinstance(clause[2], list):
            raise bad_input(f"operator {op} needs a JSON array value")
        wire_where.append({"field": clause[0], "op": op, "value": clause[2]})
    if order_by is not None:
        if not isinstance(order_by, dict) or not isinstance(order_by.get("field"), str):
            raise bad_input("order_by needs a field")
        if order_by.get("direction") not in (None, "asc", "desc"):
            raise bad_input("order_by direction must be asc or desc")
    if limit is not None and not (isinstance(limit, int) and 1 <= limit <= DB_QUERY_LIMIT_MAX):
        raise bad_input(f"limit must be 1-{DB_QUERY_LIMIT_MAX}")
    return wire_where, order_by, limit, cursor


def _doc_out(d):
    return {k: d[k] for k in ("id", "data", "version", "updatedAt") if k in d}


def _load_data(args, op):
    if args.data is not None and args.data_file is not None:
        raise bad_input("pass exactly one of --data or --data-file")
    if args.data is None and args.data_file is None:
        raise bad_input(f"{op} needs --data or --data-file")
    data = read_json_arg(args.data if args.data is not None else "@" + args.data_file, "document data")
    _check_data(data, op)
    return data


def cmd_db(api, args):
    ref = parse_ref(args.url)
    if args.as_level and args.as_level not in DB_LEVELS:
        raise bad_input(f"--as-level must be one of {', '.join(DB_LEVELS)}")
    db = Db(api, ref.slug, args.as_level, getattr(args, "require_version", False))
    op = args.db_cmd
    if op == "whoami":
        return {"slug": ref.slug, "me": db.me()}
    if op == "get":
        path = db.address(args.collection, args.doc_id)
        r = db.call("get", {"path": path}, timeout=15)
        if not r.ok:
            raise db_error(r, "read")
        d = r.data if isinstance(r.data, dict) else {}
        if not d.get("exists"):
            return {"found": False, "collection": args.collection, "doc_id": args.doc_id}
        doc = {"id": args.doc_id, "data": d.get("data") or {}}
        for k in ("version", "updatedAt"):
            if k in d:
                doc[k] = d[k]
        out = {"found": True, "collection": args.collection, "doc": doc}
        if d.get("authoredByOthers"):
            out["authoredByOthers"] = True
        return out
    if op in ("list", "query"):
        _db_check_collection(args.collection)
        col = db.resolve(args.collection)
        where, order_by, limit, cursor = _db_query_args(args)
        if op == "list" and (where or order_by):
            raise bad_input("where/order_by apply to query only")
        docs, pages, nxt = [], 0, cursor
        while True:
            a = {"collection": col}
            if limit is not None:
                a["limit"] = limit
            if nxt:
                a["cursor"] = nxt
            if op == "query" and where:
                a["where"] = where
            if op == "query" and order_by:
                a["orderBy"] = order_by
            r = db.call(op, a, timeout=15)
            if not r.ok:
                raise db_error(r, "read")
            d = r.data if isinstance(r.data, dict) else {}
            docs.extend(_doc_out(x) for x in d.get("docs") or [] if isinstance(x, dict))
            nxt = d.get("nextCursor") or None
            pages += 1
            if not args.all or not nxt or pages >= args.max_pages:
                break
        out = {"collection": args.collection, "docs": docs, "count": len(docs)}
        if nxt:
            out["next_cursor"] = nxt
        return out
    if op in ("set", "update", "delete", "str-replace"):
        path = db.address(args.collection, args.doc_id)
        if args.if_version is not None and args.if_version < 1:
            raise bad_input("--if-version must be a positive integer")
        a = {"path": path}
        verb = op.replace("-", "_")
        if op == "set":
            a.update(data=_load_data(args, "set"), replace=True)
        elif op == "update":
            a["data"] = _load_data(args, "update")
        elif op == "str-replace":
            a.update(field=args.field, old_str=args.old, new_str=args.new)
            if args.replace_all:
                a["replace_all"] = True
            a["idem"] = base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()
        if args.if_version is not None:
            a["ifVersion"] = args.if_version
        r = db.call(verb, a, write=True)
        if not r.ok:
            raise db_error(r, "write")
        d = r.data if isinstance(r.data, dict) else {}
        out = {"ok": True, "op": op, "collection": args.collection, "doc_id": args.doc_id}
        for k in ("version", "usage", "warnings"):
            if k in d:
                out[k] = d[k]
        return out
    if op == "batch":
        writes = read_json_arg(args.writes, "--writes")
        if not isinstance(writes, list) or not 1 <= len(writes) <= DB_BATCH_MAX:
            raise bad_input(f"--writes must be a JSON array of 1-{DB_BATCH_MAX} writes")
        ops, seen = [], set()
        for i, w in enumerate(writes):
            where = f"write {i + 1} of {len(writes)}"
            if not isinstance(w, dict) or w.get("op") not in ("set", "update", "delete"):
                raise bad_input(f"{where}: op must be set, update or delete")
            path = db.address(str(w.get("collection", "")), str(w.get("doc_id", "")))
            if path in seen:
                raise bad_input(f"{where} addresses {path} again; a batch writes each document at most once")
            seen.add(path)
            entry = {"op": w["op"], "path": path}
            if w["op"] in ("set", "update"):
                data = w.get("data")
                if data is None and w.get("file_path"):
                    data = read_json_arg("@" + w["file_path"], f"{where} file_path")
                _check_data(data, w["op"])
                entry["data"] = data
                if w["op"] == "set":
                    entry["replace"] = True
            if w.get("if_version") is not None:
                if not isinstance(w["if_version"], int) or w["if_version"] < 1:
                    raise bad_input(f"{where}: if_version must be a positive integer")
                entry["ifVersion"] = w["if_version"]
            ops.append(entry)
        body_size = len(json.dumps({"slug": ref.slug, "verb": "batch", "args": {"ops": ops}}).encode())
        if body_size > DB_BATCH_MAX_BYTES:
            raise bad_input(f"the batch serializes to {body_size} bytes; the limit is {DB_BATCH_MAX_BYTES}",
                            "batch_too_large")
        r = db.call("batch", {"ops": ops}, write=True, timeout=60)
        if not r.ok:
            e = db_error(r, "batch write")
            e.message += "; the batch is atomic, so nothing was written" if e.code != "store_unavailable" \
                else "; the batch outcome is unknown, read back before retrying"
            raise e
        d = r.data if isinstance(r.data, dict) else {}
        out = {"ok": True, "results": d.get("results") or []}
        for k in ("usage", "warnings"):
            if k in d:
                out[k] = d[k]
        return out
    raise bad_input("unknown db command")


def cmd_profiles(api, args):
    ref = parse_ref(args.url)
    if not 1 <= len(args.ids) <= 64:
        raise bad_input("give 1-64 user ids")
    r = api.call("POST", f"/api/frame/user/profiles/{urllib.parse.quote(ref.slug)}",
                 json_body={"ids": args.ids}, timeout=15)
    if not r.ok:
        e = fail(r, "user profiles")
        if isinstance(r.data, dict) and r.data.get("error") == "not_declared":
            e.code = "not_declared"
            e.message += " (the page must declare the `user` capability)"
        raise e
    profiles = r.data.get("profiles") if isinstance(r.data, dict) else None
    out = {}
    for uid in args.ids:
        p = profiles.get(uid) if isinstance(profiles, dict) else None
        out[uid] = {k: p[k] for k in ("name", "guest") if isinstance(p, dict) and k in p}
    return {"slug": ref.slug, "profiles": out}


# --------------------------------------------------------------------------- comments
def cmd_comments(api, args):
    ref = parse_ref(args.url)
    slug = urllib.parse.quote(ref.slug)
    if args.comments_cmd == "read":
        r = api.call("GET", f"/api/frame/comments/{slug}", query={"names": "1" if args.names else None},
                     timeout=15)
        if not r.ok:
            raise fail(r, "comments read")
        d = r.data if isinstance(r.data, dict) else {}
        threads = d.get("threads") or []
        if args.unresolved:
            threads = [t for t in threads if isinstance(t, dict) and not t.get("resolved_at")]
        out = {"slug": ref.slug, "threads": threads, "count": len(threads)}
        if "names" in d:
            out["names"] = d["names"]
        return redact(out)
    tid = args.thread_id
    if not UUID_RE.match(tid):
        raise bad_input("thread id must be a UUID (as `comments read` shows it)")
    if args.comments_cmd == "reply":
        if not args.text.strip():
            raise bad_input("reply text must not be empty")
        body = {"text": args.text}
        if args.id:
            body["id"] = args.id
        if args.continues:
            body["continues_reply_id"] = args.continues
        r = api.call("POST", f"/api/frame/comments/{slug}/{tid}", json_body=body, timeout=30,
                     retry_statuses=(429,))
        if r.status == 201 or r.ok:
            d = r.data if isinstance(r.data, dict) else {}
            return {"slug": ref.slug, "thread_id": d.get("thread_id", tid),
                    "comment_id": d.get("comment_id"), **({"replayed": True} if d.get("replayed") else {})}
        e = fail(r, "comment reply")
        text = _server_message(r.data)
        if r.status == 403 and "not activated on this thread" in text:
            e.code = "not_activated"
            e.message = ("comment reply refused: Claude is not activated on this thread (someone must "
                         "summon Claude on it in the page first)")
        raise e
    if args.comments_cmd == "resolve":
        r = api.call("POST", f"/api/frame/comments/{slug}/{tid}/session-resolve",
                     json_body={"resolved": True}, timeout=15)
        if r.status == 404 and not (isinstance(r.data, dict) and r.data.get("error") == "thread_not_found"):
            r = api.call("POST", f"/api/frame/comments/{slug}/{tid}/resolve",
                         json_body={"resolved": True}, timeout=15)
        if not r.ok:
            raise fail(r, "thread resolve")
        return {"slug": ref.slug, "thread_id": tid, "resolved": True}
    raise bad_input("unknown comments command")


# --------------------------------------------------------------------------- types / contract
def types_list(api, query=None, anthropic=False):
    q = {}
    if anthropic:
        q["scopes"] = "anthropic"
    if query:
        q["q"] = query
    r = api.call("GET", "/api/frame/types", query=q or None, timeout=15)
    if not r.ok:
        raise fail(r, "artifact type listing")
    rows = []
    for t in (r.data.get("types") or []) if isinstance(r.data, dict) else []:
        if isinstance(t, dict) and UUID_RE.match(str(t.get("slug", ""))):
            rows.append({"type_url": view_urls(api.c, t["slug"])["code_url"], "slug": t["slug"],
                         "title": t.get("title"), "description": t.get("description"),
                         **({"release": t["release"].get("version")} if isinstance(t.get("release"), dict) else {})})
    out = {"types": rows}
    if isinstance(r.data, dict) and r.data.get("next_page_token"):
        out["next_page_token"] = r.data["next_page_token"]
    return out


def list_type_instances(api, tslug, rel, limit):
    q = {"limit": str(min(max(limit, 1), 200))}
    if rel != "all":
        q["rel"] = rel
    r = api.call("GET", f"/api/frame/types/{urllib.parse.quote(tslug)}/instances", query=q, timeout=15)
    if not r.ok:
        raise fail(r, "listing artifacts made from a type")
    return redact({"type": tslug, "instances": r.data})


def cmd_types(api, args):
    if args.types_cmd == "list":
        return types_list(api, args.query, args.anthropic)
    tslug = parse_ref(args.type_url).slug
    if args.types_cmd == "describe":
        r = api.call("GET", f"/api/frame/types/{urllib.parse.quote(tslug)}", timeout=15)
        if not r.ok:
            raise fail(r, "describe artifact type")
        return redact(r.data)
    if args.types_cmd == "file":
        enc = clean_rel_path(args.path, what="type file path")
        r = api.call("GET", f"/api/frame/types/{urllib.parse.quote(tslug)}/files/{enc}", timeout=15)
        if not r.ok:
            raise fail(r, "read artifact type file")
        return r.data
    if args.types_cmd == "instances":
        return list_type_instances(api, tslug, args.rel, args.limit)
    raise bad_input("unknown types command")


def cmd_contract(api, args):
    ver = args.version
    if args.prompt or args.dts:
        if ver in (None, "latest"):
            ver = contract_latest(api)["version"]
        path = f"/api/frame/contract/{urllib.parse.quote(ver)}/" + (
            "prompt" if args.prompt else f"{urllib.parse.quote(args.dts)}.d.ts")
        r = api.call("GET", path, timeout=15)
        if not r.ok:
            raise fail(r, "contract text fetch")
        return {"version": ver, "text": r.raw.decode("utf-8", "replace")}
    if ver in (None, "latest"):
        return contract_latest(api)
    r = api.call("GET", f"/api/frame/contract/{urllib.parse.quote(ver)}", timeout=15)
    if not r.ok:
        raise fail(r, f"contract {ver} fetch")
    return r.data


# --------------------------------------------------------------------------- argparse
def build_parser():
    p = argparse.ArgumentParser(
        prog="artifact.py",
        description="Unofficial stdlib-only CLI for Claude Artifacts. Token: $CLAUDE_CODE_OAUTH_TOKEN only. "
                    "Output: JSON on stdout; errors as JSON on stderr.")
    p.add_argument("--version", action="version", version=f"artifact.py {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("publish", help="create or update an artifact (page, files, capabilities)")
    s.add_argument("file_path", nargs="?", metavar="FILE", help=".html (or .md) page to publish")
    s.add_argument("--url", help="existing artifact to update in place (keeps its URL)")
    s.add_argument("--title")
    s.add_argument("--description")
    s.add_argument("--label", help="short name for this version")
    s.add_argument("--icon", help="one short generic word for the tab icon, e.g. chart")
    s.add_argument("--favicon", help="emoji tab icon (older field)")
    s.add_argument("--lang", help="html lang attribute when the page is wrapped")
    s.add_argument("--raw", action="store_true", help="send FILE as-is, without the <head> wrapper")
    s.add_argument("--file", action="append", metavar="PUBLISHED=LOCAL",
                   help="supporting file to publish beside the page (repeatable)")
    s.add_argument("--file-type", action="append", metavar="PUBLISHED=TYPE",
                   help="content type for a --file whose extension doesn't say it")
    s.add_argument("--remove", action="append", metavar="PUBLISHED", help="remove a published file (update only)")
    s.add_argument("--copy", action="append", metavar="PUBLISHED=SRC_URL:SRC_PATH[@VER]",
                   help="copy a published file from another artifact (server side)")
    s.add_argument("--capabilities", metavar="JSON|@FILE",
                   help="runtime capabilities to declare, e.g. '{\"db\":{}}'; {} clears; omit to keep")
    s.add_argument("--contract", metavar="VERSION|latest", help="pin the runtime contract; default keeps it")
    s.add_argument("--force", action="store_true", help="overwrite without the base-version check")
    s.add_argument("--base-version", help="version this publish builds on (default: read the current one)")
    s.add_argument("--type-url", help="create a new artifact from this artifact type instead of FILE")
    s.add_argument("--idempotency-key", help="with --type-url: reuse to make a retried create safe")
    s.add_argument("--dry-run", action="store_true", help="validate and show the request; send no write")

    s = sub.add_parser("list", help="list artifacts, types, an artifact's files or assets")
    s.add_argument("--scope", default="mine", choices=["mine", "shared", "all", "types", "files", "assets"])
    s.add_argument("--url", help="artifact for --scope files/assets")
    s.add_argument("--type", help="list artifacts made from this type (type URL)")
    s.add_argument("--query", help="with --scope types: filter text")
    s.add_argument("--anthropic", action="store_true", help="with --scope types: Anthropic's types only")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--page-token", help="next_page_token (or assets `next`) from a previous listing")
    s.add_argument("--all", action="store_true", help="follow every page")

    s = sub.add_parser("read", help="read an artifact's page, or published files")
    s.add_argument("url")
    s.add_argument("--path", action="append", help="published file path to read (repeatable)")
    s.add_argument("--version", help="read a past version id (see `versions`)")
    s.add_argument("--meta", action="store_true", help="metadata only, no content fetch")
    s.add_argument("--served", action="store_true", help="read the served page, not the stored source")
    s.add_argument("--out", help="save the single page/file to this path instead of printing it")
    s.add_argument("--out-dir", help="save each read file under this directory")

    s = sub.add_parser("info", help="stored contract pin, capabilities and type lock")
    s.add_argument("url")

    s = sub.add_parser("delete", help="permanently delete an artifact (needs --yes)")
    s.add_argument("url")
    s.add_argument("--yes", action="store_true")

    for name in ("pin", "unpin"):
        s = sub.add_parser(name, help=f"{name} an artifact in the claude.ai sidebar")
        s.add_argument("url")

    s = sub.add_parser("retitle", help="rename an artifact")
    s.add_argument("url")
    s.add_argument("title")

    s = sub.add_parser("versions", help="list an artifact's kept versions (owner only)")
    s.add_argument("url")

    s = sub.add_parser("diag", help="runtime diagnostics recorded for a version")
    s.add_argument("url")
    s.add_argument("--version")

    s = sub.add_parser("assets", help="artifact asset store")
    asub = s.add_subparsers(dest="assets_cmd", required=True)
    a = asub.add_parser("upload")
    a.add_argument("url")
    a.add_argument("files", nargs="+")
    a = asub.add_parser("list")
    a.add_argument("url")
    a.add_argument("--after")
    a.add_argument("--all", action="store_true")
    a = asub.add_parser("delete")
    a.add_argument("url")
    a.add_argument("id")
    a.add_argument("--yes", action="store_true")
    a = asub.add_parser("copy")
    a.add_argument("url", help="destination artifact")
    a.add_argument("ids", nargs="+")
    a.add_argument("--from", dest="from_url", required=True, help="source artifact")
    a.add_argument("--cross-org-ok", action="store_true")
    a = asub.add_parser("read")
    a.add_argument("url")
    a.add_argument("id")
    a.add_argument("--out")

    s = sub.add_parser("db", help="artifact database (ArtifactData)")
    dsub = s.add_subparsers(dest="db_cmd", required=True)

    def db_common(x):
        x.add_argument("--as-level", choices=DB_LEVELS, help="act with only this access level")

    d = dsub.add_parser("whoami")
    d.add_argument("url")
    db_common(d)
    d = dsub.add_parser("get")
    d.add_argument("url")
    d.add_argument("collection")
    d.add_argument("doc_id")
    db_common(d)
    for name in ("list", "query"):
        d = dsub.add_parser(name)
        d.add_argument("url")
        d.add_argument("collection")
        d.add_argument("--limit", type=int)
        d.add_argument("--cursor")
        d.add_argument("--all", action="store_true", help="follow next_cursor")
        d.add_argument("--max-pages", type=int, default=100)
        if name == "query":
            d.add_argument("--where", nargs=3, action="append", metavar=("FIELD", "OP", "VALUE"),
                           help="VALUE is parsed as JSON when it is valid JSON, else used as a string")
            d.add_argument("--order-by", metavar="FIELD[:asc|desc]")
            d.add_argument("--query", metavar="JSON|@FILE",
                           help='{"where":[[f,op,v]],"order_by":{"field":..},"limit":..,"cursor":..}')
        db_common(d)
    for name in ("set", "update", "delete", "str-replace"):
        d = dsub.add_parser(name)
        d.add_argument("url")
        d.add_argument("collection")
        d.add_argument("doc_id")
        if name in ("set", "update"):
            d.add_argument("--data", metavar="JSON")
            d.add_argument("--data-file", metavar="PATH")
        if name == "str-replace":
            d.add_argument("--field", required=True)
            d.add_argument("--old", required=True)
            d.add_argument("--new", required=True)
            d.add_argument("--replace-all", action="store_true")
        d.add_argument("--if-version", type=int, help="write only if the document is at this version")
        d.add_argument("--require-version", action="store_true",
                       help="ask the server to refuse writes to existing docs without --if-version")
        db_common(d)
    d = dsub.add_parser("batch")
    d.add_argument("url")
    d.add_argument("--writes", required=True, metavar="JSON|@FILE",
                   help="[{op, collection, doc_id, data|file_path, if_version}] (1-50)")
    d.add_argument("--require-version", action="store_true")
    db_common(d)

    s = sub.add_parser("profiles", help="display names for user ids seen in db data")
    s.add_argument("url")
    s.add_argument("ids", nargs="+")

    s = sub.add_parser("comments", help="comment threads (ArtifactComments)")
    csub = s.add_subparsers(dest="comments_cmd", required=True)
    x = csub.add_parser("read")
    x.add_argument("url")
    x.add_argument("--names", action="store_true", help="also return commenter display names")
    x.add_argument("--unresolved", action="store_true")
    x = csub.add_parser("reply")
    x.add_argument("url")
    x.add_argument("thread_id")
    x.add_argument("text")
    x.add_argument("--id", help="client comment id (makes a retry idempotent)")
    x.add_argument("--continues", help="comment id this reply continues")
    x = csub.add_parser("resolve")
    x.add_argument("url")
    x.add_argument("thread_id")

    s = sub.add_parser("types", help="artifact types (templates)")
    tsub = s.add_subparsers(dest="types_cmd", required=True)
    t = tsub.add_parser("list")
    t.add_argument("--query")
    t.add_argument("--anthropic", action="store_true")
    t = tsub.add_parser("describe")
    t.add_argument("type_url")
    t = tsub.add_parser("file")
    t.add_argument("type_url")
    t.add_argument("path")
    t = tsub.add_parser("instances")
    t.add_argument("type_url")
    t.add_argument("--rel", default="all", choices=["mine", "shared", "all"])
    t.add_argument("--limit", type=int, default=200)

    s = sub.add_parser("contract", help="the runtime capability contract")
    s.add_argument("version", nargs="?", default="latest")
    s.add_argument("--prompt", action="store_true", help="the contract's prompt text")
    s.add_argument("--dts", metavar="NAME", help="a capability's TypeScript definitions")
    return p


COMMANDS = {
    "publish": cmd_publish, "list": cmd_list, "read": cmd_read, "info": cmd_info,
    "delete": cmd_delete, "pin": lambda a, x: cmd_pin(a, x, True),
    "unpin": lambda a, x: cmd_pin(a, x, False), "retitle": cmd_retitle,
    "versions": cmd_versions, "diag": cmd_diag, "assets": cmd_assets, "db": cmd_db,
    "profiles": cmd_profiles, "comments": cmd_comments, "types": cmd_types,
    "contract": cmd_contract,
}


def main(argv=None, out=None, err=None):
    out = out or sys.stdout
    err = err or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        api = Api()
        result = COMMANDS[args.cmd](api, args)
    except CliError as e:
        err.write(json.dumps(e.as_dict(), indent=2, ensure_ascii=False) + "\n")
        return e.exit_code
    out.write(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
