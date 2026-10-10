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
