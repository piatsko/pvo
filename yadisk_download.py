#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["requests>=2.32"]
# ///
"""Download files and folders from Yandex Disk through its REST API.

    uv run yadisk_download.py auth                      # save an OAuth token
    uv run yadisk_download.py whoami                     # check the token
    uv run yadisk_download.py ls disk:/Photos            # list a folder
    uv run yadisk_download.py get disk:/Photos -o ./out  # download recursively
"""

from __future__ import annotations

import argparse
import fnmatch
import getpass
import hashlib
import os
import sys
import tempfile
import time
import webbrowser
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_ROOT = "https://cloud-api.yandex.net/v1/disk"
OAUTH_AUTHORIZE = "https://oauth.yandex.ru/authorize"
APP_NAME = "yadisk-backup"
CHUNK = 8 * 1024 * 1024
LIST_PAGE = 200

_ITEM_FIELDS = ("type", "name", "path", "size", "md5", "sha256", "modified", "mime_type")
LIST_FIELDS = ",".join(
    [*_ITEM_FIELDS, "_embedded.total", *(f"_embedded.items.{f}" for f in _ITEM_FIELDS)]
)


class YandexDiskError(RuntimeError):
    pass


class IntegrityError(RuntimeError):
    pass


# --------------------------------------------------------------------------- token


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".config") / APP_NAME


def default_token_file() -> Path:
    return config_dir() / "token.txt"


def read_token(path: Path | None = None) -> str:
    env = os.environ.get("YANDEX_DISK_TOKEN", "").strip()
    if env:
        return env
    token_file = (path or default_token_file()).expanduser()
    try:
        token = token_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise YandexDiskError(
            f"No token found at {token_file}. Run 'auth' or set YANDEX_DISK_TOKEN."
        ) from exc
    if not token:
        raise YandexDiskError(f"Token file is empty: {token_file}")
    return token


