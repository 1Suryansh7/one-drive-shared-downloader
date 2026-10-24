# Repair and verification

Validated on 24 October 2026 on Windows with Python 3.13.

## Changes

- Replaced obsolete HTTPX 0.16 / Trio 0.17 dependencies with HTTPX 0.28 and
  Python's standard asyncio runtime.
- Added anonymous guest sessions for public personal OneDrive shares migrated to
  the current backend. The original unauthenticated API remains the first attempt.
- Use the signed download URL; the misleading downloadUrlNoAuth variant returned
  HTTP 401 for the upstream example.
- Fixed single-file downloads, empty folders, ignored destination directories,
  subfolder permissions, and paginated listings.
- Stream downloads to temporary files and publish them only after success.
- Added a CLI, file-selection patterns, listing mode, an async entry point, and
  optional authorized Microsoft Graph tokens.

## Live verification

The fixed CLI traversed the public folder in the upstream README and downloaded
only Wallpaper/1540908626811.png. The rest of the approximately 1.9 GB share
was not downloaded.

- Download completed: 1/1.
- File size matched the API metadata: **61,882 bytes**.
- PNG signature verified; dimensions: **1920 x 1080**.
- SHA-256: bb8cd052742a1db6869849b0755673586dbe431b5112ec7d315f9689d3a84d46

## Automated verification

All 16 offline regression tests passed. They cover single files, destination
handling and object reuse, nested and empty folders, pagination, migrated guest
sessions, signed URL selection, Graph authentication, redirect credentials,
selective previews, HTTP failures, truncated/interrupted streams, unexpected
pagination hosts, unsafe paths, concurrency limits, and async usage.

## Practical limits

Only the public personal OneDrive route was verified against the live service.
The authenticated Graph route was tested with mocked responses, without a real
account or credentials. Public guest endpoints are undocumented and can change.
The tool does not make private links public or bypass owner download restrictions.

The original repository import and download call remain supported. Use a fresh
virtual environment when upgrading from the old dependencies.
