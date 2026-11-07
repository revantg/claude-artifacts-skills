#!/usr/bin/env python3
"""Offline tests for artifact.py against a local mock of the frame API (http.server).

No network, no real token: the token is a synthetic placeholder and every test checks
it never reaches stdout/stderr. Run:  python3 -m unittest discover -s tests -v
"""
import base64
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import artifact  # noqa: E402

TOKEN = "test-placeholder-token-0000"
SLUG = "0d1e2f3a-4b5c-4d6e-8f70-8192a3b4c5d6"
SLUG2 = "11111111-2222-4333-8444-555555555555"
THREAD = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ASSET = "0123456789abcdef0123456789abcdef"
URL = f"https://claude.ai/artifact/{artifact.b58_from_uuid(SLUG)}"


class MockServer:
    """Scripted routes: first matching (method, regex) with remaining uses answers."""

    def __init__(self):
        self.routes = []
        self.requests = []
        mock_self = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                rec = {"method": self.command, "path": self.path,
                       "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
                try:
                    rec["json"] = json.loads(body) if body else None
                except ValueError:
                    rec["json"] = None
                mock_self.requests.append(rec)
                for route in mock_self.routes:
                    if route["method"] == self.command and re.search(route["pattern"], self.path) and route["left"] != 0:
                        route["left"] -= 1
                        status, hdrs, payload = route["respond"](rec)
                        self.send_response(status)
                        for k, v in hdrs.items():
                            self.send_header(k, v)
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                self.send_response(599)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = do_DELETE = do_PATCH = do_PUT = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def add(self, method, pattern, status=200, body=None, headers=None, times=-1, raw=None):
        def respond(_rec):
            hdrs = dict(headers or {})
            if raw is not None:
                payload = raw
                hdrs.setdefault("Content-Type", "application/octet-stream")
            elif isinstance(body, str):
                payload = body.encode()
                hdrs.setdefault("Content-Type", "text/plain")
            else:
                payload = json.dumps(body if body is not None else {}).encode()
                hdrs.setdefault("Content-Type", "application/json")
            return status, hdrs, payload
        self.routes.append({"method": method, "pattern": pattern, "respond": respond, "left": times})

    def find(self, method, pattern):
        return [r for r in self.requests if r["method"] == method and re.search(pattern, r["path"])]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.srv = MockServer()
        base = f"http://127.0.0.1:{self.srv.port}"
        self.env = mock.patch.dict(os.environ, {
            "CLAUDE_CODE_OAUTH_TOKEN": TOKEN,
            "CLAUDE_CODE_ARTIFACTS_API_BASE_URL": base,
            "ARTIFACT_CONTENT_ORIGIN": base + "/_content/{slug}",
        }, clear=False)
        self.env.start()
        os.environ.pop("ARTIFACT_ENV", None)
        os.environ.pop("ARTIFACT_ALLOW_CUSTOM_BASE", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.srv.close()
        self.env.stop()
        self.tmp.cleanup()

    def run_cli(self, *argv, expect=0):
        out, err = io.StringIO(), io.StringIO()
        code = artifact.main(list(argv), out=out, err=err)
        so, se = out.getvalue(), err.getvalue()
        self.assertNotIn(TOKEN, so)
        self.assertNotIn(TOKEN, se)
        self.assertEqual(code, expect, f"exit {code}; stdout={so[:800]} stderr={se[:800]}")
        return json.loads(so) if so.strip() else json.loads(se)

    def boot_route(self, ver="v1-abc", **extra):
        body = {"ver": ver, "assetToken": "asset-placeholder", "title": "Old Title",
                "perm": {"role": "owner", "mode": "private"}, **extra}
        self.srv.add("GET", rf"^/api/frame/{SLUG}\?", body=body)

    def page(self, text="<h1>Hello</h1>", name="page.html"):
        p = self.dir / name
        p.write_text(text, encoding="utf-8")
        return str(p)


# ---------------------------------------------------------------------------- auth & config
class AuthAndConfig(Base):
    def test_missing_token_exits_3_without_request(self):
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN")
        e = self.run_cli("pin", URL, expect=3)
        self.assertEqual(e["error"]["code"], "no_token")
        self.assertEqual(self.srv.requests, [])

    def test_non_allowlisted_base_refused(self):
        os.environ["CLAUDE_CODE_ARTIFACTS_API_BASE_URL"] = "https://evil.example.com"
        e = self.run_cli("pin", URL, expect=2)
        self.assertIn("ARTIFACT_ALLOW_CUSTOM_BASE", e["error"]["message"])

    def test_frame_headers_and_bearer(self):
        self.srv.add("POST", r"^/api/frame/favorite/", status=204, body="")
        self.run_cli("pin", URL)
        h = self.srv.requests[0]["headers"]
        self.assertEqual(h["authorization"], f"Bearer {TOKEN}")
        self.assertEqual(h["x-frame-cp"], "go")
        self.assertEqual(h["x-frame-surface"], "code")
        self.assertEqual(h["x-frame-platform"], "cli")
        self.assertEqual(h["anthropic-beta"], "oauth-2025-04-20")

    def test_redirect_not_followed(self):
        self.srv.add("POST", r"^/api/frame/favorite/", status=302,
                     headers={"Location": f"http://127.0.0.1:{self.srv.port}/elsewhere"}, body="")
        self.srv.add("GET", r"^/elsewhere", body={})
        self.run_cli("pin", URL, expect=1)
        self.assertEqual(self.srv.find("GET", r"^/elsewhere"), [])

    def test_retry_once_on_429_with_retry_after(self):
        self.srv.add("POST", r"^/api/frame/favorite/", status=429, headers={"Retry-After": "0"}, body="", times=1)
        self.srv.add("POST", r"^/api/frame/favorite/", status=204, body="")
        out = self.run_cli("pin", URL)
        self.assertTrue(out["pinned"])
        self.assertEqual(len(self.srv.find("POST", r"^/api/frame/favorite/")), 2)


class Refs(unittest.TestCase):
    def test_parse_forms(self):
        short = artifact.b58_from_uuid(SLUG)
        for s in (SLUG, short, f"https://claude.ai/artifact/{short}", f"https://claude.ai/code/artifact/{SLUG}",
                  f"https://claude.ai/artifact/my-page-{short}?x=1", f"https://{SLUG}.frame.claudeusercontent.com/"):
            self.assertEqual(artifact.parse_ref(s).slug, SLUG, s)
        self.assertEqual(artifact.uuid_from_b58(short), SLUG)

    def test_bad_ref(self):
        with self.assertRaises(artifact.CliError):
            artifact.parse_ref("https://example.com/not-an-artifact")

    def test_redact_keeps_page_tokens(self):
        r = artifact.redact({"assetToken": "x", "next_page_token": "p", "nested": [{"syncToken": "y"}]})
        self.assertEqual(r["assetToken"], "[redacted]")
        self.assertEqual(r["next_page_token"], "p")
        self.assertEqual(r["nested"][0]["syncToken"], "[redacted]")


# ---------------------------------------------------------------------------- publish
class Publish(Base):
    def deploy_ok(self, slug=SLUG, ver="v2-new"):
        self.srv.add("POST", r"^/api/frame/deploy/direct", body={"slug": slug, "version": ver,
                                                                 "contract": "0.1.12", "read": "owner"})

    def test_create_page(self):
        self.deploy_ok()
        out = self.run_cli("publish", self.page(), "--title", "T", "--icon", "chart", "--description", "D")
        req = self.srv.find("POST", r"^/api/frame/deploy/direct")[0]["json"]
        self.assertEqual(req["title"], "T")
        self.assertEqual(req["icon"], "chart")
        self.assertEqual(req["description"], "D")
        self.assertNotIn("slug", req)
        self.assertNotIn("force", req)
        self.assertTrue(req["content"].startswith("<!doctype html><html><head><meta charset=utf8>"))
        self.assertIn("<h1>Hello</h1>", req["content"])
        self.assertEqual(out["slug"], SLUG)
        self.assertEqual(out["url"], URL)

    def test_full_document_sent_as_is_and_title_from_tag(self):
        self.deploy_ok()
        doc = "<!doctype html><html><head><title>From Tag</title></head><body>x</body></html>"
        self.run_cli("publish", self.page(doc))
        req = self.srv.find("POST", r"^/api/frame/deploy/direct")[0]["json"]
        self.assertEqual(req["content"], doc)
        self.assertEqual(req["title"], "From Tag")

    def test_update_builds_on_read_version(self):
        self.boot_route(ver="v1-abc")
        self.srv.add("GET", r"^/api/frame/read/", body={"contract": "0.1.10", "capabilities": {"db": {}}})
        self.deploy_ok()
        self.run_cli("publish", self.page(), "--url", URL)
        req = self.srv.find("POST", r"^/api/frame/deploy/direct")[0]["json"]
        self.assertEqual(req["slug"], SLUG)
        self.assertEqual(req["baseVersion"], "v1-abc")
        self.assertNotIn("force", req)
        self.assertEqual(req["contract"], "0.1.10")       # stored pin echoed
        self.assertNotIn("capabilities", req)            # omitted: carried forward
        self.assertEqual(req["title"], "Old Title")      # current title kept

    def test_update_force(self):
        self.srv.add("GET", r"^/api/frame/read/", status=404, body="not found")
        self.deploy_ok()
        self.run_cli("publish", self.page(), "--url", URL, "--force", "--title", "New")
        req = self.srv.find("POST", r"^/api/frame/deploy/direct")[0]["json"]
        self.assertTrue(req["force"])
        self.assertNotIn("baseVersion", req)
        self.assertEqual(self.srv.find("GET", rf"^/api/frame/{SLUG}\?"), [])

    def test_conflict_exit_4(self):
        self.boot_route()
        self.srv.add("GET", r"^/api/frame/read/", body={})
        self.srv.add("POST", r"^/api/frame/deploy/direct", status=409, body={"error": "conflict", "live": "v9"})
        e = self.run_cli("publish", self.page(), "--url", URL, "--title", "T", expect=4)
        self.assertEqual(e["error"]["code"], "conflict")

    def test_capabilities_on_create_fetch_latest_contract(self):
        self.srv.add("GET", r"^/api/frame/contract/latest", body={"version": "0.1.12", "capabilities": ["db", "user"]})
        self.deploy_ok()
        self.run_cli("publish", self.page(), "--title", "T", "--capabilities", '{"db":{},"user":{}}')
        req = self.srv.find("POST", r"^/api/frame/deploy/direct")[0]["json"]
        self.assertEqual(req["contract"], "0.1.12")
        self.assertEqual(req["capabilities"], {"db": {}, "user": {}})

    def test_clear_capabilities_with_empty_object(self):
        self.boot_route()
        self.srv.add("GET", r"^/api/frame/read/", body={"contract": "0.1.12", "capabilities": {"db": {}}})
        self.deploy_ok()
        self.run_cli("publish", self.page(), "--url", URL, "--title", "T", "--capabilities", "{}")
        req = self.srv.find("POST", r"^/api/frame/deploy/direct")[0]["json"]
        self.assertEqual(req["capabilities"], {})
        self.assertEqual(req["contract"], "0.1.12")

    def test_icon_refused_then_retried_without(self):
        self.srv.add("POST", r"^/api/frame/deploy/direct", status=400, body={"error": 'unknown field "icon"'}, times=1)
        self.deploy_ok()
        self.run_cli("publish", self.page(), "--title", "T", "--icon", "chart")
        reqs = self.srv.find("POST", r"^/api/frame/deploy/direct")
        self.assertEqual(len(reqs), 2)
        self.assertIn("icon", reqs[0]["json"])
        self.assertNotIn("icon", reqs[1]["json"])

    def test_inline_files_create(self):
        self.deploy_ok()
        css = self.dir / "s.css"
        css.write_text("body{color:red}")
        png = self.dir / "a.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        self.run_cli("publish", self.page(), "--title", "T", "--file", f"style.css={css}", "--file", f"img/a.png={png}")
        req = self.srv.find("POST", r"^/api/frame/deploy/direct")[0]["json"]
        self.assertEqual(req["mode"], "replace")
        self.assertNotIn("content", req)
        m = req["manifest"]
        self.assertEqual(m["index.html"]["contentType"], "text/html")
        self.assertEqual(m["style.css"], {"content": "body{color:red}", "contentType": "text/css"})
        self.assertEqual(m["img/a.png"]["contentType"], "image/png")
        self.assertEqual(base64.b64decode(m["img/a.png"]["content"]), png.read_bytes())

    def test_remove_file_patch_mode(self):
        self.boot_route(ver="v1-abc")
        self.srv.add("GET", r"^/api/frame/read/", body={})
        self.deploy_ok()
        self.run_cli("publish", "--url", URL, "--remove", "old.css")
        req = self.srv.find("POST", r"^/api/frame/deploy/direct")[0]["json"]
        self.assertEqual(req["mode"], "patch")
        self.assertEqual(req["baseVersion"], "v1-abc")
        self.assertEqual(req["manifest"], {"old.css": None})

    def test_staged_upload_only_missing(self):
        big = self.dir / "big.js"
        big.write_text("x" * (1024 * 1024 + 10))
        small = self.dir / "small.css"
        small.write_text("p{}")
        sha_big = hashlib.sha256(big.read_bytes()).hexdigest()
        self.srv.add("POST", r"^/api/frame/deploy/prepare", body={"slug": SLUG, "missing": [sha_big]})
        self.srv.add("POST", r"^/api/frame/upload", body={"files": [{"sha256": sha_big}]})
        self.deploy_ok()
        self.run_cli("publish", self.page(), "--title", "T", "--file", f"big.js={big}", "--file", f"s.css={small}")
        prep = self.srv.find("POST", r"^/api/frame/deploy/prepare")[0]["json"]
        self.assertEqual(len(prep["shas"]), 2)
        self.assertNotIn("slug", prep)
        ups = self.srv.find("POST", r"^/api/frame/upload")
        self.assertEqual(len(ups), 1)
        self.assertEqual([f["path"] for f in ups[0]["json"]["files"]], ["big.js"])
        dep = self.srv.find("POST", r"^/api/frame/deploy/direct")[0]["json"]
        self.assertEqual(dep["slug"], SLUG)
        self.assertEqual(dep["manifest"]["big.js"], {"sha256": sha_big, "contentType": "text/javascript"})
        self.assertIn("sha256", dep["manifest"]["s.css"])

    def test_copy_from_other_artifact(self):
        self.boot_route()
        self.srv.add("GET", r"^/api/frame/read/", body={})
        self.srv.add("POST", r"^/api/frame/deploy/prepare", body={"slug": SLUG, "missing": []})
        self.srv.add("POST", r"^/api/frame/upload", body={"files": [{"sha256": "a" * 64, "contentType": "image/png"}]})
        self.deploy_ok()
        src = f"https://claude.ai/code/artifact/{SLUG2}"
        self.run_cli("publish", "--url", URL, "--copy", f"logo.png={src}:img/logo.png@v7")
        up = self.srv.find("POST", r"^/api/frame/upload")[0]["json"]
        self.assertEqual(up["files"], [{"path": "logo.png", "from": {"slug": SLUG2, "path": "img/logo.png", "ver": "v7"}}])
        dep = self.srv.find("POST", r"^/api/frame/deploy/direct")[0]["json"]
        self.assertEqual(dep["manifest"]["logo.png"]["sha256"], "a" * 64)

    def test_bad_published_path_rejected_before_send(self):
        css = self.dir / "s.css"
        css.write_text("x")
        e = self.run_cli("publish", self.page(), "--file", f"../s.css={css}", expect=2)
        self.assertEqual(e["error"]["code"], "invalid_argument")
        self.assertEqual(self.srv.requests, [])

    def test_create_from_type(self):
        self.srv.add("POST", rf"^/api/frame/types/{SLUG2}/create", body={"slug": SLUG, "version": "v1"})
        out = self.run_cli("publish", "--type-url", f"https://claude.ai/code/artifact/{SLUG2}", "--title", "Deck")
        req = self.srv.find("POST", r"/create")[0]
        self.assertIn("idempotency_key=", req["path"])
        self.assertEqual(req["json"], {"target_type_version": None, "title": "Deck"})
        self.assertEqual(out["slug"], SLUG)


# ---------------------------------------------------------------------------- list / read
class ListAndRead(Base):
    def test_list_mine_follows_pages(self):
        row = lambda i: {"kind": "page", "id": f"{i:08x}-0000-4000-8000-000000000000", "title": f"t{i}",
                         "rel": "mine", "updated_at": "2026-11-07T00:00:00Z"}
        self.srv.add("GET", r"^/api/frame/artifacts\?(?!.*page_token)", body={"artifacts": [row(1), row(2)], "next_page_token": "P2"})
        self.srv.add("GET", r"^/api/frame/artifacts\?.*page_token=P2", body={"artifacts": [row(3), {"kind": "page", "id": "x"}]})
        out = self.run_cli("list", "--all")
        self.assertEqual([r["title"] for r in out["rows"]], ["t1", "t2", "t3"])
        self.assertNotIn("next_page_token", out)
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.srv.requests[0]["path"]).query)
        self.assertEqual(q["rel"], ["mine"])
        self.assertEqual(q["kind"], ["page,document"])

    def test_list_limit_returns_token(self):
        self.srv.add("GET", r"^/api/frame/artifacts", body={"artifacts": [{"kind": "page", "id": SLUG, "title": "a"}],
                                                            "next_page_token": "P2"})
        out = self.run_cli("list", "--limit", "1")
        self.assertEqual(out["next_page_token"], "P2")

    def test_list_all_combines_tokens(self):
        self.srv.add("GET", r"rel=mine", body={"artifacts": [{"kind": "page", "id": SLUG}], "next_page_token": "M"})
        self.srv.add("GET", r"rel=shared", body={"artifacts": [{"kind": "document", "id": SLUG2, "rel": "shared"}]})
        self.srv.add("GET", r"^/api/frame/frames/external", body={"frames": []})
        out = self.run_cli("list", "--scope", "all", "--limit", "2")
        self.assertEqual(out["next_page_token"], "M~")
        self.assertEqual({r["slug"] for r in out["rows"]}, {SLUG, SLUG2})

    def test_list_falls_back_to_legacy(self):
        self.srv.add("GET", r"^/api/frame/artifacts", status=404, body="not found")
        self.srv.add("GET", r"^/api/frame/frames\?", body={"frames": [{"slug": SLUG, "title": "a", "rel": "mine"},
                                                                    {"slug": SLUG2, "rel": "shared"}]})
        out = self.run_cli("list")
        self.assertTrue(out["legacy"])
        self.assertEqual([r["slug"] for r in out["rows"]], [SLUG])

    def test_read_page_uses_asset_token_and_redacts(self):
        self.boot_route(ver="v1-abc", files=[{"path": "a.css"}])
        self.srv.add("GET", rf"^/_content/{SLUG}/_f/v1-abc/_src/index.html", raw=b"<p>src</p>",
                     headers={"Content-Type": "text/html; charset=utf-8"})
        out = self.run_cli("read", URL)
        self.assertEqual(out["page"]["content"], "<p>src</p>")
        self.assertEqual(out["files"], [{"path": "a.css"}])
        self.assertNotIn("asset-placeholder", json.dumps(out))
        c = self.srv.find("GET", r"^/_content/")[0]
        self.assertIn("__frame_t=asset-placeholder", c["path"])
        self.assertNotIn("authorization", c["headers"])   # OAuth never sent to the content host
        boot_q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.srv.requests[0]["path"]).query)
        self.assertEqual(boot_q["via"], ["model_read"])

    def test_read_file_falls_back_to_served(self):
        self.boot_route(ver="v1")
        self.srv.add("GET", r"/_f/v1/_src/data.json", status=404, body="")
        self.srv.add("GET", r"/_f/v1/data.json", raw=b'{"a":1}', headers={"Content-Type": "application/json"})
        out = self.run_cli("read", URL, "--path", "data.json", "--out-dir", str(self.dir / "o"))
        self.assertEqual((self.dir / "o" / "data.json").read_text(), '{"a":1}')
        self.assertEqual(out["files_read"][0]["bytes"], 7)

    def test_read_past_version(self):
        self.boot_route(ver="v0-old")
        out = self.run_cli("read", URL, "--version", "v0-old", "--meta")
        self.assertEqual(out["ver"], "v0-old")
        self.assertIn("ver=v0-old", self.srv.requests[0]["path"])

    def test_list_files_scope(self):
        self.boot_route(files=[{"path": "x.js"}], fileCount=1)
        out = self.run_cli("list", "--scope", "files", "--url", URL)
        self.assertEqual(out["files"], [{"path": "x.js"}])