def save_token(path: Path, token: str) -> None:
    """Write the token with owner-only permissions, atomically."""
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as tmp:
            tmp_name = tmp.name
            os.chmod(tmp_name, 0o600)
            tmp.write(token + "\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
    finally:
        if tmp_name:
            Path(tmp_name).unlink(missing_ok=True)


# --------------------------------------------------------------------------- client


@dataclass(slots=True)
class RemoteFile:
    path: str
    size: int
    sha256: str | None = None
    md5: str | None = None
    modified: str | None = None


def _session(auth_token: str | None = None) -> requests.Session:
    retry = Retry(
        total=6,
        connect=6,
        read=6,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    if auth_token:
        session.headers["Authorization"] = f"OAuth {auth_token}"
    return session


class YandexDisk:
    def __init__(
        self,
        token: str,
        timeout: tuple[float, float] = (15.0, 90.0),
        max_reconnects: int = 8,
    ) -> None:
        self.api = _session(token)
        self.content = _session()  # download hrefs are pre-signed; send no token there
        self.timeout = timeout
        self.max_reconnects = max_reconnects

    def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self.api.get(f"{API_ROOT}/{endpoint}", params=params, timeout=self.timeout)
        if not response.ok:
            raise YandexDiskError(
                f"GET /{endpoint} failed ({response.status_code}): {response.text[:300]}"
            )
        return response.json()

    def disk_info(self) -> dict[str, Any]:
        return self._get("", {"fields": "user.login,user.display_name,total_space,used_space,trash_size"})

    def stat(self, path: str) -> dict[str, Any]:
        return self._get("resources", {"path": path, "fields": LIST_FIELDS, "limit": 0})

    def list_dir(self, path: str) -> Iterator[dict[str, Any]]:
        """Yield the direct children of a folder, paging through the listing."""
        offset = 0
        while True:
            payload = self._get(
                "resources",
                {"path": path, "fields": LIST_FIELDS, "limit": LIST_PAGE, "offset": offset},
            )
            if payload.get("type") == "file":
                yield payload
                return
            items = payload.get("_embedded", {}).get("items", [])
            yield from items
            offset += len(items)
            if len(items) < LIST_PAGE:
                return

    def walk_files(self, path: str) -> Iterator[RemoteFile]:
        """Yield every file under path, depth-first, directories in listing order."""
        stack = [path]
        while stack:
            current = stack.pop()
            dirs: list[str] = []
            for item in self.list_dir(current):
                if item.get("type") == "dir":
                    dirs.append(item["path"])
                else:
                    yield RemoteFile(
                        path=item["path"],
                        size=int(item.get("size") or 0),
                        sha256=item.get("sha256"),
                        md5=item.get("md5"),
                        modified=item.get("modified"),
                    )
            stack.extend(reversed(dirs))

    def download_url(self, path: str) -> str:
        href = self._get("resources/download", {"path": path}).get("href")
        if not href:
            raise YandexDiskError(f"No download URL returned for {path}")
        return str(href)

    def download(self, remote: RemoteFile, target: Path) -> tuple[str, int]:
        """Download remote to target, resuming a partial file.

        A dropped or stalled connection is reconnected from the last byte on
        disk rather than failing the file. Returns the action taken and the
        number of bytes pulled over the network.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.stat().st_size if target.exists() else 0

        if existing == remote.size and _verify(target, remote):
            return "skipped", 0
        if existing > remote.size:
            target.unlink()
            existing = 0

        progress = Progress(remote.size, existing)
        stalls = 0
        while True:
            position = target.stat().st_size if target.exists() else 0
            if position >= remote.size:
                break
            try:
                self._fetch_from(remote, target, position, progress)
            except (requests.RequestException, OSError) as exc:
                stalls += 1
                if stalls > self.max_reconnects:
                    progress.finish()
                    raise YandexDiskError(
                        f"Transfer failed for {remote.path} after {stalls} attempts: {exc}"
                    ) from exc
                pause = min(2**stalls, 30)
                progress.note(f"{type(exc).__name__}, reconnecting in {pause}s")
                time.sleep(pause)
        progress.finish()

        actual = target.stat().st_size
        if actual != remote.size:
            raise IntegrityError(
                f"Size mismatch for {remote.path}: expected {remote.size}, got {actual}"
            )
        if remote.size > 256 * 1024 * 1024:
            print(f"    verifying {human(remote.size)}...", file=sys.stderr, flush=True)
        if not _verify(target, remote):
            target.unlink(missing_ok=True)
            raise IntegrityError(f"Checksum mismatch for {remote.path}")
        return ("resumed" if existing else "downloaded"), remote.size - existing

    def _fetch_from(
        self, remote: RemoteFile, target: Path, position: int, progress: Progress
    ) -> None:
        """Stream one attempt, appending from position. The href is re-signed each try."""
        headers = {"Range": f"bytes={position}-"} if position else {}
        response = self.content.get(
            self.download_url(remote.path), headers=headers, stream=True, timeout=self.timeout
        )
        with response:
            if response.status_code == 416 and position == remote.size:
                return
            if not response.ok:
                # Never echo the signed href into an error message.
                raise YandexDiskError(
                    f"Download failed for {remote.path} ({response.status_code})"
                )
            appending = position > 0 and response.status_code == 206
            if not appending:
                position = 0  # the server ignored Range, so start the file over
            progress.restart(position)
            with target.open("ab" if appending else "wb") as out:
                for chunk in response.iter_content(chunk_size=CHUNK):
                    if not chunk:
                        continue
                    out.write(chunk)
                    position += len(chunk)
                    progress.update(position)


def _verify(target: Path, remote: RemoteFile) -> bool:
    """Compare the local file against whichever checksum Yandex reported."""
    algo, expected = ("sha256", remote.sha256) if remote.sha256 else ("md5", remote.md5)
    if not expected:
        return target.stat().st_size == remote.size
    digest = hashlib.new(algo)
    with target.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest().lower() == expected.lower()


# --------------------------------------------------------------------------- paths


def normalize_remote(path: str) -> str:
    path = path.strip()
    if not path.startswith(("disk:", "trash:", "app:")):
        path = "disk:/" + path.lstrip("/")
    scheme, _, rest = path.partition(":")
    rest = rest.rstrip("/")
    return f"{scheme}:{rest}" if rest else f"{scheme}:/"


def local_target(root: str, remote_path: str, dest: Path, *, flat: bool) -> Path:
    """Map a remote path to a local one, keeping the folder named by root."""
    if flat:
        return dest / remote_path.rsplit("/", 1)[-1]
    root = normalize_remote(root)
    prefix = "disk:" if root in ("disk:", "disk:/") else root.rsplit("/", 1)[0]
    relative = remote_path[len(prefix) :].lstrip("/")
    target = (dest / relative).resolve()
    if not target.is_relative_to(dest.resolve()):
        raise YandexDiskError(f"Refusing to write outside {dest}: {remote_path}")
    return target


def matches(path: str, includes: list[str], excludes: list[str]) -> bool:
    relative = path.split(":", 1)[-1].lstrip("/")
    name = relative.rsplit("/", 1)[-1]
    if includes and not any(
        fnmatch.fnmatch(relative, p) or fnmatch.fnmatch(name, p) for p in includes
    ):
        return False
    return not any(
        fnmatch.fnmatch(relative, p) or fnmatch.fnmatch(name, p) for p in excludes
    )


# --------------------------------------------------------------------------- output


def human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds >= 3600:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds}s"


class Progress:
    """Redraw in place on a terminal, print a line every `interval` seconds otherwise.

    Yandex throttles large downloads to a few MiB/s, so a multi-GiB file can run
    for half an hour; without periodic output that is indistinguishable from a hang.
    """

    def __init__(self, total: int, done: int = 0, interval: float = 15.0) -> None:
        self.total = total
        self.done = self.base = done
        self.interval = interval
        self.tty = sys.stderr.isatty()
        self.started = time.monotonic()
        self.last_line = 0.0

    def restart(self, position: int) -> None:
        """A new connection took over from position; reset the rate window."""
        self.done = self.base = position
        self.started = time.monotonic()

    def update(self, position: int) -> None:
        self.done = position
        now = time.monotonic()
        if self.tty:
            self._draw("\r")
        elif now - self.last_line >= self.interval:
            self._draw("\n")
        else:
            return
        self.last_line = now

    def note(self, message: str) -> None:
        self.finish()
        print(f"    {message}", file=sys.stderr, flush=True)

    def finish(self) -> None:
        if self.tty:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()

    def _draw(self, end: str) -> None:
        elapsed = max(time.monotonic() - self.started, 1e-6)
        rate = (self.done - self.base) / elapsed
        pct = self.done * 100 / self.total if self.total else 100.0
        eta = duration((self.total - self.done) / rate) if rate > 0 else "?"
        line = (
            f"    {human(self.done)} / {human(self.total)} ({pct:5.1f}%)"
            f"  {human(rate)}/s  ETA {eta}"
        )
        sys.stderr.write(line + ("\033[K" if self.tty else "") + end)
        sys.stderr.flush()


# --------------------------------------------------------------------------- commands


def _read_secret(prompt: str) -> str:
    """Read a secret without echo when possible, else fall back to plain stdin."""
    if sys.stdin.isatty():
        try:
            return getpass.getpass(prompt).strip()
        except (EOFError, OSError):
            pass
    print(prompt, end="", file=sys.stderr, flush=True)
    line = sys.stdin.readline()
    if not line:
        raise YandexDiskError(
            "No token on stdin. Run this in a terminal, or pipe the token:\n"
            "  printf %s '<token>' | uv run yadisk_download.py auth --client-id <ClientID>"
        )
    print(file=sys.stderr)
    return line.strip()


def cmd_auth(args: argparse.Namespace) -> int:
    client_id = args.client_id or os.environ.get("YANDEX_CLIENT_ID", "").strip()
    if not client_id:
        print(
            "An OAuth client ID is required. One-time setup:\n"
            "  1. Open https://oauth.yandex.com/client/new/\n"
            "  2. Platform: 'Web services', redirect URI: "
            "https://oauth.yandex.ru/verification_code\n"
            "  3. Data access: add 'cloud_api:disk.read' (and 'cloud_api:disk.info')\n"
            "  4. Create the app, copy its ClientID, then rerun:\n"
            "       uv run yadisk_download.py auth --client-id <ClientID>",
            file=sys.stderr,
        )
        return 2

    url = f"{OAUTH_AUTHORIZE}?" + urlencode({"response_type": "token", "client_id": client_id})
    print("Open this URL and approve access to Yandex Disk:\n")
    print(f"  {url}\n")
    print("Then copy the access_token value out of the page or the redirected URL.")
    sys.stdout.flush()
    if args.url_only:
        return 0
    if not args.no_browser and sys.stdin.isatty():
        webbrowser.open(url)
    token = _read_secret("Access token: ")
    if not token:
        raise YandexDiskError("No token entered")
    save_token(args.token_file, token)
    print(f"Saved token to {args.token_file.expanduser()} (mode 600)")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    info = YandexDisk(read_token(args.token_file)).disk_info()
    user = info.get("user", {})
    total, used = int(info.get("total_space", 0)), int(info.get("used_space", 0))
    print(f"user:  {user.get('display_name') or user.get('login', '?')}")
    print(f"usage: {human(used)} of {human(total)} ({human(total - used)} free)")
    print(f"trash: {human(int(info.get('trash_size', 0)))}")
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    disk = YandexDisk(read_token(args.token_file))
    path = normalize_remote(args.path)
    files = dirs = 0
    total = 0
    for item in disk.list_dir(path):
        if item.get("type") == "dir":
            dirs += 1
            print(f"{'<dir>':>12}  {item['path']}/")
        else:
            size = int(item.get("size") or 0)
            files += 1
            total += size
            print(f"{human(size):>12}  {item['path']}")
    print(f"\n{dirs} folders, {files} files, {human(total)}", file=sys.stderr)
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    disk = YandexDisk(read_token(args.token_file))
    dest: Path = args.output.expanduser()
    dest.mkdir(parents=True, exist_ok=True)

    counts = {"downloaded": 0, "resumed": 0, "skipped": 0, "failed": 0}
    bytes_fetched = 0
    selected = 0

    for raw_root in args.paths:
        root = normalize_remote(raw_root)
        meta = disk.stat(root)
        sources: Iterator[RemoteFile]
        if meta.get("type") == "file":
            sources = iter(
                [
                    RemoteFile(
                        path=meta["path"],
                        size=int(meta.get("size") or 0),
                        sha256=meta.get("sha256"),
                        md5=meta.get("md5"),
                        modified=meta.get("modified"),
                    )
                ]
            )
        else:
            sources = disk.walk_files(root)

        for remote in sources:
            if not matches(remote.path, args.include, args.exclude):
                continue
            if args.max_files is not None and selected >= args.max_files:
                break
            selected += 1
            target = local_target(root, remote.path, dest, flat=args.flat)

            if args.dry_run:
                print(f"would fetch  {human(remote.size):>10}  {remote.path} -> {target}", flush=True)
                continue

            print(f"{remote.path}  ({human(remote.size)})", flush=True)
            try:
                action, transferred = disk.download(remote, target)
            except (YandexDiskError, IntegrityError, OSError, requests.RequestException) as exc:
                counts["failed"] += 1
                print(f"  FAILED: {exc}", file=sys.stderr)
                if args.stop_on_error:
                    break
                continue
            counts[action] += 1
            bytes_fetched += transferred
            print(f"  {action} -> {target}", flush=True)

    if args.dry_run:
        print(f"\n{selected} files would be fetched")
        return 0
    print(
        f"\n{counts['downloaded']} downloaded, {counts['resumed']} resumed, "
        f"{counts['skipped']} already present, {counts['failed']} failed "
        f"({human(bytes_fetched)} transferred)"
    )
    return 1 if counts["failed"] else 0


# --------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yadisk_download.py",
        description="Download files from Yandex Disk via the REST API.",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=default_token_file(),
        help="where the OAuth token lives (default: %(default)s)",
    )
    # Accept --token-file after the subcommand too; SUPPRESS keeps the global default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--token-file", type=Path, default=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command", required=True)

    auth = sub.add_parser("auth", parents=[common], help="obtain and store an OAuth token")
    auth.add_argument("--client-id", help="OAuth app ClientID (or set YANDEX_CLIENT_ID)")
    auth.add_argument("--no-browser", action="store_true", help="do not open a browser")
    auth.add_argument(
        "--url-only", action="store_true", help="print the authorization URL and exit"
    )
    auth.set_defaults(func=cmd_auth)

    whoami = sub.add_parser("whoami", parents=[common], help="show the account and disk usage")
    whoami.set_defaults(func=cmd_whoami)

    ls = sub.add_parser("ls", parents=[common], help="list a folder")
    ls.add_argument("path", nargs="?", default="disk:/")
    ls.set_defaults(func=cmd_ls)

    get = sub.add_parser("get", parents=[common], help="download files or folders recursively")
    get.add_argument("paths", nargs="+", metavar="REMOTE", help="e.g. disk:/Photos or /Photos/a.jpg")
    get.add_argument("-o", "--output", type=Path, default=Path("./yadisk"), help="local destination")
    get.add_argument("--include", action="append", default=[], metavar="GLOB")
    get.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    get.add_argument("--flat", action="store_true", help="drop folder structure")
    get.add_argument("--max-files", type=int, help="stop after N matching files")
    get.add_argument("--dry-run", action="store_true")
    get.add_argument("--stop-on-error", action="store_true")
    get.set_defaults(func=cmd_get)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (YandexDiskError, IntegrityError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"network error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted (rerun to resume)", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
