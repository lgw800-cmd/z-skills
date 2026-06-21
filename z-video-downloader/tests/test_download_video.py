from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "download_video.py"
SPEC = importlib.util.spec_from_file_location("download_video", SCRIPT)
download_video = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(download_video)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002, ANN001
        pass


class InvidiousLikeHandler(BaseHTTPRequestHandler):
    payload = b"\x00\x00\x00\x18ftypmp42" + b"video-data" * 4096
    video_id = "v1wZwxY3CMg"

    def log_message(self, format, *args):  # noqa: A002, ANN001
        pass

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == f"/api/v1/videos/{self.video_id}":
            body = json.dumps({"title": "Sample Invidious Video"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/latest_version":
            self.send_response(302)
            self.send_header("Location", f"/companion/latest_version?{parsed.query}")
            self.end_headers()
            return

        if parsed.path == "/companion/latest_version":
            query = parse_qs(parsed.query)
            video_id = (query.get("id") or [""])[0]
            self.send_response(302)
            self.send_header(
                "Location",
                f"/companion/videoplayback?id={video_id}&itag=18&clen={len(self.payload)}&host=unit.test",
            )
            self.end_headers()
            return

        if parsed.path == "/companion/videoplayback":
            match = re.fullmatch(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
            if not match:
                self.send_response(416)
                self.end_headers()
                return
            start, end = (int(match.group(1)), int(match.group(2)))
            end = min(end, len(self.payload) - 1)
            body = self.payload[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(self.payload)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.end_headers()


class DownloadVideoTests(unittest.TestCase):
    def test_classify_common_urls(self):
        cases = {
            "https://example.com/video.mp4": "direct",
            "https://cdn.example.com/live/index.m3u8": "stream",
            "https://www.youtube.com/watch?v=BaW_jenozKc": "platform",
            "https://www.bilibili.com/video/BV1xx411c7mD": "platform",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(download_video.classify_url(url), expected)

    def test_platform_names(self):
        self.assertEqual(download_video.platform_name("https://youtu.be/BaW_jenozKc"), "YouTube")
        self.assertEqual(download_video.platform_name("https://b23.tv/abc123"), "Bilibili")
        self.assertEqual(download_video.platform_name("https://www.douyin.com/video/123"), "Douyin")

    def test_youtube_video_id(self):
        self.assertEqual(download_video.youtube_video_id("https://youtu.be/v1wZwxY3CMg"), "v1wZwxY3CMg")
        self.assertEqual(download_video.youtube_video_id("https://www.youtube.com/watch?v=v1wZwxY3CMg"), "v1wZwxY3CMg")
        self.assertEqual(download_video.youtube_video_id("https://www.youtube.com/shorts/v1wZwxY3CMg"), "v1wZwxY3CMg")
        self.assertEqual(download_video.youtube_video_id("https://example.com/watch?v=v1wZwxY3CMg"), "")

    def test_build_ytdlp_cmd_defaults_to_single_video_1080(self):
        cmd = download_video.build_ytdlp_cmd(
            "https://www.youtube.com/watch?v=BaW_jenozKc",
            Path("/tmp/out"),
            ytdlp=Path("/opt/ytdlp"),
            quality="1080",
            max_video_mb=500,
            browser_cookies="chrome",
            playlist=False,
        )
        self.assertIn("--cookies-from-browser", cmd)
        self.assertIn("chrome", cmd)
        self.assertIn("--no-playlist", cmd)
        self.assertIn("bv*[height<=1080]+ba/b[height<=1080]/b", cmd)
        self.assertIn("--merge-output-format", cmd)
        self.assertIn("mp4", cmd)

    def test_direct_download_from_local_http_server(self):
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as out_dir:
            source = Path(source_dir)
            payload = b"fake mp4 bytes"
            (source / "sample.mp4").write_bytes(payload)
            server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *args, **kwargs: QuietHandler(*args, directory=source_dir, **kwargs))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_address[1]}/sample.mp4"
                record = download_video.download_direct_video(
                    download_video.requests.Session(),
                    url,
                    Path(out_dir),
                    max_video_mb=1,
                )
                self.assertEqual(record["status"], "ok")
                self.assertEqual(record["bytes"], len(payload))
                self.assertEqual(Path(record["files"][0]).read_bytes(), payload)
            finally:
                server.shutdown()
                server.server_close()

    def test_invidious_fallback_downloads_range_proxy(self):
        with tempfile.TemporaryDirectory() as out_dir:
            server = ThreadingHTTPServer(("127.0.0.1", 0), InvidiousLikeHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                instance = f"http://127.0.0.1:{server.server_address[1]}"
                record = download_video.download_youtube_invidious_fallback(
                    download_video.requests.Session(),
                    f"https://youtu.be/{InvidiousLikeHandler.video_id}",
                    Path(out_dir),
                    max_video_mb=1,
                    timeout=5,
                    instances=[instance],
                    chunk_size=4096,
                )
                self.assertEqual(record["status"], "ok")
                self.assertEqual(record["platform"], "YouTube / Invidious proxy")
                self.assertEqual(Path(record["files"][0]).read_bytes(), InvidiousLikeHandler.payload)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
