"""The repository configuration ``.github/cicd-doctor.yml``: parsing, validation, cache (D-90).

The file belongs to the repository, so it is untrusted input like a log: it is read only from the
default branch (the adapter never asks for another ref), it is capped, and it is checked as a
stream of YAML events *before* anything is built from it. What the events reject (aliases,
anchors, tags, several documents, duplicate keys, deep nesting, too many nodes) is what a
``safe_load`` would accept silently or answer slowly. A file that does not validate turns the LLM
off (fail closed for the privacy setting) and names the failing fields, never their values.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from cicd_doctor.core.models import LoadedConfig, RepoConfig, RepoRef
from cicd_doctor.core.ports import Clock, RepoConfigSource

MAX_CONFIG_BYTES = 16 * 1024  # D-90: checked before parsing
MAX_NODES = 2000
MAX_DEPTH = 8
MAX_PROBLEMS = 5
MAX_PROBLEM_CHARS = 60
_SAFE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.[]")

DEFAULT_LOADED = LoadedConfig()


def invalid(*problems: str) -> LoadedConfig:
    """An invalid file: the defaults, with ``problems`` (already field paths) attached."""
    return LoadedConfig(state="invalid", problems=tuple(problems[:MAX_PROBLEMS]))


def parse_repo_config(text: str) -> LoadedConfig:
    """The configuration of one file's text. Never raises, never echoes a value of the file.

    An empty file (or one with only comments) is the same as no file.
    """
    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        return invalid("size")
    problems = _scan(text)
    if problems:
        return invalid(*problems)
    try:
        data: Any = yaml.safe_load(text)
    except (yaml.YAMLError, RecursionError, ValueError):
        return invalid("yaml")
    if data is None:
        return DEFAULT_LOADED
    if not isinstance(data, dict):
        return invalid("root")
    if data.get("llm") is False:
        # YAML 1.1 reads the bare word ``off`` (and ``no``) as False: the owner meant "off".
        data = {**data, "llm": "off"}
    try:
        config = RepoConfig.model_validate(data, strict=True)
    except ValidationError as exc:
        paths: list[str] = []
        for error in exc.errors():
            path = _path(error["loc"])
            if path not in paths:
                paths.append(path)
        return invalid(*paths)
    return LoadedConfig(config=config, state="file")


def effective_llm(loaded: LoadedConfig) -> Literal["auto", "on_demand", "off"]:
    """The ``llm`` setting that applies: an invalid file turns the LLM off (D-90)."""
    return "off" if loaded.state == "invalid" else loaded.config.llm


def ignores(loaded: LoadedConfig, workflow_name: str) -> bool:
    """Whether the repository asked to ignore this workflow (exact, case-sensitive name)."""
    return workflow_name in loaded.config.ignore_workflows


def _path(loc: tuple[int | str, ...]) -> str:
    """``ignore_workflows[2]`` or ``comment``. A key of the file is untrusted text: anything
    outside a short safe alphabet becomes ``?``."""
    out = ""
    for part in loc:
        if isinstance(part, int):
            out += f"[{part}]"
        else:
            name = part if part and set(part) <= _SAFE_CHARS else "?"
            out += ("." if out else "") + name[:MAX_PROBLEM_CHARS]
    return out or "root"


@dataclass
class _Frame:
    mapping: bool
    seen: set[str] = field(default_factory=set)
    count: int = 0  # children seen so far (a mapping alternates key, value)


def _scan(text: str) -> list[str]:
    """Walk the YAML events and name what is rejected before any value is built."""
    problems: list[str] = []
    stack: list[_Frame] = []
    nodes = 0
    documents = 0
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.DocumentStartEvent):
                documents += 1
                if documents > 1:
                    return ["documents"]
            if isinstance(event, yaml.AliasEvent):
                return ["aliases"]
            if isinstance(event, yaml.NodeEvent) and getattr(event, "anchor", None) is not None:
                return ["anchors"]
            if isinstance(event, yaml.CollectionStartEvent) and event.tag is not None:
                return ["tags"]
            if isinstance(event, yaml.ScalarEvent) and event.tag is not None:
                return ["tags"]
            if isinstance(event, yaml.CollectionEndEvent):
                stack.pop()
                continue
            if not isinstance(
                event, (yaml.ScalarEvent, yaml.SequenceStartEvent, yaml.MappingStartEvent)
            ):
                continue
            nodes += 1
            if nodes > MAX_NODES:
                return ["size"]
            if stack:
                parent = stack[-1]
                is_key = parent.mapping and parent.count % 2 == 0
                parent.count += 1
                if is_key:
                    key = _key_problem(event, parent)
                    if key is not None:
                        problems.append(key)
                        if len(problems) >= MAX_PROBLEMS:
                            return problems
            if isinstance(event, (yaml.SequenceStartEvent, yaml.MappingStartEvent)):
                if len(stack) >= MAX_DEPTH:
                    return ["depth"]
                stack.append(_Frame(mapping=isinstance(event, yaml.MappingStartEvent)))
    except (yaml.YAMLError, RecursionError, ValueError):
        return ["yaml"]
    return problems


def _key_problem(event: yaml.Event, parent: _Frame) -> str | None:
    if not isinstance(event, yaml.ScalarEvent):
        return "key"
    if event.value == "<<":
        return "merge"
    if event.value in parent.seen:
        return f"duplicate:{_path((event.value,))}"
    parent.seen.add(event.value)
    return None


# --- the cache ---------------------------------------------------------------------------------


class CachedRepoConfig:
    """A ``RepoConfigSource`` that remembers answers for a while, per repository (D-90).

    A valid or invalid file is kept ``ttl``; "no file" and "unreadable" are kept ``missing_ttl``
    (shorter: a repository that just added its file should not wait long). A transient error is
    never cached and never turned into the defaults: it propagates and the job retries. There is
    no invalidation: a change on the default branch applies within ``ttl``.
    """

    PRUNE_AT = 1024

    def __init__(
        self,
        source: RepoConfigSource,
        clock: Clock,
        *,
        ttl: timedelta = timedelta(seconds=300),
        missing_ttl: timedelta = timedelta(seconds=60),
    ) -> None:
        self._source = source
        self._clock = clock
        self._ttl = ttl
        self._missing_ttl = missing_ttl
        self._entries: dict[int, tuple[datetime, LoadedConfig]] = {}

    async def load(self, repo: RepoRef) -> LoadedConfig:
        now = self._clock.now()
        hit = self._entries.get(repo.repo_id)
        if hit is not None and now < hit[0]:
            return hit[1]
        loaded = await self._source.load(repo)
        wait = self._ttl if loaded.state in ("file", "invalid") else self._missing_ttl
        if len(self._entries) >= self.PRUNE_AT:
            self._entries = {k: v for k, v in self._entries.items() if now < v[0]}
        self._entries[repo.repo_id] = (now + wait, loaded)
        return loaded
