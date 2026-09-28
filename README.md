# jellyfin-curator

Config-driven playlist and collection automation for Jellyfin, loosely in the
spirit of Kometa. Runs once from the CLI or as a long-lived Docker container.

## Rule types

| type | selects | typical use |
|---|---|---|
| `media_filter` | episodes/movies by audio/subtitle streams | missing subs, missing Japanese audio |
| `continue_watching` | next unwatched episode of each started-but-unfinished season | per-library "continue watching" |
| `new_seasons` | first episode of an untouched season after a finished one | "new seasons of shows I watch" |

Every rule targets a `playlist` (default: ordered, owned by the configured user)
or a `collection` (global, unordered; episodes are rolled up to their series).

Sync is diff-based: existing playlists are edited in place, never recreated.
The tool only touches playlists/collections whose names match a rule.

## Setup

```sh
cp .env.example .env                              # set JELLYFIN_URL + JELLYFIN_API_KEY
cp config/config.example.yaml config/config.yaml  # edit rules
uv sync
uv run jfcurator check              # connectivity, user, library names
uv run jfcurator languages Anime    # how tracks are actually tagged
uv run jfcurator run --dry-run      # preview changes
uv run jfcurator run
```

`.env` and `config/config.yaml` are git-ignored.

## Docker / Unraid

```sh
docker compose up -d --build
```

The container runs `jfcurator daemon`, syncing every `JFCURATOR_INTERVAL` seconds
(default 3600) and re-reading the config each cycle. It runs as `99:100`
(nobody:users). Mount your config directory at `/config`.

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format .
```
