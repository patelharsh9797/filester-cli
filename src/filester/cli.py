"""
filester-cli
============

A CLI + watch-daemon for the Filester (https://filester.me) storage API.
Built for pushing files (originally: ctbrec stream recordings) straight
from a server to Filester without downloading them locally first.

After installing, everything is available under one command:

    filester account
    filester folders
    filester mkdir "Streamers/Alice"
    filester upload video.mp4 --folder-path alice
    filester watch --dir /recordings --folder-path Streamers --mirror --delete-after

Config comes from environment variables (or a .env file next to where
you run it): FILESTER_API_KEY, FILESTER_BASE_URL, FILESTER_MAX_RETRIES.
See .env.example.
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
from typing import Optional

import requests
from requests_toolbelt.multipart.encoder import (
    MultipartEncoder,
    MultipartEncoderMonitor,
)
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.prompt import IntPrompt
from rich.table import Table

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    from rich_argparse import RichHelpFormatter
except ImportError:
    RichHelpFormatter = argparse.HelpFormatter

from . import __version__

DEFAULT_BASE_URL = "https://u1.filester.me"
DEFAULT_STATE_FILE = "~/.filester-cli/state.json"
DEFAULT_CONFIG_DIR = (
    Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "filester"
)
DEFAULT_EXTENSIONS = [".mp4", ".flv", ".ts", ".mkv", ".m4v"]
DEFAULT_IGNORE_PATTERNS = ["*.tmp", "*.part", "*.download", ".*"]

console = Console()
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
    def upload_file(
        self, path: Path, folder_id: Optional[str] = None, show_progress=True
    ):
        size = path.stat().st_size
        progress = None
        task_id = None
        if show_progress:
            progress = Progress(
                SpinnerColumn(),
                TextColumn(
                    "[bold cyan]{task.fields[filename]}[/][yellow]{task.fields[note]}[/]"
                ),
                BarColumn(bar_width=30),
                "[progress.percentage]{task.percentage:>3.1f}%",
                DownloadColumn(),
                TransferSpeedColumn(),
                TimeRemainingColumn(),
                console=console,
                transient=True,
            )
            progress.start()
            task_id = progress.add_task(
                "upload", filename=path.name[:32], note="", total=size
            )

        def _on_progress(monitor):
            assert progress is not None
            progress.update(task_id, completed=monitor.bytes_read)
            if monitor.bytes_read >= size:
                progress.update(
                    task_id,
                    note=" - upload sent, waiting for Filester to process...",
                )

        attempt = 0
        try:
            while True:
                attempt += 1
                if progress is not None:
                    progress.reset(task_id, total=size)
                    progress.update(task_id, note="")
                fh = open(path, "rb")
                try:
                    encoder = MultipartEncoder(
                        fields={"file": (path.name, fh, "application/octet-stream")}
                    )
                    if progress is not None:
                        monitor = MultipartEncoderMonitor(encoder, _on_progress)
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
            if progress is not None:
                progress.stop()


# --------------------------------------------------------------------------
# Folder path resolution
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
        self._ensure_loaded()
        return [(self._full_path(f), f["id"]) for f in self._folders]

    def resolve(self, path_str: Optional[str], create=True, public=1) -> Optional[str]:
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
                    "created remote folder %r (id=%s)",
                    self._full_path(folder_rec),
                    folder_id,
                )
            parent = folder_id
        return parent

    def resolve_smart(
        self, query: Optional[str], create=True, public=1
    ) -> Optional[str]:
        """Fuzzy resolution for interactive use: case-insensitive substring
        search over existing folder paths, so you don't need the exact path
        or an id. Exactly one match -> use it. Multiple -> pick-list (or an
        error if not a tty). No matches -> falls back to strict resolve()."""
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
            log.info("matched existing folder [bold]%s[/] -> %s", path, fid)
            return fid

        if len(candidates) > 1:
            if sys.stdin.isatty():
                table = Table(
                    title=f"multiple folders match {query!r}", show_lines=False
                )
                table.add_column("#", justify="right", style="cyan")
                table.add_column("path", style="bold")
                table.add_column("id", style="dim")
                for i, (path, fid) in enumerate(candidates, 1):
                    table.add_row(str(i), path, fid)
                console.print(table)
                console.print(
                    f"  [{len(candidates) + 1}] create new folder {query!r} instead"
                )
                choice = IntPrompt.ask("pick one", default=len(candidates) + 1)
                if 1 <= choice <= len(candidates):
                    return candidates[choice - 1][1]
                # else fall through to create below
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


def find_env_file(explicit: Optional[str]) -> Optional[Path]:
    """Search order: an explicitly-passed --env-file, ./.env in the current
    directory, then a fixed per-user config location - so `uv tool install`
    / pipx installs (which run outside any project directory) still pick up
    saved credentials no matter where you invoke `filester` from."""
    candidates = []
    if explicit and explicit != ".env":
        candidates.append(Path(explicit).expanduser())
    else:
        candidates.append(Path(".env"))
        candidates.append(DEFAULT_CONFIG_DIR / ".env")
    for c in candidates:
        if c.exists():
            return c
    return None


def cmd_config(client: FilesterClient, args):
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    env_path = DEFAULT_CONFIG_DIR / ".env"

    if args.show:
        if env_path.exists():
            console.print(f"[bold]{env_path}[/]")
            for line in env_path.read_text().splitlines():
                if line.strip().startswith("FILESTER_API_KEY"):
                    key, _, val = line.partition("=")
                    console.print(f"{key}={val[:4]}{'*' * max(len(val) - 4, 0)}")
                else:
                    console.print(line)
        else:
            console.print(f"[dim]no config saved yet at {env_path}[/]")
        return

    api_key = args.config_api_key or console.input("Filester API key: ").strip()
    if not api_key:
        raise FilesterError("no API key given")
    base_url = args.config_base_url or DEFAULT_BASE_URL

    env_path.write_text(f"FILESTER_API_KEY={api_key}\nFILESTER_BASE_URL={base_url}\n")
    env_path.chmod(0o600)
    console.print(
        f"[green]saved[/] to {env_path} - `filester` will pick this up automatically from now on"
    )


def cmd_account(client: FilesterClient, args):
    data = client.account()
    used, limit = data.get("storage_used", 0), data.get("storage_limit", 0)
    pct = (used / limit * 100) if limit else 0

    table = Table(title="Filester account", show_header=False, box=None)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("user", str(data.get("username")))
    table.add_row("storage", f"{human_size(used)} / {human_size(limit)}  ({pct:.1f}%)")
    table.add_row("files", str(data.get("files_count")))
    table.add_row("folders", str(data.get("folders_count")))
    table.add_row("api requests today", str(data.get("api_requests_today")))
    console.print(table)

    if pct >= 80:
        console.print(
            "[bold yellow]![/] storage is over 80% full - the free tier caps at 10GB total,"
            " uploads will start failing once it's full."
        )


def cmd_folders(client: FilesterClient, args):
    resolver = FolderResolver(client)
    table = Table(title="Folders")
    table.add_column("id", style="dim")
    table.add_column("path", style="bold")
    rows = 0
    for path, fid in sorted(resolver.all_paths()):
        if args.search and args.search.lower() not in path.lower():
            continue
        table.add_row(fid, path)
        rows += 1
    console.print(table)
    if rows == 0:
        console.print("[dim]no matching folders[/]")


def cmd_mkdir(client: FilesterClient, args):
    resolver = FolderResolver(client)
    folder_id = resolver.resolve(
        args.path, create=True, public=0 if args.private else 1
    )
    console.print(f"[green]ready[/]: {args.path} -> [bold]{folder_id}[/]")


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

    table = Table(title="Files")
    table.add_column("id", style="dim")
    table.add_column("size", justify="right")
    table.add_column("name", style="bold")
    table.add_column("url", style="cyan")
    for f in result["data"]:
        table.add_row(
            f.get("uuid", f.get("id")), human_size(f["size"]), f["name"], f["url"]
        )
    console.print(table)

    pg = result.get("pagination", {})
    if pg:
        console.print(
            f"[dim]page {pg.get('page')}/{pg.get('pages')} ({pg.get('total')} files total)[/]"
        )


def _upload_one(
    client: FilesterClient,
    resolver: FolderResolver,
    path: Path,
    folder_path: Optional[str],
    folder_id: Optional[str],
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
    console.print(f"[bold green]OK[/]  {path.name} -> [cyan]{result['url']}[/]")
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
    console.print(f"[dim]uploaded {count} file(s) from {target}[/]")


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
                        continue

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


def cmd_upgrade(client: FilesterClient, args):
    import shutil
    import subprocess

    repo_url = "git+https://github.com/patelharsh9797/filester-cli"

    if shutil.which("uv"):
        cmd = ["uv", "tool", "upgrade", "filester-cli"]
    elif shutil.which("pipx"):
        cmd = ["pipx", "upgrade", "filester-cli"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", repo_url]

    console.print(f"[dim]$ {' '.join(cmd)}[/]")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        raise FilesterError(
            f"upgrade command failed (exit {result.returncode}) - try manually: {' '.join(cmd)}"
        )
    console.print(
        "[green]done[/] - run `filester --version` to confirm the new version"
    )


# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="filester",
        description="A CLI for the Filester storage API",
        formatter_class=RichHelpFormatter,
    )
    p.add_argument(
        "--version", "-V", action="version", version=f"filester-cli {__version__}"
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
    sub.add_parser("upgrade", help="upgrade filester-cli to the latest version")

    sp = sub.add_parser(
        "config",
        help="save your API key to a persistent config file (~/.config/filester/.env)",
    )

    sp.add_argument(
        "--api-key",
        dest="config_api_key",
        default=None,
        help="skip the prompt and set it directly",
    )
    sp.add_argument("--base-url", dest="config_base_url", default=None)
    sp.add_argument(
        "--show",
        action="store_true",
        help="show the currently saved config instead of setting it",
    )

    sp = sub.add_parser("folders", help="list all folders")
    sp.add_argument(
        "--search",
        "-s",
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
    sp.add_argument(
        "--folder-path",
        "-d",
        default=None,
        help='e.g. "Streamers/Alice", or just a partial name',
    )
    sp.add_argument("--page", type=int, default=1)
    sp.add_argument("--per-page", type=int, default=20)
    sp.add_argument("--search", "-s", default=None)

    sp = sub.add_parser(
        "upload", help="upload a single file, or every matching file in a directory"
    )
    sp.add_argument("path")
    sp.add_argument(
        "--folder-id", default=None, help="upload into this existing folder id"
    )
    sp.add_argument(
        "--folder-path",
        "-d",
        default=None,
        help='remote destination, e.g. "alice" (fuzzy-matched) or "Streamers/Alice"',
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
        "-D",
        action="store_true",
        help="delete local file(s) once uploaded successfully",
    )
    sp.add_argument("--no-progress", "-np", action="store_true")

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
        "-d",
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
        help="how many subfolder levels to mirror (default: 1)",
    )
    sp.add_argument("--ext", "-e", nargs="*", default=None)
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
        "-D",
        action="store_true",
        help="delete local file once uploaded",
    )
    sp.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    sp.add_argument("--no-progress", "-np", action="store_true")

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(
                console=console, show_path=False, markup=True, rich_tracebacks=True
            )
        ],
    )

    env_path = find_env_file(args.env_file)
    if env_path:
        if load_dotenv:
            load_dotenv(env_path, override=False)
            log.debug("loaded env vars from %s", env_path)
        else:
            log.warning("%s exists but python-dotenv isn't installed", env_path)

    client = FilesterClient(
        api_key=args.api_key, base_url=args.base_url, max_retries=args.max_retries
    )

    if not client.api_key and args.command not in ("config", "upgrade"):
        log.warning(
            "no API key set - run `filester config` to save one, or set FILESTER_API_KEY"
        )

    handlers = {
        "account": cmd_account,
        "config": cmd_config,
        "folders": cmd_folders,
        "mkdir": cmd_mkdir,
        "files": cmd_files,
        "upload": cmd_upload,
        "watch": cmd_watch,
        "upgrade": cmd_upgrade,
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
