# filester-cli

A CLI + watch-daemon for the [Filester](https://filester.me) storage API.
Originally built to push `ctbrec` stream recordings straight from a VPS to
Filester without ever downloading them locally first — but works for any
directory of files you want auto-uploaded.

```
$ filester account
  Filester account
  user                 your_username
  storage              2.1GB / 10.0GB  (21.0%)
  files                14
  folders              3

$ filester upload video.mp4 --folder-path alice
  matched existing folder Streamers/Alice -> a1b2c3
  ⠋ video.mp4  ████████████████████░░░░░░░░  68%  1.2/1.8 GB  4.3 MB/s  0:02:11
  OK  video.mp4 -> https://u1.filester.me/f/xyz
```

## Install

```bash
# with pipx (recommended - isolated, puts `filester` on your PATH)
pipx install git+https://github.com/YOUR_USERNAME/filester-cli

# or with uv
uv tool install git+https://github.com/YOUR_USERNAME/filester-cli

# or from a local clone, for development
git clone https://github.com/YOUR_USERNAME/filester-cli
cd filester-cli
uv pip install -e .        # or: pip install -e .
```

Once installed, `filester` is a single command on your `$PATH` - no more
`python3 filester_cli.py ...`, no manual venv activation needed for pipx/uv
tool installs.

## Configure

```bash
cp .env.example .env
nano .env   # paste your API key from https://filester.me/account
```

`filester` auto-loads `./.env` on every run (override with `--env-file`).
Vars: `FILESTER_API_KEY`, `FILESTER_BASE_URL`, `FILESTER_MAX_RETRIES`.

## Commands

```bash
filester account                                    # storage usage
filester folders [--search TERM]                     # list / grep folders
filester mkdir "Streamers/Alice"                      # creates both levels if missing
filester files [--folder-path X] [--search TERM]      # list files
filester upload <file-or-dir> [--folder-path X]        # upload
filester watch --dir <path> [--folder-path X]          # auto-upload daemon
```

Run `filester <command> --help` for full flag lists.

### `--folder-path` is fuzzy

You don't need the exact path or an id for `upload`/`files`. If what you
type is a case-insensitive substring of exactly one existing folder's full
path, it's used automatically. If it matches more than one, you get a
pick-list. If it matches nothing, the full path you typed is created.

```bash
filester upload clip.mp4 --folder-path alice   # matches "Streamers/Alice"
```

### `watch` - the main event

Watches a directory, waits for a file to stop growing (so it doesn't grab
a stream still recording), uploads it, and optionally deletes the local
copy once confirmed:

```bash
filester watch \
    --dir /recordings \
    --folder-path Streamers \
    --mirror --mirror-depth 1 \
    --delete-after \
    --poll-interval 30 \
    --stable-seconds 60
```

`--mirror --mirror-depth 1` assumes a `recordings/<model>/<file>` layout
(ctbrec's default) and auto-creates/reuses a matching
`Streamers/<model>` folder for each one. Progress and events are logged to
stdout, so under `systemd` this shows up in `journalctl -u filester-watch -f`
and under Docker in `docker logs -f <container>`. Already-uploaded files
are tracked in `~/.filester-cli/state.json` so restarts don't re-upload.

An example systemd unit is in `systemd/filester-watch.service`.

## Roadmap

- [ ] `filester download` - pull files back down (not built yet)
- [ ] parallel uploads for `upload <dir>`

## Important caveats

- **10GB total storage / 10GB max file size** on the free tier - fills up
  fast with continuous recording. `--delete-after` only helps your local
  disk/bandwidth, not Filester's cap.
- **No resumable/chunked upload** in this API version - a failed transfer
  restarts from scratch (with retry/backoff), it can't resume partway.
- Filester's docs note the service has faced ongoing DoS attacks and
  uploads can fail unpredictably - the client retries 5xx/429/network
  errors with exponential backoff, but persistent outages are out of its
  control.
- **Files are auto-deleted after 45 days of no views/downloads** - this is
  a bandwidth-saving relay, not permanent storage.

## License

MIT