# ---------------------------------------------------------------------------- lifecycle
class Lifecycle(Base):
    def test_delete_needs_yes(self):
        e = self.run_cli("delete", URL, expect=2)
        self.assertEqual(e["error"]["code"], "confirmation_required")
        self.assertEqual(self.srv.requests, [])

    def test_delete(self):
        self.srv.add("DELETE", rf"^/api/frame/{SLUG}$", status=204, body="")
        self.assertTrue(self.run_cli("delete", URL, "--yes")["deleted"])

    def test_delete_already_gone(self):
        self.srv.add("DELETE", rf"^/api/frame/{SLUG}$", status=404, body="not found")
        self.assertTrue(self.run_cli("delete", URL, "--yes")["alreadyGone"])

    def test_unpin_uses_delete(self):
        self.srv.add("DELETE", r"^/api/frame/favorite/", status=204, body="")
        self.assertFalse(self.run_cli("unpin", URL)["pinned"])

    def test_retitle(self):
        self.srv.add("POST", r"^/api/frame/retitle/", body={"title": "New"})
        self.assertEqual(self.run_cli("retitle", URL, "New")["title"], "New")
        self.assertEqual(self.srv.requests[0]["json"], {"title": "New"})

    def test_versions(self):
        self.srv.add("GET", r"^/api/frame/versions/", body={"live": "v2", "history": ["v2", "v1"],
                                                            "versions": {"v1": {"created_at": "2026-11-01T00:00:00Z"}}})
        out = self.run_cli("versions", URL)
        self.assertEqual(out["versions"][0], {"id": "v2", "current": True})
        self.assertEqual(out["versions"][1]["createdAt"], "2026-11-01T00:00:00Z")


