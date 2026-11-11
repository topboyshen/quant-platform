#!/usr/bin/env python3
"""Bounded GitHub release reader for the QLib release fallback.

The only network targets are the fixed GitHub release API for
``chenditc/investment_data`` and the two release assets it names. The release
manifest asset is parsed and cross-checked against the API asset list before
the archive is downloaded. The archive is size- and SHA-256-verified before a
restricted extractor accepts only regular files and directories below one
``qlib_bin/`` top-level directory.

Every failure is a fixed machine code; no URL, response body, asset host or
filesystem detail is included in the exception text.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path, PurePosixPath
from urllib.parse import quote

GITHUB_REPO = "chenditc/investment_data"
RELEASE_API = "https://api.github.com/repos/" + GITHUB_REPO + "/releases/tags/{tag}"
DOWNLOAD_PREFIX = "https://github.com/" + GITHUB_REPO + "/releases/download/"

REF_RE = re.compile(r"[0-9a-v]{32}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
TAG_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
PROXY_RE = re.compile(
    r"(?:socks5h|socks5|http|https)://(?:[A-Za-z0-9.\-]+|\[[0-9A-Fa-f:]+\])(?::[0-9]{1,5})?\Z")
ARCHIVE_NAME = "qlib_bin.tar.gz"
MANIFEST_NAME = "qlib_bin.manifest.json"
TOP_LEVEL = "qlib_bin"

MAX_API_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_ARCHIVE_BYTES = 1_500_000_000
MAX_EXTRACT_BYTES = 4_000_000_000
MAX_MEMBERS = 300_000
DEFAULT_METADATA_SECONDS = 25
DEFAULT_ARCHIVE_SECONDS = 1200


class ReleaseError(RuntimeError):
    """A fixed, safe machine code for a release-package failure."""


def _fail(code: str) -> "ReleaseError":
    return ReleaseError(code)


def _tag(tag: str) -> str:
    if not isinstance(tag, str) or TAG_RE.fullmatch(tag) is None:
        raise _fail("invalid_release_tag")
    return tag


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _fail("release_manifest_duplicate_key")
        result[key] = value
    return result


def parse_manifest_bytes(raw: bytes, tag: str) -> dict:
    """Parse the small manifest asset and enforce its fixed schema."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_MANIFEST_BYTES:
        raise _fail("release_manifest_too_large")
    try:
        document = json.loads(
            bytes(raw).decode("utf-8"), object_pairs_hook=_json_object,
            parse_constant=lambda _: (_ for _ in ()).throw(_fail("release_manifest_nonfinite")))
    except ReleaseError:
        raise
    except (UnicodeError, ValueError):
        raise _fail("release_manifest_invalid_json") from None
    if not isinstance(document, dict):
        raise _fail("release_manifest_not_object")
    if document.get("release_tag") != tag:
        raise _fail("release_tag_mismatch")
    if document.get("target_trade_date") != tag:
        raise _fail("release_target_mismatch")
    commit = document.get("dolt_commit")
    if not isinstance(commit, str) or REF_RE.fullmatch(commit) is None:
        raise _fail("release_commit_invalid")
    size = document.get("archive_size_bytes")
    if type(size) is not int or not 0 < size <= MAX_ARCHIVE_BYTES:
        raise _fail("release_archive_size_invalid")
    digest = document.get("archive_sha256")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise _fail("release_archive_hash_invalid")
    digest = digest[len("sha256:"):]
    if SHA256_RE.fullmatch(digest) is None:
        raise _fail("release_archive_hash_invalid")
    return {
        "release_tag": tag,
        "target_trade_date": tag,
        "dolt_commit": commit,
        "archive_size_bytes": size,
        "archive_sha256": digest,
        "future_start_date": document.get("future_start_date"),
        "future_end_date": document.get("future_end_date"),
    }


