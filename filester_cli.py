#!/usr/bin/env python3
"""
filester-cli
============

A small command-line wrapper + auto-upload daemon for the Filester
(https://filester.me) storage API, built for pushing ctbrec recordings
straight from a VPS instead of downloading locally first.

Commands:
    account                     Show account info / storage usage
    folders                     List all folders
    mkdir <path>                Create a folder (path form auto-creates parents, e.g. "Streamers/Alice")
    files                       List files (optionally within a folder)
    upload <path>               Upload a single file or a whole directory (recursively)
    watch                       Watch a directory and auto-upload finished recordings

Config is read from environment variables (or a .env-style file passed
with --env-file), see .env.example:
    FILESTER_API_KEY
    FILESTER_BASE_URL        (default: https://u1.filester.me)
    FILESTER_MAX_RETRIES     (default: 5)

Docs reference: https://filester.me/api-docs
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import random
import signal
import sys
import time
from pathlib import Path

import requests
from requests_toolbelt.multipart.encoder import (
    MultipartEncoder,
    MultipartEncoderMonitor,
)

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

DEFAULT_BASE_URL = "https://u1.filester.me"
DEFAULT_STATE_FILE = "~/.filester-cli/state.json"
DEFAULT_EXTENSIONS = [".mp4", ".flv", ".ts", ".mkv", ".m4v"]
DEFAULT_IGNORE_PATTERNS = ["*.tmp", "*.part", "*.download", ".*"]

log = logging.getLogger("filester")


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class FilesterError(Exception):
    def __init__(self, message, status_code=None, payload=None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------


class FilesterClient:
    def __init__(self, api_key=None, base_url=None, max_retries=5, connect_timeout=15):
        self.api_key = api_key or os.environ.get("FILESTER_API_KEY")
        self.base_url = (
            base_url or os.environ.get("FILESTER_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.max_retries = max_retries
        self.connect_timeout = connect_timeout
        self.session = requests.Session()

    def _headers(self, extra=None):
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if extra:
            headers.update(extra)
        return headers

    def _backoff(self, attempt, reason):
        delay = min(60, 2**attempt) + random.uniform(0, 1)
        log.warning(
            "retrying after %s (attempt %d/%d) - sleeping %.1fs",
            reason,
            attempt,
            self.max_retries,
            delay,
        )
        time.sleep(delay)

    def _request(self, method, path, retryable=True, **kwargs):
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self.session.request(
                    method,
                    url,
                    timeout=(self.connect_timeout, 120),
                    headers=self._headers(kwargs.pop("headers", None)),
                    **kwargs,
                )
            except requests.RequestException as exc:
                if retryable and attempt <= self.max_retries:
                    self._backoff(attempt, f"network error: {exc}")
                    continue
                raise FilesterError(
                    f"network error calling {method} {path}: {exc}"
                ) from exc

            if (
                retryable
                and (resp.status_code == 429 or resp.status_code >= 500)
                and attempt <= self.max_retries
            ):
                self._backoff(attempt, f"HTTP {resp.status_code}")
                continue

            return self._parse(resp)

    @staticmethod
    def _parse(resp):
        try:
            data = resp.json()
        except ValueError:
            data = {
                "success": False,
                "message": (resp.text or "")[:500] or f"HTTP {resp.status_code}",
            }
        if not resp.ok or data.get("success") is False:
            raise FilesterError(
                data.get("message", f"HTTP {resp.status_code}"),
                status_code=resp.status_code,
                payload=data,
            )
        return data

    # ---- account / health ----
    def account(self):
        return self._request("GET", "/api/v1/account")["data"]

    def health(self):
        return self._request("GET", "/health", retryable=False)

    # ---- folders ----
    def list_folders(self):
        return self._request("GET", "/api/v1/folders")["data"]

    def create_folder(self, name, parent=None, public=1, password=None):
        body = {"name": name, "public": public}
        if parent and parent != "root":
            body["parent"] = parent
        if password:
            body["password"] = password
        return self._request("POST", "/api/v1/folder", json=body)["data"]

    def folder_files(self, identifier):
        return self._request("GET", f"/api/v1/folder/{identifier}/files")["data"]

    def delete_folders(self, identifiers):
        return self._request(
            "POST", "/folder/delete", json={"identifiers": identifiers}
        )

    # ---- files ----
    def list_files(self, page=1, per_page=20, folder=None, search=None):
        params = {"page": page, "per_page": per_page}
        if folder:
            params["folder"] = folder
        if search:
            params["search"] = search
        return self._request("GET", "/api/v1/files", params=params)

    def delete_files(self, identifiers):
        return self._request("POST", "/file/delete", json={"identifiers": identifiers})

    # ---- upload ----
    def upload_file(self, path: Path, folder_id: str | None = None, show_progress=True):
        size = path.stat().st_size
        bar = None
        if show_progress:
            try:
                from tqdm import tqdm

                bar = tqdm(
                    total=size,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=path.name[:30],
                    leave=False,
                )
            except ImportError:
                bar = None

        attempt = 0
        try:
            while True:
                attempt += 1
                fh = open(path, "rb")
                try:
                    encoder = MultipartEncoder(
                        fields={"file": (path.name, fh, "application/octet-stream")}
                    )
                    if bar:
                        bar.reset(total=size)
                        monitor = MultipartEncoderMonitor(
                            encoder, lambda m: bar.update(m.bytes_read - bar.n)
                        )
                    else:
                        monitor = MultipartEncoderMonitor(encoder)

                    headers = self._headers({"Content-Type": monitor.content_type})
                    if folder_id:
                        headers["X-Folder-ID"] = folder_id

                    resp = self.session.post(
                        f"{self.base_url}/api/v1/upload",
                        data=monitor,
                        headers=headers,
                        timeout=(self.connect_timeout, None),
                    )
                except requests.RequestException as exc:
                    if attempt <= self.max_retries:
                        self._backoff(attempt, f"network error: {exc}")
                        continue
                    raise FilesterError(f"upload failed for {path}: {exc}") from exc
                finally:
                    fh.close()

                if (
                    resp.status_code == 429 or resp.status_code >= 500
                ) and attempt <= self.max_retries:
                    self._backoff(attempt, f"HTTP {resp.status_code}")
                    continue

                return self._parse(resp)
        finally:
            if bar:
                bar.close()


# --------------------------------------------------------------------------
# Folder path resolution (e.g. "Streamers/Alice" -> creates both levels)
# --------------------------------------------------------------------------


class FolderResolver:
    def __init__(self, client: FilesterClient):
        self.client = client
        self._index = {}
        self._folders = []
        self._folders_by_id = {}
        self._loaded = False

    def _ensure_loaded(self):
        if self._loaded:
            return
        self._folders = self.client.list_folders()
        self._folders_by_id = {f["id"]: f for f in self._folders}
        for f in self._folders:
            self._index[(f.get("parent_id"), f["name"])] = f["id"]
        self._loaded = True

    def _full_path(self, folder: dict) -> str:
        parts = [folder["name"]]
        p = folder.get("parent_id")
        while p:
            parent = self._folders_by_id.get(p)
            if not parent:
                break
            parts.insert(0, parent["name"])
            p = parent.get("parent_id")
        return "/".join(parts)

    def all_paths(self):
        """List of (full_path, id) for every existing folder."""
        self._ensure_loaded()
        return [(self._full_path(f), f["id"]) for f in self._folders]

    def resolve(self, path_str: str | None, create=True, public=1) -> str | None:
        """Strict resolution: '/'-separated path, auto-creates missing segments."""
        if not path_str or path_str == "root":
            return None
        self._ensure_loaded()
        parent = None
        for part in [p for p in path_str.split("/") if p]:
            key = (parent, part)
            folder_id = self._index.get(key)
            if folder_id is None:
                if not create:
                    raise FilesterError(
                        f"folder not found: {path_str!r} (missing segment {part!r})"
                    )
                created = self.client.create_folder(part, parent=parent, public=public)
                folder_id = created["identifier"]
                self._index[key] = folder_id
                folder_rec = {"id": folder_id, "name": part, "parent_id": parent}
                self._folders.append(folder_rec)
                self._folders_by_id[folder_id] = folder_rec
                log.info(
                    "created remote folder %r (id=%s, parent=%s)",
                    part,
                    folder_id,
                    parent,
                )
            parent = folder_id
        return parent

    def resolve_smart(self, query: str | None, create=True, public=1) -> str | None:
        """Like resolve(), but for a query that doesn't exactly match an existing
        path: does a case-insensitive substring search over all existing folder
        paths first, so you don't have to type/remember the exact path or id.
        - exactly one substring match -> use it
        - multiple matches -> interactive pick-list (or an error listing them, if not a tty)
        - no matches at all -> falls back to strict resolve() (creates the path)
        """
        if not query or query == "root":
            return None
        self._ensure_loaded()
        paths = self.all_paths()

        for path, fid in paths:
            if path == query:
                return fid

        q = query.lower()
        candidates = [(path, fid) for path, fid in paths if q in path.lower()]

        if len(candidates) == 1:
            path, fid = candidates[0]
            log.info("matched existing folder %r -> %s", path, fid)
            return fid

        if len(candidates) > 1:
            if sys.stdin.isatty():
                print(f"multiple folders match {query!r}:", file=sys.stderr)
                for i, (path, fid) in enumerate(candidates, 1):
                    print(f"  [{i}] {path}  ({fid})", file=sys.stderr)
                print(f"  [n] create new folder {query!r} instead", file=sys.stderr)
                choice = input("pick one: ").strip().lower()
                if choice != "n":
                    try:
                        return candidates[int(choice) - 1][1]
                    except (ValueError, IndexError):
                        raise FilesterError("invalid selection")
                # fall through to create below
            else:
                names = ", ".join(p for p, _ in candidates)
                raise FilesterError(
                    f"ambiguous folder {query!r}, matches: {names} - use the exact path/id, or run interactively"
                )

        return self.resolve(query, create=create, public=public)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:3.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}PB"


def matches_any(name: str, patterns) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def load_state(state_file: Path) -> dict:
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except (ValueError, OSError):
            log.warning("state file %s unreadable, starting fresh", state_file)
    return {"uploaded": {}}


def save_state(state_file: Path, state: dict):
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(state_file)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_account(client: FilesterClient, args):
    data = client.account()
    used, limit = data.get("storage_used", 0), data.get("storage_limit", 0)
    pct = (used / limit * 100) if limit else 0
    print(f"user:            {data.get('username')}")
    print(f"storage used:    {human_size(used)} / {human_size(limit)}  ({pct:.1f}%)")
    print(f"files:           {data.get('files_count')}")
    print(f"folders:         {data.get('folders_count')}")
    print(f"api requests today: {data.get('api_requests_today')}")
    if pct >= 80:
        print(
            "\n[!] storage is over 80% full - Filester's free tier caps at 10GB total,"
            " uploads will start failing once it's full.",
            file=sys.stderr,
        )


def cmd_folders(client: FilesterClient, args):
    resolver = FolderResolver(client)
    for path, fid in sorted(resolver.all_paths()):
        if args.search and args.search.lower() not in path.lower():
            continue
        print(f"{fid}  {path}")


def cmd_mkdir(client: FilesterClient, args):
    resolver = FolderResolver(client)
    folder_id = resolver.resolve(
        args.path, create=True, public=0 if args.private else 1
    )
    print(f"ready: {args.path} -> {folder_id}")


def cmd_files(client: FilesterClient, args):
    resolver = FolderResolver(client) if args.folder_path else None
    folder_id = (
        resolver.resolve_smart(args.folder_path, create=False)
        if resolver
        else args.folder_id
    )
    result = client.list_files(
        page=args.page, per_page=args.per_page, folder=folder_id, search=args.search
    )
    for f in result["data"]:
        print(
            f"{f.get('uuid', f.get('id')):<38} {human_size(f['size']):>10}  {f['name']:<40} {f['url']}"
        )
    pg = result.get("pagination", {})
    if pg:
        print(
            f"\npage {pg.get('page')}/{pg.get('pages')}  ({pg.get('total')} files total)",
            file=sys.stderr,
        )


def _upload_one(
    client: FilesterClient,
    resolver: FolderResolver,
    path: Path,
    folder_path: str | None,
    folder_id: str | None,
    no_progress: bool,
    delete_after: bool,
    smart: bool = True,
):
    if folder_path is not None:
        folder_id = (
            resolver.resolve_smart(folder_path)
            if smart
            else resolver.resolve(folder_path)
        )
    log.info(
        "uploading %s (%s)%s",
        path,
        human_size(path.stat().st_size),
        f" -> folder {folder_id}" if folder_id else "",
    )
    result = client.upload_file(
        path, folder_id=folder_id, show_progress=not no_progress
    )
    print(f"OK  {path.name} -> {result['url']}")
    if delete_after:
        path.unlink()
        log.info("deleted local file %s", path)
    return result


def cmd_upload(client: FilesterClient, args):
    resolver = FolderResolver(client)
    target = Path(args.path).expanduser()
    if not target.exists():
        raise FilesterError(f"no such file or directory: {target}")

    if target.is_file():
        _upload_one(
            client,
            resolver,
            target,
            args.folder_path,
            args.folder_id,
            args.no_progress,
            args.delete_after,
        )
        return

    # directory: walk recursively, optionally mirroring subfolders under --folder-path
    exts = [e.lower() for e in (args.ext or DEFAULT_EXTENSIONS)]
    count = 0
    for root, _dirs, files in os.walk(target):
        for name in sorted(files):
            fp = Path(root) / name
            if exts and fp.suffix.lower() not in exts:
                continue
            if matches_any(name, DEFAULT_IGNORE_PATTERNS):
                continue
            rel_parent = fp.parent.relative_to(target)
            if args.mirror and str(rel_parent) != ".":
                sub = "/".join(rel_parent.parts)
                dest_path = f"{args.folder_path}/{sub}" if args.folder_path else sub
            else:
                dest_path = args.folder_path
            try:
                _upload_one(
                    client,
                    resolver,
                    fp,
                    dest_path,
                    args.folder_id if not dest_path else None,
                    args.no_progress,
                    args.delete_after,
                )
                count += 1
            except FilesterError as exc:
                log.error("failed to upload %s: %s", fp, exc)
    print(f"\nuploaded {count} file(s) from {target}", file=sys.stderr)


def cmd_watch(client: FilesterClient, args):
    watch_dir = Path(args.dir).expanduser().resolve()
    if not watch_dir.is_dir():
        raise FilesterError(f"not a directory: {watch_dir}")

    state_file = Path(args.state_file).expanduser()
    state = load_state(state_file)
    resolver = FolderResolver(client)
    exts = [e.lower() for e in (args.ext or DEFAULT_EXTENSIONS)]

    stop = {"flag": False}

    def _handle_signal(signum, _frame):
        log.info("received signal %s, will stop after current file", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # size seen on the previous poll, for stability detection
    last_seen_size = {}

    log.info(
        "watching %s (poll every %ss, stable after %ss unchanged)",
        watch_dir,
        args.poll_interval,
        args.stable_seconds,
    )

    while not stop["flag"]:
        try:
            candidates = []
            for root, _dirs, files in os.walk(watch_dir):
                for name in files:
                    fp = Path(root) / name
                    if exts and fp.suffix.lower() not in exts:
                        continue
                    if matches_any(name, DEFAULT_IGNORE_PATTERNS):
                        continue
                    key = str(fp)
                    try:
                        stat = fp.stat()
                    except OSError:
                        continue

                    already = state["uploaded"].get(key)
                    if (
                        already
                        and already.get("size") == stat.st_size
                        and already.get("mtime") == stat.st_mtime
                    ):
                        continue  # already uploaded, unchanged since

                    prev_size = last_seen_size.get(key)
                    stable_by_age = (time.time() - stat.st_mtime) >= args.stable_seconds
                    stable_by_repeat = prev_size == stat.st_size
                    last_seen_size[key] = stat.st_size

                    if stat.st_size > 0 and (stable_by_age or stable_by_repeat):
                        candidates.append((fp, stat))

            for fp, stat in candidates:
                if stop["flag"]:
                    break
                rel_parent = fp.parent.relative_to(watch_dir)
                if args.mirror and str(rel_parent) != ".":
                    depth = args.mirror_depth or len(rel_parent.parts)
                    sub = "/".join(rel_parent.parts[:depth])
                    dest_path = f"{args.folder_path}/{sub}" if args.folder_path else sub
                else:
                    dest_path = args.folder_path

                try:
                    result = _upload_one(
                        client,
                        resolver,
                        fp,
                        dest_path,
                        args.folder_id if not dest_path else None,
                        args.no_progress,
                        delete_after=False,
                        smart=False,
                    )
                except FilesterError as exc:
                    log.error(
                        "upload failed for %s, will retry next cycle: %s", fp, exc
                    )
                    continue

                state["uploaded"][str(fp)] = {
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "uploaded_at": time.time(),
                    "url": result.get("url"),
                }
                save_state(state_file, state)

                if args.delete_after:
                    try:
                        fp.unlink()
                        log.info("deleted local file %s after successful upload", fp)
                    except OSError as exc:
                        log.error("could not delete %s: %s", fp, exc)

        except FilesterError as exc:
            log.error("watch cycle error: %s", exc)

        for _ in range(int(args.poll_interval)):
            if stop["flag"]:
                break
            time.sleep(1)

    log.info("stopped")


# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="filester-cli", description="CLI wrapper for the Filester storage API"
    )
    p.add_argument(
        "--env-file",
        default=".env",
        help="path to a .env file to load (default: ./.env if present)",
    )
    p.add_argument("--api-key", default=None, help="overrides FILESTER_API_KEY")
    p.add_argument("--base-url", default=None, help="overrides FILESTER_BASE_URL")
    p.add_argument(
        "--max-retries",
        type=int,
        default=int(os.environ.get("FILESTER_MAX_RETRIES", 5)),
    )
    p.add_argument("-v", "--verbose", action="store_true")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("account", help="show account info / storage usage")
    sp = sub.add_parser("folders", help="list all folders")
    sp.add_argument(
        "--search",
        default=None,
        help="only show folders whose path contains this (case-insensitive)",
    )

    sp = sub.add_parser(
        "mkdir", help='create a folder, e.g. "Streamers/Alice" creates both levels'
    )
    sp.add_argument("path")
    sp.add_argument("--private", action="store_true")

    sp = sub.add_parser("files", help="list files")
    sp.add_argument("--folder-id", default=None)
    sp.add_argument("--folder-path", default=None, help='e.g. "Streamers/Alice"')
    sp.add_argument("--page", type=int, default=1)
    sp.add_argument("--per-page", type=int, default=20)
    sp.add_argument("--search", default=None)

    sp = sub.add_parser(
        "upload", help="upload a single file, or every matching file in a directory"
    )
    sp.add_argument("path")
    sp.add_argument(
        "--folder-id", default=None, help="upload into this existing folder id"
    )
    sp.add_argument(
        "--folder-path",
        default=None,
        help='remote destination, e.g. "Streamers/Alice" (auto-created)',
    )
    sp.add_argument(
        "--mirror",
        action="store_true",
        help="when path is a directory, mirror its subfolders remotely",
    )
    sp.add_argument(
        "--ext",
        nargs="*",
        default=None,
        help=f"only upload these extensions (default: {DEFAULT_EXTENSIONS})",
    )
    sp.add_argument(
        "--delete-after",
        action="store_true",
        help="delete local file(s) once uploaded successfully",
    )
    sp.add_argument("--no-progress", action="store_true")

    sp = sub.add_parser(
        "watch", help="watch a directory and auto-upload finished recordings"
    )
    sp.add_argument(
        "--dir",
        required=True,
        help="directory to watch (e.g. ctbrec's recordings folder)",
    )
    sp.add_argument(
        "--folder-id",
        default=None,
        help="upload everything into this existing folder id",
    )
    sp.add_argument(
        "--folder-path",
        default=None,
        help='remote base folder, e.g. "Streamers" (auto-created)',
    )
    sp.add_argument(
        "--mirror",
        action="store_true",
        default=True,
        help="mirror subfolder names remotely (default: on)",
    )
    sp.add_argument("--no-mirror", dest="mirror", action="store_false")
    sp.add_argument(
        "--mirror-depth",
        type=int,
        default=1,
        help="how many subfolder levels to mirror (default: 1, e.g. per-model)",
    )
    sp.add_argument("--ext", nargs="*", default=None)
    sp.add_argument(
        "--poll-interval",
        type=float,
        default=30,
        help="seconds between directory scans",
    )
    sp.add_argument(
        "--stable-seconds",
        type=float,
        default=60,
        help="a file must be unchanged this long before it's uploaded",
    )
    sp.add_argument(
        "--delete-after",
        action="store_true",
        help="delete local file once uploaded (saves disk, not just bandwidth)",
    )
    sp.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    sp.add_argument("--no-progress", action="store_true")

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    env_path = Path(args.env_file).expanduser()
    if env_path.exists():
        if load_dotenv:
            load_dotenv(env_path, override=False)
            log.debug("loaded env vars from %s", env_path)
        else:
            log.warning(
                "%s exists but python-dotenv isn't installed - run `uv pip install python-dotenv` "
                "or pass --env-file/-export vars manually",
                env_path,
            )

    client = FilesterClient(
        api_key=args.api_key, base_url=args.base_url, max_retries=args.max_retries
    )
    if not client.api_key:
        log.warning(
            "no API key set (FILESTER_API_KEY) - uploads will go through as anonymous guest uploads"
        )

    handlers = {
        "account": cmd_account,
        "folders": cmd_folders,
        "mkdir": cmd_mkdir,
        "files": cmd_files,
        "upload": cmd_upload,
        "watch": cmd_watch,
    }

    try:
        handlers[args.command](client, args)
    except FilesterError as exc:
        log.error("%s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