# ---------------------------------------------------------------------------- assets
class Assets(Base):
    def test_upload_raw_body(self):
        f = self.dir / "a.png"
        f.write_bytes(b"\x89PNGdata")
        self.srv.add("POST", r"/agent-upload$", body={"opaque_id": ASSET, "size_bytes": 8, "content_type": "image/png"})
        out = self.run_cli("assets", "upload", URL, str(f))
        req = self.srv.requests[0]
        self.assertEqual(req["path"], f"/api/frame/blob/{SLUG}/agent-upload")
        self.assertEqual(req["headers"]["content-type"], "image/png")
        self.assertEqual(req["body"], b"\x89PNGdata")
        self.assertEqual(out["uploaded"][0]["url"], f"/_blob/{ASSET}")

    def test_upload_rejects_unknown_type(self):
        f = self.dir / "a.exe"
        f.write_bytes(b"x")
        self.run_cli("assets", "upload", URL, str(f), expect=2)
        self.assertEqual(self.srv.requests, [])

    def test_list_paging(self):
        self.srv.add("POST", r"/agent-list$", body={"assets": [{"opaque_id": ASSET}], "usage": {"files": 2}, "next": "N1"}, times=1)
        self.srv.add("POST", r"/agent-list$", body={"assets": [{"opaque_id": "f" * 32}], "usage": {"files": 2}})
        out = self.run_cli("assets", "list", URL, "--all")
        self.assertEqual(len(out["assets"]), 2)
        self.assertEqual(self.srv.requests[1]["json"], {"limit": 50, "after": "N1"})

    def test_delete_and_copy(self):
        self.srv.add("POST", rf"/{ASSET}/agent-delete$", body={"deleted": True})
        self.assertTrue(self.run_cli("assets", "delete", URL, ASSET, "--yes")["deleted"])
        self.srv.add("POST", r"/agent-copy$", body={"assets": [{"from_id": ASSET, "opaque_id": "e" * 32}]})
        out = self.run_cli("assets", "copy", URL, ASSET, "--from", SLUG2)
        self.assertEqual(self.srv.find("POST", r"/agent-copy$")[0]["json"], {"from": SLUG2, "ids": [ASSET]})
        self.assertEqual(out["copied"][0]["id"], "e" * 32)

    def test_capability_disabled_message(self):
        f = self.dir / "a.png"
        f.write_bytes(b"x")
        self.srv.add("POST", r"/agent-upload$", status=403, body={"error": {"code": "capability_disabled"}})
        e = self.run_cli("assets", "upload", URL, str(f), expect=1)
        self.assertEqual(e["error"]["code"], "capability_disabled")
        self.assertIn("assets", e["error"]["message"])