def _asset(api: dict, name: str) -> dict:
    assets = api.get("assets")
    if not isinstance(assets, list):
        raise _fail("release_assets_invalid")
    matches = [item for item in assets if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1:
        raise _fail("release_asset_missing:" + name)
    item = matches[0]
    url = item.get("browser_download_url")
    if not isinstance(url, str) or not url.startswith(DOWNLOAD_PREFIX):
        raise _fail("release_asset_url_invalid")
    _tag_from_url(url)
    size = item.get("size")
    if type(size) is not int or size <= 0:
        raise _fail("release_asset_size_invalid")
    return {"name": name, "size": size, "url": url}


def _tag_from_url(url: str) -> str:
    """Return the release tag segment of a well-formed download URL."""
    remainder = url[len(DOWNLOAD_PREFIX):]
    tag, separator, rest = remainder.partition("/")
    if not separator or not rest or "/" in tag or TAG_RE.fullmatch(tag) is None:
        raise _fail("release_asset_url_invalid")
    return tag


def validate_release_api(api: dict, tag: str) -> dict:
    if not isinstance(api, dict):
        raise _fail("release_api_not_object")
    if api.get("tag_name") != tag:
        raise _fail("release_api_tag_mismatch")
    if api.get("draft") is not False or api.get("prerelease") is not False:
        raise _fail("release_api_not_published")
    manifest_asset = _asset(api, MANIFEST_NAME)
    archive_asset = _asset(api, ARCHIVE_NAME)
    if _tag_from_url(manifest_asset["url"]) != tag or _tag_from_url(archive_asset["url"]) != tag:
        raise _fail("release_asset_tag_mismatch")
    if manifest_asset["size"] > MAX_MANIFEST_BYTES:
        raise _fail("release_manifest_asset_too_large")
    if archive_asset["size"] > MAX_ARCHIVE_BYTES:
        raise _fail("release_archive_asset_too_large")
    return {"manifest": manifest_asset, "archive": archive_asset}


def _valid_proxy(proxy: str) -> bool:
    if not isinstance(proxy, str) or PROXY_RE.fullmatch(proxy) is None:
        return False
    hostport = proxy.split("://", 1)[1]
    if hostport.startswith("["):
        _, separator, tail = hostport.partition("]")
        if not separator:
            return False
        if tail.startswith(":") and not tail[1:].isdigit():
            return False
        port = int(tail[1:]) if tail.startswith(":") else None
    elif ":" in hostport:
        host, port_text = hostport.rsplit(":", 1)
        if not host or not port_text.isdigit():
            return False
        port = int(port_text)
    else:
        port = None
    return port is None or 1 <= port <= 65535


def _curl_command(url: str, *, max_time: int, max_bytes: int, output: Path | None,
                  proxy: str | None, connect_timeout: int) -> list[str]:
    if not isinstance(url, str) or not url.startswith("https://"):
        raise _fail("invalid_release_url")
    if proxy is not None and not _valid_proxy(proxy):
        raise _fail("invalid_proxy")
    if type(max_time) is not int or not 1 <= max_time <= 7200:
        raise _fail("invalid_release_timeout")
    if type(connect_timeout) is not int or not 1 <= connect_timeout <= 60:
        raise _fail("invalid_release_connect_timeout")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_EXTRACT_BYTES:
        raise _fail("invalid_release_byte_limit")
    args = [
        "curl", "-q", "-sS", "--location", "--max-redirs", "2",
        "--proto", "=https", "--proto-redir", "=https", "--tlsv1.2",
        "--connect-timeout", str(connect_timeout), "--max-time", str(max_time),
        "--max-filesize", str(max_bytes),
        "-H", "User-Agent: qlib-release-fallback/1",
        "-H", "Accept: application/vnd.github+json",
        "-H", "X-GitHub-Api-Version: 2022-11-28",
    ]
    if proxy:
        args += ["--proxy", proxy, "--noproxy", ""]
    else:
        args += ["--noproxy", "*"]
    if output is not None:
        args += ["-o", str(output)]
    args += ["--write-out", "\nQLIB_RELEASE_HTTP:%{http_code}", url]
    return args


def _run_curl(args: list[str], *, timeout: int) -> tuple[int, bytes]:
    try:
        completed = subprocess.run(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        raise _fail("release_transport_failed") from None
    return completed.returncode, completed.stdout


def _decode_http(rc: int, output: bytes) -> tuple[int, bytes]:
    body, separator, status = output.rpartition(b"\nQLIB_RELEASE_HTTP:")
    if not separator or re.fullmatch(rb"[0-9]{3}", status) is None:
        raise _fail("release_transport_response_invalid")
    code = int(status)
    if rc != 0 and code == 0:
        raise _fail("release_transport_failed")
    return code, body


def fetch_release_api(tag: str, *, proxy: str | None = None,
                      connect_timeout: int = 8,
                      max_time: int = DEFAULT_METADATA_SECONDS) -> dict:
    """Fetch and validate the GitHub release metadata for exactly ``tag``."""
    tag = _tag(tag)
    url = RELEASE_API.format(tag=quote(tag, safe=""))
    args = _curl_command(url, max_time=max_time, max_bytes=MAX_API_BYTES,
                         output=None, proxy=proxy, connect_timeout=connect_timeout)
    rc, output = _run_curl(args, timeout=max_time + 5)
    code, body = _decode_http(rc, output)
    if code == 404:
        raise _fail("release_not_published")
    if code != 200:
        raise _fail("release_api_http_rejected")
    if len(body) > MAX_API_BYTES:
        raise _fail("release_api_too_large")
    try:
        api = json.loads(body.decode("utf-8"), object_pairs_hook=_json_object,
                         parse_constant=lambda _: (_ for _ in ()).throw(_fail("release_api_nonfinite")))
    except ReleaseError:
        raise
    except (UnicodeError, ValueError):
        raise _fail("release_api_invalid_json") from None
    if not isinstance(api, dict):
        raise _fail("release_api_not_object")
    validated = validate_release_api(api, tag)
    return {"tag": tag, "assets": validated}


def download_manifest_asset(asset: dict, dest: Path, *, proxy: str | None = None,
                            connect_timeout: int = 8,
                            max_time: int = DEFAULT_METADATA_SECONDS) -> bytes:
    dest = Path(dest)
    if dest.exists() or dest.is_symlink():
        raise _fail("release_manifest_destination_exists")
    part = dest.with_name(dest.name + ".part")
    args = _curl_command(asset["url"], max_time=max_time, max_bytes=MAX_MANIFEST_BYTES,
                         output=part, proxy=proxy, connect_timeout=connect_timeout)
    rc, output = _run_curl(args, timeout=max_time + 5)
    code, _ = _decode_http(rc, output)
    try:
        if code != 200:
            raise _fail("release_manifest_http_rejected")
        size = part.stat().st_size
        if size > MAX_MANIFEST_BYTES or size != asset["size"]:
            raise _fail("release_manifest_size_mismatch")
        raw = part.read_bytes()
        if len(raw) != size:
            raise _fail("release_manifest_size_mismatch")
        os.replace(part, dest)
        return raw
    finally:
        if part.exists():
            part.unlink()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_archive(asset: dict, size: int, digest: str, dest: Path, *,
                     proxy: str | None = None, connect_timeout: int = 10,
                     max_time: int = DEFAULT_ARCHIVE_SECONDS) -> Path:
    """Download exactly one archive; never retry a completed hash mismatch."""
    dest = Path(dest)
    if dest.exists() or dest.is_symlink():
        raise _fail("release_archive_destination_exists")
    if type(size) is not int or not 0 < size <= MAX_ARCHIVE_BYTES:
        raise _fail("release_archive_size_invalid")
    if SHA256_RE.fullmatch(digest) is None:
        raise _fail("release_archive_hash_invalid")
    part = dest.with_name(dest.name + ".part")
    args = _curl_command(asset["url"], max_time=max_time, max_bytes=size + 1,
                         output=part, proxy=proxy, connect_timeout=connect_timeout)
    try:
        rc, output = _run_curl(args, timeout=max_time + 10)
        code, _ = _decode_http(rc, output)
        if code != 200:
            raise _fail("release_archive_http_rejected")
        actual_size = part.stat().st_size
        if actual_size != size:
            raise _fail("release_archive_size_mismatch")
        actual_digest = _sha256_file(part)
        if actual_digest != digest:
            raise _fail("release_archive_hash_mismatch")
        os.replace(part, dest)
        return dest
    finally:
        if part.exists():
            part.unlink()


def _safe_member(member: tarfile.TarInfo) -> None:
    name = member.name
    if not isinstance(name, str) or not name or "\x00" in name:
        raise _fail("release_archive_member_invalid")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise _fail("release_archive_member_invalid")
    if path.parts[0] != TOP_LEVEL:
        raise _fail("release_archive_top_level_invalid")
    if member.isdir():
        return
    if not member.isfile():
        raise _fail("release_archive_member_type_invalid")
    if type(member.size) is not int or member.size < 0:
        raise _fail("release_archive_member_size_invalid")


def inspect_archive(archive: Path) -> list[tarfile.TarInfo]:
    """Reject traversal, links, devices, duplicates, and oversized archives."""
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
    except (OSError, tarfile.TarError):
        raise _fail("release_archive_invalid") from None
    if not members or len(members) > MAX_MEMBERS:
        raise _fail("release_archive_member_count_invalid")
    names = set()
    total = 0
    for member in members:
        _safe_member(member)
        if member.name in names:
            raise _fail("release_archive_duplicate_member")
        names.add(member.name)
        if member.isfile():
            total += member.size
            if total > MAX_EXTRACT_BYTES:
                raise _fail("release_archive_extract_too_large")
    if TOP_LEVEL + "/calendars/day.txt" not in names:
        raise _fail("release_archive_calendar_missing")
    if TOP_LEVEL + "/instruments/all.txt" not in names:
        raise _fail("release_archive_instruments_missing")
    return members


def extract_archive(archive: Path, dest: Path) -> Path:
    """Extract an inspected archive into a fresh directory using data filter."""
    dest = Path(dest)
    if dest.exists() or dest.is_symlink():
        raise _fail("release_extract_destination_exists")
    members = inspect_archive(archive)
    dest.mkdir(mode=0o700, parents=True)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            try:
                tar.extractall(dest, members=members, filter="data")
            except TypeError:
                # Python 3.11 has no extraction filter; inspect_archive has
                # already rejected traversal, links, devices and duplicates.
                tar.extractall(dest, members=members)
    except (OSError, tarfile.TarError):
        shutil.rmtree(dest, ignore_errors=True)
        raise _fail("release_archive_extract_failed") from None
    root = dest / TOP_LEVEL
    if (root.is_symlink() or not root.is_dir()
            or (root / "calendars" / "day.txt").is_symlink()
            or (root / "instruments" / "all.txt").is_symlink()
            or (root / "features").is_symlink()):
        shutil.rmtree(dest, ignore_errors=True)
        raise _fail("release_extract_layout_invalid")
    return root


def prepare_release(tag: str, work_dir: Path, *, proxy: str | None = None,
                    metadata_timeout: int = DEFAULT_METADATA_SECONDS,
                    archive_timeout: int = DEFAULT_ARCHIVE_SECONDS,
                    on_stage=None) -> dict:
    """Fetch, verify and extract one release; return paths and manifest."""
    tag = _tag(tag)
    work = Path(work_dir)
    if work.exists() and (work.is_symlink() or not work.is_dir()):
        raise _fail("release_work_dir_invalid")
    work.mkdir(mode=0o700, parents=True, exist_ok=True)

    def mark(name: str) -> None:
        if on_stage is not None:
            on_stage(name)

    mark("release_manifest")
    api = fetch_release_api(tag, proxy=proxy, max_time=metadata_timeout)
    mark("release_manifest_download")
    manifest_raw = download_manifest_asset(
        api["assets"]["manifest"], work / f"{MANIFEST_NAME}.{tag}",
        proxy=proxy, max_time=metadata_timeout)
    manifest = parse_manifest_bytes(manifest_raw, tag)
    archive_asset = api["assets"]["archive"]
    if archive_asset["size"] != manifest["archive_size_bytes"]:
        raise _fail("release_archive_size_disagrees_with_manifest")
    mark("release_archive_download")
    archive = download_archive(
        archive_asset, manifest["archive_size_bytes"], manifest["archive_sha256"],
        work / f"{ARCHIVE_NAME}.{tag}", proxy=proxy, max_time=archive_timeout)
    mark("release_extract")
    root = extract_archive(archive, work / f"extract-{tag}")
    return {"tag": tag, "manifest": manifest, "archive": archive,
            "release_dir": root, "work_dir": work}


def wait_for_release(tag: str, deadline: float, *, poll_seconds: int = 600,
                     proxy: str | None = None,
                     metadata_timeout: int = DEFAULT_METADATA_SECONDS) -> dict:
    """Poll only for a not-yet-published tag, bounded by a monotonic deadline."""
    tag = _tag(tag)
    if type(deadline) not in (int, float) or not deadline > time.monotonic():
        raise _fail("invalid_release_deadline")
    if type(poll_seconds) is not int or not 10 <= poll_seconds <= 3600:
        raise _fail("invalid_release_poll_interval")
    while True:
        try:
            return fetch_release_api(tag, proxy=proxy, max_time=metadata_timeout)
        except ReleaseError as exc:
            if str(exc) != "release_not_published":
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _fail("release_not_published") from None
            time.sleep(min(poll_seconds, remaining))
