"""Config file loading, env substitution, and validation."""

from __future__ import annotations

import os
import re
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class Strict(BaseModel):
    """Base model that rejects unknown keys so typos surface as errors."""

    model_config = ConfigDict(extra="forbid")


class Sort(StrEnum):
    NAME = "name"
    SERIES = "series"
    LAST_PLAYED = "last_played"
    DATE_ADDED = "date_added"
    PREMIERE_DATE = "premiere_date"
    RANDOM = "random"


class SortSpec(Strict):
    """A `field desc` / `field asc` sort expression."""

    key: Sort = Sort.NAME
    descending: bool = False

    @classmethod
    def parse(cls, raw: str | dict | SortSpec) -> SortSpec:
        if isinstance(raw, SortSpec):
            return raw
        if isinstance(raw, dict):
            return cls.model_validate(raw)
        parts = raw.strip().split()
        key = Sort(parts[0])
        descending = len(parts) > 1 and parts[1].lower() in {"desc", "descending"}
        return cls(key=key, descending=descending)


class StreamCriteria(Strict):
    """Constraints on the audio or subtitle streams of an episode/movie.

    All supplied constraints must hold for an item to match.
    """

    present: bool | None = Field(
        default=None,
        description="True requires at least one stream of this type; False requires none.",
    )
    languages: list[str] | None = Field(
        default=None,
        description="Match if at least one stream is in one of these ISO 639-2 codes.",
    )
    missing_language: list[str] | None = Field(
        default=None,
        description="Match only if NO stream is in any of these ISO 639-2 codes.",
    )
    include_external: bool = Field(
        default=True,
        description="Count sidecar (.srt etc.) streams. Set False to require embedded only.",
    )
    undefined_as_expected: bool = Field(
        default=True,
        description="Assume untagged (`und`) tracks are in the languages being checked for.",
    )

    @field_validator("languages", "missing_language")
    @classmethod
    def _lower(cls, v: list[str] | None) -> list[str] | None:
        return [s.strip().lower() for s in v] if v else v

    def is_empty(self) -> bool:
        return self.present is None and not self.languages and not self.missing_language


class MediaMatch(Strict):
    audio: StreamCriteria | None = None
    subtitles: StreamCriteria | None = None

    def overlay(self, other: MediaMatch) -> MediaMatch:
        """`other`'s criteria where set, falling back to ours per stream type."""
        return MediaMatch(
            audio=other.audio if other.audio is not None else self.audio,
            subtitles=other.subtitles if other.subtitles is not None else self.subtitles,
        )


class SeriesOverride(Strict):
    """Different match criteria for specific series (e.g. Chinese rather than Japanese audio)."""

    series: list[str] = Field(min_length=1, description="Series names, case-insensitive.")
    # Replaces the rule's audio and/or subtitles criteria; whichever is omitted is inherited.
    match: MediaMatch


class RuleBase(Strict):
    """Fields shared by every rule type."""

    name: str = ""  # filled in from the mapping key
    target: Literal["playlist", "collection"] = "playlist"
    enabled: bool = True
    limit: int | None = Field(default=None, gt=0)
    sort: SortSpec = SortSpec()
    # Remove the playlist/collection entirely when the rule produces no items.
    delete_when_empty: bool = True

    @field_validator("sort", mode="before")
    @classmethod
    def _parse_sort(cls, v: object) -> object:
        return SortSpec.parse(v) if isinstance(v, str) else v


class MediaFilterRule(RuleBase):
    """Select individual episodes/movies by their media streams."""

    type: Literal["media_filter"]
    item_type: Literal["episode", "movie"] = "episode"
    match: MediaMatch
    overrides: list[SeriesOverride] = Field(default_factory=list)
    sort: SortSpec = SortSpec(key=Sort.SERIES)

    @field_validator("overrides")
    @classmethod
    def _unique_series(cls, v: list[SeriesOverride]) -> list[SeriesOverride]:
        seen: set[str] = set()
        for override in v:
            for name in override.series:
                key = name.strip().lower()
                if key in seen:
                    raise ValueError(f"series {name!r} appears in more than one override")
                seen.add(key)
        return v

    def match_for(self, series_name: str | None) -> MediaMatch:
        """Effective criteria for an item in `series_name`, applying any override."""
        key = (series_name or "").strip().lower()
        for override in self.overrides:
            if key in {s.strip().lower() for s in override.series}:
                return self.match.overlay(override.match)
        return self.match


class ContinueWatchingRule(RuleBase):
    """Next unwatched episode of every season that is started but unfinished."""

    type: Literal["continue_watching"]
    # Include Season 0 / Specials when deciding progress.
    include_specials: bool = False
    # One entry per series (the single next episode) vs. every remaining episode.
    episodes_per_series: int = 1
    sort: SortSpec = SortSpec(key=Sort.LAST_PLAYED, descending=True)


class NewSeasonsRule(RuleBase):
    """First episode of an unwatched season of a show whose earlier season is finished."""

    type: Literal["new_seasons"]
    include_specials: bool = False
    sort: SortSpec = SortSpec(key=Sort.LAST_PLAYED, descending=True)


Rule = Annotated[
    MediaFilterRule | ContinueWatchingRule | NewSeasonsRule,
    Field(discriminator="type"),
]


class LibraryConfig(Strict):
    rules: dict[str, Rule] = Field(default_factory=dict)


class JellyfinConfig(Strict):
    url: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    user: str = Field(description="Username whose watch state drives user-specific rules.")
    timeout: float = 60.0
    verify_ssl: bool = True

    @field_validator("url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")


class Options(Strict):
    page_size: int = Field(default=500, gt=0, le=1000)
    # Parallel page requests when listing large libraries.
    concurrency: int = Field(default=4, ge=1, le=16)
    # Prefix applied to every created playlist/collection name.
    name_prefix: str = ""


class Config(Strict):
    jellyfin: JellyfinConfig
    options: Options = Options()
    libraries: dict[str, LibraryConfig] = Field(default_factory=dict)

    def iter_rules(self) -> list[tuple[str, Rule]]:
        """Yield (library_name, rule) for every enabled rule."""
        out: list[tuple[str, Rule]] = []
        for lib_name, lib in self.libraries.items():
            for rule in lib.rules.values():
                if rule.enabled:
                    out.append((lib_name, rule))
        return out


def _substitute_env(node: object) -> object:
    """Recursively expand ${VAR} references in string values."""
    if isinstance(node, str):

        def repl(m: re.Match[str]) -> str:
            name = m.group(1)
            try:
                return os.environ[name]
            except KeyError:
                raise ValueError(f"config references undefined env var ${{{name}}}") from None

        return _ENV_PATTERN.sub(repl, node)
    if isinstance(node, dict):
        return {k: _substitute_env(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_substitute_env(v) for v in node]
    return node


def load_config(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text()) or {}
    raw = _substitute_env(raw)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top level of config must be a mapping")

    # Inject each rule's mapping key as its name so rules are addressable by it.
    for lib in (raw.get("libraries") or {}).values():
        for rule_name, rule in ((lib or {}).get("rules") or {}).items():
            if isinstance(rule, dict):
                rule.setdefault("name", rule_name)

    return Config.model_validate(raw)
