#!/usr/bin/env python3
"""
publish_artifact.py — publish a Claude Artifact ("frame") from the command line,
without going through the Claude Code Artifact tool.

Reverse-engineered from the Claude Code CLI binary (v2.1.212). It replicates the
"direct upload" lane the CLI uses internally:

    POST {BASE_API_URL}/api/frame/deploy/direct   (content inline)
      -> { slug, version, read?, shared? }
    view URL: {CLAUDE_AI_ORIGIN}/code/artifact/<slug>

Stateless & portable: Python 3 stdlib only, no third-party deps, and it needs NO
local Claude Code install or login as long as you supply an OAuth token via
`--token` or `$CLAUDE_CODE_OAUTH_TOKEN`. Drop this one file into any harness / CI /
machine and it works. (On a machine where `claude` is logged in, it can also read
the stored credentials automatically — see auth priority below.)

Usage:
    publish_artifact.py page.html
    publish_artifact.py page.html --title "My Page" --favicon "📊" \
        --description "A demo page"
    publish_artifact.py page.md --title "Notes"        # light markdown -> html
    publish_artifact.py page.html --slug <slug>        # update existing artifact
    publish_artifact.py --list                         # list your artifacts
    publish_artifact.py page.html --token "$TOK"       # fully stateless

Auth priority (see references/auth.md for how to obtain a token anywhere):
    1. --token <tok>                   explicit, most portable
    2. $CLAUDE_CODE_OAUTH_TOKEN        env var (ideal for CI); `claude setup-token`
    3. ~/.claude/.credentials.json     claudeAiOauth.accessToken (+ auto-refresh)

Environment overrides:
    CLAUDE_CODE_OAUTH_TOKEN            use this bearer token directly
    CLAUDE_CODE_ARTIFACTS_API_BASE_URL override frame API base (default api.anthropic.com)
    ARTIFACT_ENV=staging|prod          pick base/origin preset (default prod)
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

# ---- constants lifted verbatim from the CLI binary (prod config `Bol`) --------
PRESETS = {
    "prod": {
        "BASE_API_URL": "https://api.anthropic.com",
        "CLAUDE_AI_ORIGIN": "https://claude.ai",
        "TOKEN_URL": "https://platform.claude.com/v1/oauth/token",
    },
    "staging": {
        "BASE_API_URL": "https://api.anthropic.com",
        "CLAUDE_AI_ORIGIN": "https://claude-ai.staging.ant.dev",
        "TOKEN_URL": "https://platform.claude.com/v1/oauth/token",
    },
}
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"   # Claude Code prod OAuth client
CREDS_PATH = Path.home() / ".claude" / ".credentials.json"
MAX_BYTES = 16 * 1024 * 1024                         # Yye = 16MB, hard cap
# exact <head> the CLI injects (LGg)
HEAD_STYLE = (
    "<style>:root{color-scheme:light}"
    "body{margin:0;padding:0;font:14px -apple-system,BlinkMacSystemFont,sans-serif;"
    "background:#faf9f5;color:#141413}img{max-width:100%}</style>"
)
FRAME_HEADERS = {
    "X-Frame-CP": "go",
    "X-Frame-Surface": "code",
    "X-Frame-Platform": "cli",
    "anthropic-beta": "oauth-2025-04-20",
}


def cfg():
    env = os.environ.get("ARTIFACT_ENV", "prod")
    c = dict(PRESETS.get(env, PRESETS["prod"]))
    override = os.environ.get("CLAUDE_CODE_ARTIFACTS_API_BASE_URL")
    if override:
        c["BASE_API_URL"] = override.rstrip("/")
    return c


# ---- auth --------------------------------------------------------------------
def _read_creds():
    if not CREDS_PATH.exists():
        return None
    try:
        return json.loads(CREDS_PATH.read_text()).get("claudeAiOauth")
    except Exception:
        return None


def _refresh(refresh_token, token_url):
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        token_url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        tok = json.loads(r.read())
    # persist the rotated tokens back so the file stays valid for the CLI too
    oauth = _read_creds() or {}
    oauth["accessToken"] = tok["access_token"]
    if tok.get("refresh_token"):
        oauth["refreshToken"] = tok["refresh_token"]
    if tok.get("expires_in"):
        oauth["expiresAt"] = int(time.time() * 1000) + int(tok["expires_in"]) * 1000
    try:
        data = {}
        if CREDS_PATH.exists():
            data = json.loads(CREDS_PATH.read_text())
        data["claudeAiOauth"] = oauth
        CREDS_PATH.write_text(json.dumps(data))
        os.chmod(CREDS_PATH, 0o600)
    except Exception as e:
        print(f"war: could not persist refreshed token: {e}", file=sys.stderr)
    return tok["access_token"]


def get_token(token_url, explicit=None):
    """Resolve a bearer token across all scenarios, refreshing if expired.

    Priority (most explicit / most portable first):
      1. explicit --token argument
      2. $CLAUDE_CODE_OAUTH_TOKEN
      3. ~/.claude/.credentials.json -> claudeAiOauth.accessToken (refresh if expired)
    The first two need NO local Claude install or login — that is what makes this
    skill portable across harnesses/CI. The third is a convenience on machines
    where `claude` is already logged in.
    """
    # 1) explicit token argument wins
    if explicit:
        return explicit
    # 2) explicit env token
    env_tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env_tok:
        return env_tok
    # 3) stored OAuth credentials
    oauth = _read_creds()
    if not oauth or not oauth.get("accessToken"):
        sys.exit("not authenticated: no CLAUDE_CODE_OAUTH_TOKEN and no "
                 f"claudeAiOauth.accessToken in {CREDS_PATH}. Run `claude` and /login.")
    expires_at = oauth.get("expiresAt")  # epoch ms
    expired = isinstance(expires_at, (int, float)) and time.time() * 1000 >= (expires_at - 60_000)
    if expired:
        rt = oauth.get("refreshToken")
        if not rt:
            sys.exit("access token expired and no refresh token available; run `claude` /login.")
        try:
            return _refresh(rt, token_url)
        except urllib.error.HTTPError as e:
            sys.exit(f"token refresh failed ({e.code}): {e.read().decode('utf-8','replace')[:300]}")
    return oauth["accessToken"]


# ---- page building -----------------------------------------------------------
def _md_to_html(md):
    """Best-effort markdown rendering. Uses python-markdown if importable,
    else a minimal fallback (the real CLI renders server-side)."""
    try:
        import markdown  # type: ignore
        return markdown.markdown(md, extensions=["fenced_code", "tables"])
    except Exception:
        import html as _h
        return "<pre style='white-space:pre-wrap;padding:2rem'>" + _h.escape(md) + "</pre>"


def build_page(path, lang=None):
    raw = Path(path).read_text(encoding="utf-8")
    ext = Path(path).suffix.lower()
    if ext in (".md", ".markdown"):
        body = _md_to_html(raw)
    else:
        body = raw
    # sanitizer parity with the CLI (Ast)
    low = body.lower()
    if "</script" in low:
        sys.exit("content rejected: page contains a </script> tag (breaks the inline bundle).")
    if "<!--" in body and "<script" in low:
        sys.exit("content rejected: page mixes <!-- comments with <script> (bundle sanitizer).")
    lang_attr = f' lang="{lang}"' if lang else ""
    html = (f"<!doctype html><html{lang_attr}><head><meta charset=utf8>"
            '<meta name=viewport content="width=device-width,initial-scale=1">'
            f"{HEAD_STYLE}</head><body>\n{body}\n</body></html>")
    size = len(html.encode("utf-8"))
    if size > MAX_BYTES:
        sys.exit(f"too large: {size/1024/1024:.1f}MB (max {MAX_BYTES//1024//1024}MB)")
    return html


# ---- API ---------------------------------------------------------------------
def api(method, url, token, payload=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    headers.update(FRAME_HEADERS)
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"_raw": raw}


def publish(args):
    c = cfg()
    token = get_token(c["TOKEN_URL"], args.token)
    html = build_page(args.file, args.lang)
    payload = {
        "title": args.title or Path(args.file).stem,
        "favicon": args.favicon or "📄",
        "content": html,
    }
    if args.description:
        payload["description"] = args.description
    if args.label:
        payload["label"] = args.label
    if args.slug:
        payload["slug"] = args.slug
    if args.force:
        payload["force"] = True
    url = f"{c['BASE_API_URL']}/api/frame/deploy/direct"
    status, body = api("POST", url, token, payload)
    if status < 200 or status >= 300:
        print(f"deploy failed ({status}): {json.dumps(body)[:600]}", file=sys.stderr)
        sys.exit(1)
    slug = body.get("slug")
    if not slug:
        print(f"deploy returned incomplete response: {json.dumps(body)[:600]}", file=sys.stderr)
        sys.exit(1)
    view = f"{c['CLAUDE_AI_ORIGIN']}/code/artifact/{slug}"
    print(view)
    if args.json:
        print(json.dumps({**body, "url": view}, indent=2), file=sys.stderr)
    return view


def do_list(args):
    c = cfg()
    token = get_token(c["TOKEN_URL"], args.token)
    status, body = api("GET", f"{c['BASE_API_URL']}/api/frame/frames?limit={args.limit}", token, timeout=30)
    if status < 200 or status >= 300:
        print(f"list failed ({status}): {json.dumps(body)[:600]}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(body, indent=2))


def main():
    p = argparse.ArgumentParser(description="Publish a Claude Artifact from the CLI.")
    p.add_argument("file", nargs="?", help="path to .html or .md file")
    p.add_argument("--title")
    p.add_argument("--favicon", help="one or two emoji for the tab icon")
    p.add_argument("--description")
    p.add_argument("--label", help="short version label")
    p.add_argument("--slug", help="update an existing artifact instead of creating one")
    p.add_argument("--lang", help="html lang attribute, e.g. en")
    p.add_argument("--force", action="store_true", help="overwrite without base-version check")
    p.add_argument("--token", help="OAuth bearer token to use directly (overrides env "
                                   "and credentials file; makes the tool fully stateless)")
    p.add_argument("--list", action="store_true", help="list your artifacts and exit")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--json", action="store_true", help="also print raw response to stderr")
    args = p.parse_args()
    if args.list:
        do_list(args)
        return
    if not args.file:
        p.error("a file argument is required (or use --list)")
    publish(args)


if __name__ == "__main__":
    main()