# ---------------------------------------------------------------------------- database
class Database(Base):
    def db_reqs(self):
        return [r["json"] for r in self.srv.find("POST", r"^/api/frame/db/agent$")]

    def test_get(self):
        self.srv.add("POST", r"^/api/frame/db/agent$", body={"exists": True, "data": {"h": 0.25}, "version": 3})
        out = self.run_cli("db", "get", URL, "logs", "d1", "--as-level", "view")
        self.assertEqual(self.db_reqs()[0], {"slug": SLUG, "verb": "get", "args": {"path": "logs/d1"}, "asLevel": "view"})
        self.assertEqual(out["doc"], {"id": "d1", "data": {"h": 0.25}, "version": 3})

    def test_get_missing(self):
        self.srv.add("POST", r"^/api/frame/db/agent$", body={"exists": False})
        self.assertFalse(self.run_cli("db", "get", URL, "logs", "nope")["found"])

    def test_query_body_and_cursor_paging(self):
        self.srv.add("POST", r"^/api/frame/db/agent$", body={"docs": [{"id": "a", "data": {}, "version": 1}], "nextCursor": "C1"}, times=1)
        self.srv.add("POST", r"^/api/frame/db/agent$", body={"docs": [{"id": "b", "data": {}, "version": 1}]})
        out = self.run_cli("db", "query", URL, "logs", "--where", "date", "==", "2026-11-07",
                           "--where", "hours", ">", "0.1", "--where", "tag", "in", '["C0.1"]', "--limit", "1", "--all")
        reqs = self.db_reqs()
        self.assertEqual(reqs[0]["args"]["where"], [
            {"field": "date", "op": "eq", "value": "2026-11-07"},
            {"field": "hours", "op": "gt", "value": 0.1},
            {"field": "tag", "op": "in", "value": ["C0.1"]}])
        self.assertEqual(reqs[0]["args"]["limit"], 1)
        self.assertNotIn("cursor", reqs[0]["args"])
        self.assertEqual(reqs[1]["args"]["cursor"], "C1")
        self.assertEqual([d["id"] for d in out["docs"]], ["a", "b"])
        self.assertNotIn("next_cursor", out)

    def test_query_order_by(self):
        self.srv.add("POST", r"^/api/frame/db/agent$", body={"docs": []})
        self.run_cli("db", "query", URL, "logs", "--order-by", "date:desc", "--limit", "5")
        self.assertEqual(self.db_reqs()[0]["args"]["orderBy"], {"field": "date", "direction": "desc"})

    def test_bad_operator_and_parity_rejected_offline(self):
        self.run_cli("db", "query", URL, "logs", "--where", "a", "~=", "1", expect=2)
        e = self.run_cli("db", "list", URL, "logs/doc1", expect=2)
        self.assertEqual(e["error"]["code"], "parity")
        self.assertEqual(self.srv.requests, [])

    def test_set_update_delete_with_if_version(self):
        self.srv.add("POST", r"^/api/frame/db/agent$", body={"version": 4, "usage": {"documents": 1, "maxDocuments": 1000}})
        self.run_cli("db", "set", URL, "t", "d", "--data", '{"a":1}', "--if-version", "3")
        self.run_cli("db", "update", URL, "t", "d", "--data", '{"a":{"__delete__":true}}')
        self.run_cli("db", "delete", URL, "t", "d", "--if-version", "4", "--require-version")
        r = self.db_reqs()
        self.assertEqual(r[0]["args"], {"path": "t/d", "data": {"a": 1}, "replace": True, "ifVersion": 3})
        self.assertEqual(r[1]["args"], {"path": "t/d", "data": {"a": {"__delete__": True}}})
        self.assertEqual(r[2]["args"], {"path": "t/d", "ifVersion": 4})
        self.assertTrue(r[2]["requireVersion"])
        self.assertNotIn("requireVersion", r[0])

    def test_delete_sentinel_rejected_in_set_and_arrays(self):
        self.run_cli("db", "set", URL, "t", "d", "--data", '{"a":{"__delete__":true}}', expect=2)
        self.run_cli("db", "update", URL, "t", "d", "--data", '{"a":[{"__delete__":true}]}', expect=2)
        self.assertEqual(self.srv.requests, [])

    def test_version_mismatch_exit_4(self):
        self.srv.add("POST", r"^/api/frame/db/agent$", status=409,
                     body={"error": {"code": "version_mismatch", "version": 7}})
        e = self.run_cli("db", "update", URL, "t", "d", "--data", '{"a":2}', "--if-version", "5", expect=4)
        self.assertEqual(e["error"]["code"], "version_mismatch")
        self.assertEqual(e["error"]["version"], 7)
        self.assertIn("version 7", e["error"]["message"])

    def test_version_required(self):
        self.srv.add("POST", r"^/api/frame/db/agent$", status=409,
                     body={"error": {"code": "version_mismatch", "required": True}})
        e = self.run_cli("db", "set", URL, "t", "d", "--data", "{}", "--require-version", expect=4)
        self.assertIn("--if-version", e["error"]["message"])

    def test_str_replace(self):
        self.srv.add("POST", r"^/api/frame/db/agent$", body={"version": 2})
        self.run_cli("db", "str-replace", URL, "t", "d", "--field", "body", "--old", "a", "--new", "b",
                     "--replace-all", "--if-version", "1")
        a = self.db_reqs()[0]
        self.assertEqual(a["verb"], "str_replace")
        self.assertEqual({k: v for k, v in a["args"].items() if k != "idem"},
                         {"path": "t/d", "field": "body", "old_str": "a", "new_str": "b", "replace_all": True, "ifVersion": 1})
        self.assertRegex(a["args"]["idem"], r"^[A-Za-z0-9_-]{22}$")

    def test_batch(self):
        self.srv.add("POST", r"^/api/frame/db/agent$", body={"results": [{"version": 1}, {}]})
        writes = [{"op": "set", "collection": "t", "doc_id": "a", "data": {"x": 1}},
                  {"op": "delete", "collection": "t", "doc_id": "b", "if_version": 2}]
        out = self.run_cli("db", "batch", URL, "--writes", json.dumps(writes))
        self.assertEqual(self.db_reqs()[0]["args"], {"ops": [
            {"op": "set", "path": "t/a", "data": {"x": 1}, "replace": True},
            {"op": "delete", "path": "t/b", "ifVersion": 2}]})
        self.assertEqual(out["results"], [{"version": 1}, {}])

    def test_batch_limits(self):
        too_many = [{"op": "delete", "collection": "t", "doc_id": f"d{i}"} for i in range(51)]
        self.run_cli("db", "batch", URL, "--writes", json.dumps(too_many), expect=2)
        dup = [{"op": "delete", "collection": "t", "doc_id": "d"}] * 2
        self.run_cli("db", "batch", URL, "--writes", json.dumps(dup), expect=2)
        self.assertEqual(self.srv.requests, [])

    def test_me_resolution(self):
        self.srv.add("POST", r"^/api/frame/db/agent$", body={"id": "u_abc"}, times=1)
        self.srv.add("POST", r"^/api/frame/db/agent$", body={"docs": []})
        self.run_cli("db", "list", URL, "data/users/me/decks/cards")
        r = self.db_reqs()
        self.assertEqual(r[0]["verb"], "whoami")
        self.assertEqual(r[1]["args"]["collection"], "data/users/u_abc/decks/cards")

    def test_credential_rejected(self):
        self.srv.add("POST", r"^/api/frame/db/agent$", status=403,
                     body="db verbs require a user-scoped OAuth token, not an org service key")
        e = self.run_cli("db", "get", URL, "t", "d", expect=1)
        self.assertEqual(e["error"]["code"], "credential_rejected")

    def test_profiles(self):
        self.srv.add("POST", r"^/api/frame/user/profiles/", body={"profiles": {"u_1": {"name": "Ana"}, "u_x": {}}})
        out = self.run_cli("profiles", URL, "u_1", "u_2")
        self.assertEqual(self.srv.requests[0]["json"], {"ids": ["u_1", "u_2"]})
        self.assertEqual(out["profiles"], {"u_1": {"name": "Ana"}, "u_2": {}})


