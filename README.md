# jellyfin-curator

Config-driven playlist and collection automation for Jellyfin, loosely in the
spirit of Kometa. Runs once from the CLI or as a long-lived Docker container.

## Rule types

| type | selects | typical use |
|---|---|---|
| `media_filter` | episodes/movies by audio/subtitle streams | missing subs, missing Japanese audio |
| `continue_watching` | next unwatched episode of each started-but-unfinished season | per-library "continue watching" |
| `new_seasons` | first episode of an untouched season after a finished one | "new seasons of shows I watch" |
| `anilist_list` | series/movies on a public AniList user's lists, by entry status | Planning / Watching / Completed |
| `anilist_top_rated` | AniList's top `count` anime by score that you have | "Top Rated" |
| `binge` | shows that are started with unwatched episodes left (`series_status: [ended]` to narrow) | "Binge", "Binge Ended" |
| `not_started` | shows with nothing played | "Not Started" |
| `all_items` | every series and movie, usually with `exclude` | "No Collection" |

Every rule targets a `playlist` (default: ordered, owned by the configured user)
or a `collection` (global, unordered; episodes are rolled up to their series).
The last five types are collection-only.

## Collections

Jellyfin collections are global rather than per library, so collections that
draw from several libraries go in the top-level `collections:` section with a
`libraries:` list. Each name is one collection, and rule names must be unique.

- `exclude: [Other Rule, ...]` drops anything those rules selected, compared by
  series/movie. Rules are evaluated in dependency order. If an excluded rule
  fails, the rule that excludes it is skipped as well.
- `sort_title` / `summary` set the collection's sort title and overview, which
  controls its position in the Collections view.
- Collections have no stored item order in Jellyfin. `sort` therefore only
  decides which items survive a `limit`.

AniList ids are matched to Jellyfin items directly on the `AniList` provider id
(set by the AniList metadata plugin on series). Otherwise they are matched
through the [Kometa Anime-IDs](https://github.com/Kometa-Team/Anime-IDs)
mapping, the same source Kometa uses: AniDB and TVDB ids for shows, AniDB and
IMDb ids for movies. The mapping is cached for 24h. A later-season AniList entry
maps to its whole show, as in Kometa. Specials/OVAs (TVDB season 0) don't, so an
OVA on your Planning list won't pull in the parent series.

Sync is diff-based: existing playlists are edited in place, never recreated.
The tool only touches playlists/collections whose names match a rule.

`media_filter` rules accept per-series `overrides`, e.g. checking Chinese
rather than Japanese audio for donghua in an anime library. An override's
`audio`/`subtitles` replace the rule's; whichever it omits is inherited.
See `config/config.example.yaml`.

## Setup

```sh
cp .env.example .env                              # set JELLYFIN_URL + JELLYFIN_API_KEY
cp config/config.example.yaml config/config.yaml  # edit rules
uv sync
uv run jfcurator check              # connectivity, user, library names
uv run jfcurator languages "Anime Shows"   # how tracks are actually tagged
uv run jfcurator run --dry-run      # preview changes
uv run jfcurator run
```

`.env` and `config/config.yaml` are git-ignored.

## Run logs

Each run writes two files to `logs/`, next to the config file (`/config/logs`
in Docker):

- `run-<timestamp>.md` is a summary: one row per rule, then the names of
  everything each playlist/collection gained or lost, plus any errors.
- `run-<timestamp>.log` is the debug log: everything, including each HTTP
  request and the full traceback of any failure. The console only shows INFO.

Dry runs get a `-dryrun` suffix. The last `keep_runs` (default 100) runs are
kept. Set `options.log_dir: null` to turn both off.

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
