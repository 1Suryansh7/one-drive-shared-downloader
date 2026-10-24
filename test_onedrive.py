"""Offline regressions: python -m unittest -v."""
import asyncio
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from onedrive import (GUEST_TOKEN_URL, GRAPH_API, LEGACY_API, PERSONAL_API,
                      OneDriveError, OneDriveSharedFolder, _safe_name, url_to_id)

REAL_ASYNC_CLIENT = httpx.AsyncClient
SHARE = "https://1drv.ms/f/s!public-example"


def file_item(name="hello.txt", payload=b"hello", **extra):
    return {"id": name, "name": name, "file": {}, "size": len(payload),
            "@content.downloadUrl": "https://files.example/" + name, **extra}


class InterruptedStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"partial"
        raise httpx.ReadError("download interrupted")


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "destination"
        self.stdout = io.StringIO()

    def run_download(self, handler, downloader=None, **kwargs):
        def client_factory(**options):
            return REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **options)
        downloader = downloader or OneDriveSharedFolder(SHARE)
        with patch("onedrive.httpx.AsyncClient", side_effect=client_factory), redirect_stdout(self.stdout):
            downloader.download(self.output, **kwargs)
        return downloader

    def test_single_file_destination_and_client_reuse(self):
        def handler(request):
            if request.url.host == "files.example":
                self.assertNotIn("Authorization", request.headers)
                return httpx.Response(200, content=b"hello")
            return httpx.Response(200, json=file_item())
        downloader = self.run_download(handler)
        self.assertEqual((self.output / "hello.txt").read_bytes(), b"hello")
        self.run_download(handler, downloader)
        self.assertEqual(downloader._downloaded_files, 1)
        self.assertIsNone(downloader._client)

    def test_nested_folders_empty_folders_and_pagination(self):
        root = {"id": "root", "name": "Share", "folder": {},
                "parentReference": {"driveId": "drive"}}
        def handler(request):
            path = request.url.path
            if request.url.host == "files.example":
                content = b"" if path == "/zero.bin" else b"hello"
                return httpx.Response(200, content=content)
            if path.endswith("/root"):
                return httpx.Response(200, json=root)
            if path.endswith("/items/root/children"):
                return httpx.Response(200, json={"value": [
                    file_item(),
                    {"id": "sub", "name": "Sub", "folder": {"childCount": 1}},
                    {"id": "empty", "name": "Empty", "folder": {"childCount": 0}},
                ], "@odata.nextLink": LEGACY_API + "/page2"})
            if path.endswith("/items/sub/children"):
                return httpx.Response(200, json={"value": [file_item("nested.txt")]})
            if path.endswith("/page2"):
                return httpx.Response(200, json={"value": [file_item("zero.bin", b"")]})
            self.fail("Unexpected API route: " + path)
        self.run_download(handler)
        self.assertEqual((self.output / "Share/Sub/nested.txt").read_bytes(), b"hello")
        self.assertTrue((self.output / "Share/Empty").is_dir())
        self.assertEqual((self.output / "Share/zero.bin").stat().st_size, 0)
        self.assertEqual(len(list(self.output.rglob("*.txt"))), 2)

    def test_migrated_guest_flow_and_no_token_on_download(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.host == "api.onedrive.com":
                return httpx.Response(401, json={"error": {"code": "unauthenticated"}})
            if str(request.url) == GUEST_TOKEN_URL:
                self.assertEqual(request.method, "POST")
                return httpx.Response(200, json={"token": "guest-session"})
            if request.url.host == "my.microsoftpersonalcontent.com":
                self.assertEqual(request.headers["Authorization"], "Badger guest-session")
                self.assertEqual(request.headers["Prefer"], "autoredeem")
                item = file_item()
                # The NoAuth variant lacks tempauth and returns 401 without
                # browser cookies, even when the ordinary signed URL works.
                item["@content.downloadUrlNoAuth"] = "https://broken.example/no-session"
                return httpx.Response(200, json=item)
            self.assertEqual(request.url.host, "files.example")
            self.assertNotIn("Authorization", request.headers)
            return httpx.Response(200, content=b"hello")
        self.run_download(handler)
        self.assertEqual(len(requests), 4)
        self.assertEqual((self.output / "hello.txt").read_bytes(), b"hello")
        self.assertNotIn("guest-session", self.stdout.getvalue())

    def test_graph_download_url_and_bearer_token(self):
        def handler(request):
            if request.url.host == "graph.microsoft.com":
                self.assertEqual(request.headers["Authorization"], "Bearer graph-token")
                item = file_item()
                item["@microsoft.graph.downloadUrl"] = item.pop("@content.downloadUrl")
                return httpx.Response(200, json=item)
            self.assertEqual(request.url.host, "files.example")
            self.assertNotIn("Authorization", request.headers)
            return httpx.Response(200, content=b"hello")
        self.run_download(handler, OneDriveSharedFolder(SHARE, access_token="graph-token"))
        self.assertEqual((self.output / "hello.txt").read_bytes(), b"hello")

    def test_content_redirect_strips_api_authorization(self):
        def handler(request):
            if request.url.host == "graph.microsoft.com":
                self.assertEqual(request.headers["Authorization"], "Bearer graph-token")
                if request.url.path.endswith("/content"):
                    return httpx.Response(302, headers={"Location": "https://files.example/raw"})
                item = file_item()
                del item["@content.downloadUrl"]
                return httpx.Response(200, json=item)
            self.assertNotIn("Authorization", request.headers)
            return httpx.Response(200, content=b"hello")
        self.run_download(handler, OneDriveSharedFolder(SHARE, access_token="graph-token"))
        self.assertEqual((self.output / "hello.txt").read_bytes(), b"hello")

    def test_include_and_preview_write_nothing(self):
        def handler(request):
            return httpx.Response(200, json={
                "id": "root", "name": "Share", "folder": {},
                "children": [file_item(), file_item("other.bin")]})
        self.run_download(handler, include=["*.txt"], dry_run=True)
        self.assertIn("Share/hello.txt", self.stdout.getvalue())
        self.assertNotIn("other.bin", self.stdout.getvalue())
        self.assertFalse(self.output.exists())

    def test_permission_failure_preserves_existing_file(self):
        self.output.mkdir()
        existing = self.output / "hello.txt"
        existing.write_bytes(b"original")
        def handler(request):
            if request.url.host == "files.example":
                return httpx.Response(403)
            return httpx.Response(200, json=file_item())
        with self.assertRaisesRegex(OneDriveError, "HTTP 403"):
            self.run_download(handler)
        self.assertEqual(existing.read_bytes(), b"original")
        self.assertEqual(list(self.output.glob("*.part")), [])

    def test_truncated_download_is_not_published(self):
        def handler(request):
            if request.url.host == "files.example":
                return httpx.Response(200, content=b"bad")
            return httpx.Response(200, json=file_item())
        with self.assertRaisesRegex(OneDriveError, "Incomplete download"):
            self.run_download(handler)
        self.assertFalse((self.output / "hello.txt").exists())
        self.assertEqual(list(self.output.glob("*.part")), [])

    def test_interrupted_stream_cleans_up_temporary_file(self):
        def handler(request):
            if request.url.host == "files.example":
                return httpx.Response(200, stream=InterruptedStream())
            return httpx.Response(200, json=file_item())
        with self.assertRaises(httpx.ReadError):
            self.run_download(handler)
        self.assertFalse((self.output / "hello.txt").exists())
        self.assertEqual(list(self.output.glob("*.part")), [])

    def test_untrusted_pagination_host_rejected_before_request(self):
        requested_hosts = []
        def handler(request):
            requested_hosts.append(request.url.host)
            return httpx.Response(200, json={"id": "root", "name": "Share",
                "folder": {}, "children": [], "children@odata.nextLink": "https://evil.example/steal"})
        with self.assertRaisesRegex(OneDriveError, "unexpected API"):
            self.run_download(handler, OneDriveSharedFolder(SHARE, access_token="secret"))
        self.assertEqual(requested_hosts, ["graph.microsoft.com"])
        self.assertFalse(self.output.exists())

    def test_bad_names_and_destination_escape_rejected(self):
        for name in ["..", "../evil", "/evil", "a\\b", "C:evil", "CON", "NUL.txt",
                     "trailing.", "trailing ", "a\x00b", "", None]:
            with self.subTest(name=name), self.assertRaises(OneDriveError):
                _safe_name(name)
        self.assertEqual(_safe_name("Wallpaper"), "Wallpaper")
        self.assertEqual(_safe_name("café.txt"), "café.txt")
        downloader = OneDriveSharedFolder(SHARE)
        downloader._output = self.output.resolve()
        with self.assertRaises(OneDriveError):
            downloader._destination(Path("../escape"))

    def test_invalid_api_response_does_not_create_output(self):
        with self.assertRaisesRegex(OneDriveError, "invalid API response"):
            self.run_download(lambda request: httpx.Response(200, text="<html>Sign in</html>"))
        self.assertFalse(self.output.exists())

    def test_concurrency_is_bounded(self):
        active, maximum = 0, 0
        async def handler(request):
            nonlocal active, maximum
            if request.url.host == "files.example":
                active += 1
                maximum = max(active, maximum)
                await asyncio.sleep(0.01)
                active -= 1
                return httpx.Response(200, content=b"hello")
            return httpx.Response(200, json={"id": "root", "name": "Share", "folder": {},
                "children": [file_item(f"{number}.txt") for number in range(5)]})
        self.run_download(handler, concurrent_reqs=2)
        self.assertEqual(maximum, 2)
        self.assertEqual(len(list(self.output.rglob("*.txt"))), 5)

    def test_invalid_concurrency_fails_before_network(self):
        for value in [0, -1, True, 1.5]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.run_download(lambda request: self.fail("Network should not be used"),
                                  concurrent_reqs=value)

    def test_running_event_loop_has_actionable_message(self):
        async def check():
            with self.assertRaisesRegex(RuntimeError, "download_async"):
                OneDriveSharedFolder(SHARE).download()
        asyncio.run(check())

    def test_viewer_redeem_token_and_url_encoding(self):
        downloader = OneDriveSharedFolder("https://onedrive.live.com/?redeem=encoded-share&id=item")
        self.assertEqual(downloader._share_id, "u!encoded-share")
        self.assertNotRegex(url_to_id(SHARE), r"[+/=]")
        for url in ["http://1drv.ms/f/x", "https://evil.example/file", "https://1drv.ms.evil.example/file"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                OneDriveSharedFolder(url)


if __name__ == "__main__":
    unittest.main()
