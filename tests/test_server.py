"""End-to-end tests for the tailclip server. Run: python -m unittest discover tests"""
import json
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import tailclip  # noqa: E402

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tailclip.Config(max_items=3, max_item_bytes=1024 * 1024, ttl_seconds=3600)
        self.server = tailclip.make_server(self.cfg, "127.0.0.1", 0)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def req(self, method, path, body=None, headers=None):
        r = urllib.request.Request(self.base + path, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def push(self, body, ctype="text/plain; charset=utf-8", **headers):
        return self.req("POST", "/api/clips", body, {"Content-Type": ctype, **headers})

    def test_text_roundtrip(self):
        status, _, body = self.push("héllo 世界".encode())
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["kind"], "text")
        status, headers, body = self.req("GET", "/api/latest")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Tailclip-Kind"], "text")
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        self.assertEqual(body.decode(), "héllo 世界")

    def test_image_is_sniffed_even_as_octet_stream(self):
        status, _, body = self.push(PNG, "application/octet-stream")
        self.assertEqual(status, 201)
        meta = json.loads(body)
        self.assertEqual((meta["kind"], meta["mime"]), ("image", "image/png"))
        _, headers, raw = self.req("GET", "/api/latest")
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(raw, PNG)

    def test_untyped_utf8_body_becomes_text(self):
        _, _, body = self.push(b"from shortcuts", "application/octet-stream")
        self.assertEqual(json.loads(body)["kind"], "text")

    def test_file_with_name_downloads_as_attachment(self):
        _, _, body = self.push(b"%PDF-1.7 ...", "application/pdf", **{"X-Filename": "notes%20v2.pdf"})
        meta = json.loads(body)
        self.assertEqual((meta["kind"], meta["name"]), ("file", "notes v2.pdf"))
        _, headers, _ = self.req("GET", "/api/latest")
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(headers["Content-Security-Policy"], "sandbox")

    def test_unnamed_uploads_get_a_name_with_extension(self):
        _, _, body = self.push(b"%PDF-1.7 ...", "application/pdf")
        meta = json.loads(body)
        self.assertEqual(meta["kind"], "file")
        self.assertRegex(meta["name"], r"^file-\d{8}-\d{6}\.pdf$")
        _, _, body = self.push(PNG, "image/png")
        self.assertRegex(json.loads(body)["name"], r"^photo-\d{8}-\d{6}\.png$")
        _, _, body = self.push(b"just text")
        self.assertIsNone(json.loads(body)["name"])

    def test_html_upload_is_never_served_as_html(self):
        self.push(b"<script>alert(1)</script>", "text/html", **{"X-Filename": "x.html"})
        _, headers, _ = self.req("GET", "/api/latest")
        self.assertNotIn("text/html", headers["Content-Type"])
        self.assertIn("attachment", headers["Content-Disposition"])

    def test_limits_keep_newest_items(self):
        for i in range(5):
            self.push(f"clip {i}".encode())
        _, _, body = self.req("GET", "/api/clips")
        previews = [c["preview"] for c in json.loads(body)["clips"]]
        self.assertEqual(previews, ["clip 4", "clip 3", "clip 2"])

    def test_too_large_and_empty_are_rejected(self):
        status, _, _ = self.push(b"x" * (self.cfg.max_item_bytes + 1))
        self.assertEqual(status, 413)
        status, _, _ = self.push(b"   ")
        self.assertEqual(status, 400)

    def test_expired_clips_disappear(self):
        self.push(b"old")
        self.server.store._clips[0].created = time.time() - 7200
        status, _, _ = self.req("GET", "/api/latest")
        self.assertEqual(status, 404)

    def test_cross_site_writes_are_blocked(self):
        status, _, _ = self.push(b"evil", **{"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        status, _, _ = self.push(b"evil", **{"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        status, _, _ = self.req("DELETE", "/api/clips", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        status, _, _ = self.push(b"fine", **{"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 201)

    def test_delete_and_clear(self):
        _, _, body = self.push(b"one")
        clip_id = json.loads(body)["id"]
        self.push(b"two")
        self.assertEqual(self.req("DELETE", f"/api/clips/{clip_id}")[0], 200)
        self.assertEqual(self.req("GET", f"/api/clips/{clip_id}")[0], 404)
        self.assertEqual(self.req("DELETE", "/api/clips")[0], 200)
        self.assertEqual(json.loads(self.req("GET", "/api/clips")[2])["clips"], [])

    def test_allowed_logins(self):
        self.server.cfg = tailclip.Config(allowed_logins=frozenset({"me@github"}))
        self.assertEqual(self.req("GET", "/api/clips")[0], 403)
        self.assertEqual(self.req("GET", "/api/clips", headers={"Tailscale-User-Login": "Me@GitHub"})[0], 200)

    def test_source_label(self):
        _, _, body = self.push(b"a", **{"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"})
        self.assertEqual(json.loads(body)["source"], "iPhone")
        _, _, body = self.push(b"b", **{"X-Source": "Work laptop"})
        self.assertEqual(json.loads(body)["source"], "Work laptop")

    def test_index_page(self):
        status, headers, body = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn(b"tailclip", body)


if __name__ == "__main__":
    unittest.main()
