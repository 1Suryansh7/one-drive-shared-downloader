"""Download shared OneDrive files and folders (Python 3.10+)."""
from __future__ import annotations

import argparse
import asyncio
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from itertools import islice
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import parse_qs, quote, urlsplit

import httpx

LEGACY_API = "https://api.onedrive.com/v1.0"
PERSONAL_API = "https://my.microsoftpersonalcontent.com/_api/v2.0"
GRAPH_API = "https://graph.microsoft.com/v1.0"
GUEST_TOKEN_URL = "https://api-badgerp.svc.ms/v1.0/token"
GUEST_APP_ID = "5cbed6ac-a083-4e14-b191-b4ba07653de2"


class OneDriveError(RuntimeError):
    """An actionable error without sharing URLs or tokens in its message."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def url_to_id(url: str) -> str:
    return urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def chunks(items, size: int):
    if size < 1:
        raise ValueError("concurrent_reqs must be a positive integer")
    iterator = iter(items)
    return iter(lambda: tuple(islice(iterator, size)), ())


def _safe_name(name: str) -> str:
    if (
        not isinstance(name, str) or not name or name in {".", ".."}
        or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
        or name.endswith((".", " "))
        or re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I)
    ):
        raise OneDriveError("OneDrive returned a name that cannot be saved safely")
    return name


@dataclass
class _RemoteFile:
    path: Path
    size: int | None
    api_url: str = field(repr=False)
    download_url: str | None = field(default=None, repr=False)


class OneDriveSharedFolder:
    def __init__(self, root_url: str, *, access_token: str | None = None):
        root_url = root_url.strip()
        parsed = urlsplit(root_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (
            host in {"1drv.ms", "onedrive.live.com", "photos.onedrive.com"}
            or host.endswith(".sharepoint.com")
        ) or parsed.username or parsed.password:
            raise ValueError("Provide an HTTPS OneDrive or SharePoint sharing URL")
        self._root_url = root_url
        self._access_token = access_token
        encoded = parse_qs(parsed.query).get("redeem", [""])[0] or url_to_id(root_url)
        self._share_id = "u!" + encoded
        self._files: dict[str, _RemoteFile] = {}
        self._directories: set[Path] = set()
        self._downloaded_files = 0
        self._client: httpx.AsyncClient | None = None
        self._api, self._headers = LEGACY_API, {}

    @staticmethod
    def _check_status(response: httpx.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        if status in {401, 403}:
            message = ("OneDrive denied access. The link may require sign-in or "
                       "have downloads disabled. For protected links, provide "
                       "an authorized Microsoft Graph access token.")
        elif status == 404:
            message = "OneDrive could not find the shared item; check the link and its expiry."
        else:
            message = "OneDrive could not complete the request."
        raise OneDriveError(f"{message} (HTTP {status})", status)

    def _api_headers(self, url: str) -> dict[str, str]:
        # Never send the guest session or bearer token to a pagination host.
        parsed, expected = urlsplit(url), urlsplit(self._api)
        if (parsed.scheme, parsed.netloc) != (expected.scheme, expected.netloc):
            raise OneDriveError("OneDrive returned an unexpected API or pagination host")
        return self._headers

    async def _json(self, url: str) -> dict:
        response = await self._client.get(url, headers=self._api_headers(url))
        self._check_status(response)
        try:
            data = response.json()
        except ValueError:
            raise OneDriveError("OneDrive returned an invalid API response") from None
        if not isinstance(data, dict) or "error" in data:
            raise OneDriveError("OneDrive returned an invalid API response")
        return data

    async def _resolve_root(self) -> tuple[dict, str]:
        if self._access_token:
            self._api = GRAPH_API
            self._headers = {"Authorization": f"Bearer {self._access_token}",
                             "Prefer": "redeemSharingLinkIfNecessary"}
            url = f"{self._api}/shares/{self._share_id}/driveItem"
            return await self._json(url), url

        self._api, self._headers = LEGACY_API, {}
        url = f"{self._api}/shares/{self._share_id}/root"
        try:
            return await self._json(url), url
        except OneDriveError as error:
            if error.status_code not in {400, 401, 403, 404}:
                raise

        # Migrated personal drives use OneDrive's anonymous guest session.
        # The server only grants permissions already allowed by the share.
        response = await self._client.post(GUEST_TOKEN_URL, json={"appId": GUEST_APP_ID})
        self._check_status(response)
        try:
            token = response.json()["token"]
        except (ValueError, KeyError, TypeError):
            raise OneDriveError("OneDrive did not issue an anonymous guest session") from None
        if not isinstance(token, str) or not token:
            raise OneDriveError("OneDrive did not issue an anonymous guest session")
        self._api = PERSONAL_API
        self._headers = {"Authorization": f"Badger {token}", "Prefer": "autoredeem"}
        url = f"{self._api}/shares/{self._share_id}/driveItem"
        return await self._json(url), url

    def _item_url(self, item: dict, drive_id: str | None, fallback: str) -> str:
        item_id = item.get("id")
        if item_id and drive_id:
            return f"{self._api}/drives/{quote(drive_id, safe='')}/items/{quote(item_id, safe='')}"
        return fallback

    async def _walk(self, item: dict, parent: Path, item_url: str,
                    drive_id: str | None = None) -> None:
        path = parent / _safe_name(item.get("name"))
        drive_id = item.get("parentReference", {}).get("driveId") or drive_id
        item_url = self._item_url(item, drive_id, item_url)
        if "file" in item:
            url = (item.get("@microsoft.graph.downloadUrl")
                   or item.get("@content.downloadUrl"))
            self._files[path.as_posix()] = _RemoteFile(path, item.get("size"), item_url, url)
            return
        if "folder" not in item:
            raise OneDriveError(f"Unsupported OneDrive item: {path.as_posix()}")
        self._directories.add(path)
        if "children" in item:
            children, next_url = item["children"], item.get("children@odata.nextLink")
        elif item["folder"].get("childCount") == 0:
            return
        else:
            page = await self._json(item_url + "/children")
            children, next_url = page.get("value", []), page.get("@odata.nextLink")
        visited_pages = set()
        while True:
            if not isinstance(children, list):
                raise OneDriveError("OneDrive returned an invalid folder listing")
            for child in children:
                if not isinstance(child, dict) or not child.get("id"):
                    raise OneDriveError("OneDrive returned an item without an ID")
                child_url = f"{self._api}/shares/{self._share_id}/items/{quote(child['id'], safe='')}"
                await self._walk(child, path, child_url, drive_id)
            if not next_url:
                break
            if next_url in visited_pages:
                raise OneDriveError("OneDrive returned a repeated pagination link")
            visited_pages.add(next_url)
            page = await self._json(next_url)
            children, next_url = page.get("value", []), page.get("@odata.nextLink")

    def _destination(self, relative: Path) -> Path:
        target = (self._output / relative).resolve()
        if not target.is_relative_to(self._output):
            raise OneDriveError("A destination path points outside the download directory")
        return target

    async def _download(self, file: _RemoteFile) -> None:
        target = self._destination(file.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        url = file.download_url or file.api_url + "/content"
        if urlsplit(url).scheme != "https":
            raise OneDriveError("OneDrive returned an invalid download URL")
        headers = {} if file.download_url else self._api_headers(url)
        temporary = None
        try:
            async with self._client.stream("GET", url, headers=headers, follow_redirects=True) as response:
                self._check_status(response)
                with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".onedrive-",
                                                 suffix=".part", delete=False) as output:
                    temporary = Path(output.name)
                    count = 0
                    async for data in response.aiter_bytes(chunk_size=64 * 1024):
                        output.write(data)
                        count += len(data)
                if file.size is not None and count != file.size:
                    raise OneDriveError(f"Incomplete download: {file.path.as_posix()}")
            os.replace(temporary, target)
            self._downloaded_files += 1
            print(f"Download progress: {self._downloaded_files}/{len(self._files)} "
                  f"({100 * self._downloaded_files / len(self._files):.2f}%)")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def download_async(self, path: str | Path = ".", concurrent_reqs: int = 5,
                             *, include: list[str] | None = None, dry_run: bool = False) -> None:
        """Async entry point; include patterns match relative paths or basenames."""
        if isinstance(concurrent_reqs, bool) or not isinstance(concurrent_reqs, int) or concurrent_reqs < 1:
            raise ValueError("concurrent_reqs must be a positive integer")
        self._files.clear()
        self._directories.clear()
        self._downloaded_files = 0
        self._output = Path(path).expanduser().resolve()
        async with httpx.AsyncClient(timeout=300) as client:
            self._client = client
            try:
                print("Traversing directory tree..")
                root, root_url = await self._resolve_root()
                await self._walk(root, Path(), root_url)
                if include:
                    self._files = {key: file for key, file in self._files.items()
                                   if any(fnmatchcase(key, pattern) or fnmatchcase(file.path.name, pattern)
                                          for pattern in include)}
                print(f"Found {len(self._files)} files" + (" (preview only)." if dry_run else ", downloading.."))
                if dry_run:
                    for file in self._files.values():
                        print(f"{file.size if file.size is not None else '?'}\t{file.path.as_posix()}")
                    return
                self._output.mkdir(parents=True, exist_ok=True)
                if not include:
                    for directory in self._directories:
                        self._destination(directory).mkdir(parents=True, exist_ok=True)
                for batch in chunks(self._files.values(), concurrent_reqs):
                    tasks = [asyncio.create_task(self._download(file)) for file in batch]
                    try:
                        await asyncio.gather(*tasks)
                    except BaseException:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        raise
            finally:
                self._client = None

    def download(self, path: str | Path = ".", concurrent_reqs: int = 5,
                 *, include: list[str] | None = None, dry_run: bool = False) -> None:
        """Download the shared item, preserving its name and folder structure."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.download_async(path, concurrent_reqs, include=include, dry_run=dry_run))
        else:
            raise RuntimeError("Use 'await downloader.download_async(...)' inside an async application")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="OneDrive or SharePoint sharing link")
    parser.add_argument("-o", "--output", default=".", help="Destination directory")
    parser.add_argument("-j", "--concurrent", type=int, default=5, help="Concurrent file downloads (default: 5)")
    parser.add_argument("--include", action="append", help="Download only matching paths or filenames; repeatable")
    parser.add_argument("--list", action="store_true", help="Preview paths and sizes without writing files")
    args = parser.parse_args()
    try:
        downloader = OneDriveSharedFolder(args.url, access_token=os.environ.get("ONEDRIVE_ACCESS_TOKEN"))
        downloader.download(args.output, args.concurrent, include=args.include, dry_run=args.list)
    except (OneDriveError, ValueError, OSError) as error:
        parser.exit(1, f"Error: {error}\n")
    except httpx.HTTPError as error:
        # httpx exception strings can contain credential-bearing signed URLs.
        parser.exit(1, f"Network request failed ({type(error).__name__}); retry the download.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
