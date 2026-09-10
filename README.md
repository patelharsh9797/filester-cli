# filester-cli

A CLI + watch-daemon for the [Filester](https://filester.me) storage API.
Originally built to push `ctbrec` stream recordings straight from a VPS to
Filester without ever downloading them locally first — but works for any
directory of files you want auto-uploaded.

```
$ filester account
  Filester account
  user                 harsh
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
# with uv (recommended)
uv tool install git+https://github.com/patelharsh9797/filester-cli

# or with pipx
pipx install git+https://github.com/patelharsh9797/filester-cli

# or from a local clone, for development
git clone https://github.com/patelharsh9797/filester-cli
cd filester-cli
uv pip install -e .        # or: pip install -e .
```

`filester` lands on your `$PATH` as a single command — no manual venv
activation, no `python3 filester_cli.py`.

## Configure

```bash
filester config --api-key YOUR_API_KEY
```

This saves your key to `~/.config/filester/.env` (mode `600`). `filester`
auto-loads it from there on every run, from any directory — no `cd`, no
`source .env`, no per-command flags needed.

```bash
filester config --show     # see what's saved (key is masked)
```

Other ways to set it, if you prefer:
- a `./.env` file in the current directory (checked first, before the
  saved config) — see `.env.example`
- environment variables: `FILESTER_API_KEY`, `FILESTER_BASE_URL`,
  `FILESTER_MAX_RETRIES`
- `--api-key` / `--base-url` flags on any command

## Upgrading

```bash
filester upgrade
```

Detects however you installed it (`uv tool`, `pipx`, or plain `pip`) and
runs the right upgrade command for you — no need to remember
`uv tool install --force ...` or similar.

## Commands

```bash
filester account                                    # storage usage
filester config [--api-key KEY] [--show]              # save/view credentials
filester upgrade                                     # update to the latest version
filester folders [--search TERM]                     # list / grep folders
filester mkdir "Streamers/Alice"                      # creates both levels if missing
filester files [--folder-path X] [--search TERM]      # list files
filester upload <file-or-dir> [--folder-path X]        # upload
filester watch --dir <path> [--folder-path X]          # auto-upload daemon
```

Run `filester <command> --help` for full flag lists.

### Shell autocomplete

Tab-completion for subcommands (`account`, `upload`, `folders`, ...) and
flags (`--folder-path`, `--search`, ...) via [argcomplete](https://github.com/kislyuk/argcomplete):

```bash
# bash - add to ~/.bashrc, then restart your shell / `source ~/.bashrc`
eval "$(register-python-argcomplete filester)"

# zsh - add to ~/.zshrc
autoload -U bashcompinit && bashcompinit
eval "$(register-python-argcomplete filester)"
```

`register-python-argcomplete` ships with the `argcomplete` package, which
is installed automatically as a dependency - nothing extra to install.

### Short flags

| Flag | Short | Commands |
|---|---|---|
| `--version` | `-V` | global |
| `--search` | `-s` | `folders`, `files` |
| `--folder-path` | `-d` | `files`, `watch` |
| `--delete-after` | `-D` | `upload`, `watch` |
| `--no-progress` | `-np` | `upload`, `watch` |
| `--ext` | `-e` | `watch` |

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

## Versioning & releasing

There's a single source of truth: the `version` field in `pyproject.toml`.
`filester --version` / `-V` reads it dynamically from the installed
package's metadata (`importlib.metadata`), so there's nothing else to bump
in code — no hardcoded version string sitting in a second file to forget
about.

To cut a new release:

```bash
# 1. bump the version
#    edit pyproject.toml -> version = "0.3.0"   (follow semver: MAJOR.MINOR.PATCH)

# 2. commit and tag it
git add pyproject.toml
git commit -m "release: v0.3.0"
git tag v0.3.0
git push && git push --tags

# 3. users update with:
filester upgrade
```

Semver guide for picking the version bump:
- **PATCH** (0.2.0 -> 0.2.1): bug fixes, no new flags/behavior
- **MINOR** (0.2.0 -> 0.3.0): new commands/flags, backwards compatible
- **MAJOR** (0.2.0 -> 1.0.0): breaking changes (renamed/removed flags, changed defaults)

Since `uv tool install`/`pipx install` from a git URL always pulls whatever
is on the default branch, `filester upgrade` will pick up the latest commit
on `main` even without a tag — tags are mainly for having a readable
changelog and a point to roll back to, not a strict requirement for
upgrades to work.

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