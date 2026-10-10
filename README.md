# yadisk_download.py

A single-file downloader for Yandex Disk, run with `uv` (dependencies are declared inline
via PEP 723, so there is nothing to install).

## Does Yandex Disk have an API?

Yes — a REST API at `https://cloud-api.yandex.net/v1/disk`, documented at
<https://yandex.com/dev/disk-api/doc/en/>. Auth is OAuth 2.0: every request carries
`Authorization: OAuth <token>`. (There is also a WebDAV endpoint, `webdav.yandex.ru`,
which takes an app password instead — fine for `rclone`, but the REST API is what gives
you checksums, pagination and signed download URLs.)

## One-time setup

1. Register an OAuth app at <https://oauth.yandex.com/client/new/>:
   - platform **Web services**, redirect URI `https://oauth.yandex.ru/verification_code`
   - data access: `cloud_api:disk.read` (add `cloud_api:disk.info` for `whoami`)
   - copy the **ClientID**
2. Get a token (implicit grant — the token comes back in the browser, you paste it in):

   ```bash
   uv run yadisk_download.py auth --client-id <ClientID>
   ```

   It is stored mode-600 at `~/.config/yadisk-backup/token.txt`. `YANDEX_DISK_TOKEN` in the
   environment overrides the file; `--token-file` points elsewhere.

   Outside a real terminal (Claude Code's `!` prompt, CI) there is no hidden input, so get
   the URL and pipe the token in:

   ```bash
   uv run yadisk_download.py auth --client-id <ClientID> --url-only
   printf %s '<token>' | uv run yadisk_download.py auth --client-id <ClientID>
   ```

## Usage

```bash
uv run yadisk_download.py whoami                 # verify the token, show disk usage
uv run yadisk_download.py ls disk:/Photos        # list one folder
uv run yadisk_download.py get disk:/Photos -o ./out          # recursive download
uv run yadisk_download.py get /notes.md -o ./out             # a single file
uv run yadisk_download.py get disk:/ -o ./out --dry-run      # plan the whole disk
uv run yadisk_download.py get disk:/ -o ./out \
    --exclude '*.tmp' --exclude 'Cache/*' --max-files 20
```

Paths may be written `disk:/Photos` or `/Photos`. `disk:/Photos` lands in `out/Photos/...`;
`--flat` drops the structure.

`--timeout SECONDS` caps how long any single file may take. A file that runs over is
abandoned and the run moves on to the next one; its partial download is kept, so a later
run picks it up where it stopped. There is no cap by default.

```bash
uv run yadisk_download.py get disk:/Загрузки -o ./out --timeout 120
```

```text
disk:/Studies.zip  (1.6 GiB)
  timed out after 2m00s at 892.0 MiB of 1.6 GiB - partial file kept, rerun to resume

3 downloaded, 1 resumed, 0 already present, 1 timed out, 0 failed (2.4 GiB transferred)

hit the --timeout budget, rerun to resume where each left off:
  disk:/Studies.zip  (54.4% of 1.6 GiB)
```

Timed-out files are not counted as failures, so the exit code stays 0; only real errors
(a checksum mismatch, an unreachable host) make it 1. Separately, a connection that simply
goes quiet for 90 seconds is reconnected from the last byte on disk without any of this
coming into play.

Progress is reported only once a transfer has run for 10 seconds, so small files download
silently. On a terminal the line redraws in place; piped or in CI it prints every 15 seconds.

## Behaviour

- **Incremental:** a file already present with the right size and checksum is skipped.
- **Resumable:** a partial file continues with an HTTP `Range` request; `Ctrl-C` then rerun.
- **Verified:** SHA-256 (or MD5, whichever the API reports) is checked after every download;
  a mismatch deletes the file and reports the failure.
- **Retried:** 429/5xx and connection errors retry with backoff, honouring `Retry-After`.
- One failing file does not abort the run unless `--stop-on-error` is passed; the exit code
  is 1 if anything failed.
- The token is sent only to `cloud-api.yandex.net`; download URLs are pre-signed and fetched
  without it, and are never printed in error messages.

Never commit the token file.