# ---------------------------------------------------------------------------- comments / types
class CommentsAndTypes(Base):
    def test_read(self):
        self.srv.add("GET", r"^/api/frame/comments/", body={"threads": [{"id": THREAD, "comments": []},
                                                                      {"id": SLUG2, "resolved_at": "x", "comments": []}]})
        out = self.run_cli("comments", "read", URL, "--names", "--unresolved")
        self.assertEqual(out["count"], 1)
        self.assertIn("names=1", self.srv.requests[0]["path"])

    def test_reply(self):
        self.srv.add("POST", rf"^/api/frame/comments/{SLUG}/{THREAD}$", status=201, body={"thread_id": THREAD, "comment_id": "c1"})
        out = self.run_cli("comments", "reply", URL, THREAD, "Done.", "--id", "my-id")
        self.assertEqual(self.srv.requests[0]["json"], {"text": "Done.", "id": "my-id"})
        self.assertEqual(out["comment_id"], "c1")

    def test_reply_not_activated(self):
        self.srv.add("POST", r"^/api/frame/comments/", status=403, body="not activated on this thread")
        e = self.run_cli("comments", "reply", URL, THREAD, "hi", expect=1)
        self.assertEqual(e["error"]["code"], "not_activated")

    def test_resolve_falls_back(self):
        self.srv.add("POST", r"/session-resolve$", status=404, body="not found")
        self.srv.add("POST", r"/resolve$", body={})
        self.assertTrue(self.run_cli("comments", "resolve", URL, THREAD)["resolved"])
        self.assertEqual(self.srv.requests[1]["json"], {"resolved": True})

    def test_types_and_contract(self):
        self.srv.add("GET", r"^/api/frame/types\?", body={"types": [{"slug": SLUG2, "title": "Slides"}]})
        out = self.run_cli("types", "list", "--query", "deck", "--anthropic")
        self.assertEqual(out["types"][0]["title"], "Slides")
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.srv.requests[0]["path"]).query)
        self.assertEqual(q, {"scopes": ["anthropic"], "q": ["deck"]})
        self.srv.add("GET", r"^/api/frame/contract/latest", body={"version": "0.1.12", "capabilities": ["db"]})
        self.srv.add("GET", r"^/api/frame/contract/0.1.12/db.d.ts", body="declare const db: unknown;")
        out = self.run_cli("contract", "--dts", "db")
        self.assertEqual(out["text"], "declare const db: unknown;")


if __name__ == "__main__":
    unittest.main()
