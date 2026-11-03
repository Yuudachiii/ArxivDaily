"""Bounded, recoverable arXiv metadata collection through arxiv.py 4.x."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable

from .models import Paper, ProcessingError, iso, normalize_id, parse_time
from .rules import match_rules
from .upstream import canonicalize_metadata, merge_metadata


@dataclass
class CollectionResult:
    papers: list[Paper] = field(default_factory=list)
    complete: bool = True
    shards: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _moment(value: str | date | datetime, *, end_of_day: bool = False) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("window datetimes must have a timezone")
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.max if end_of_day else time.min, timezone.utc)
    if len(value) == 10:
        return _moment(date.fromisoformat(value), end_of_day=end_of_day)
    return parse_time(value)


def plan_window(
    now: datetime,
    state: dict,
    manual_start: str | date | datetime | None = None,
    manual_end: str | date | datetime | None = None,
    *,
    initial_days: int = 7,
    lookback_days: int = 14,
) -> dict:
    """Plan UTC collection, retaining a first-run floor and reporting old gaps.

    `last_complete_collection` is the end of the last fully persisted window.
    A manual date-only end covers its full UTC day. Manual runs do not change
    the automatic floor or collection checkpoint.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not (type(initial_days) is int and 1 <= initial_days <= 7 and type(lookback_days) is int and 1 <= lookback_days <= 14):
        raise ValueError("automatic collection windows are capped at 7 initial and 14 lookback days")
    now = now.astimezone(timezone.utc)
    collection_state = state.get("collection", state)
    floor_value = collection_state.get("initial_floor")
    floor = _moment(floor_value) if floor_value else now - timedelta(days=initial_days)
    if manual_start is not None or manual_end is not None:
        if manual_start is None or manual_end is None:
            raise ValueError("manual_start and manual_end must be supplied together")
        start = _moment(manual_start)
        end = _moment(manual_end, end_of_day=True)
        if end < start:
            raise ValueError("manual end precedes start")
        return {"start": iso(start), "end": iso(end), "initial_floor": iso(floor), "uncovered": [], "manual": True}
    # A floor saved under a shorter initial window must not block an expansion.
    floor = min(floor, now - timedelta(days=initial_days))
    start = max(floor, now - timedelta(days=lookback_days))
    uncovered = []
    last_value = collection_state.get("last_complete_collection") or collection_state.get("last_complete_end")
    if last_value:
        last = _moment(last_value)
        if last < start:
            uncovered.append({"start": iso(max(last, floor)), "end": iso(start), "reason": "beyond_auto_lookback; use manual date backfill"})
    elif floor < start:
        uncovered.append({"start": iso(floor), "end": iso(start), "reason": "initial window exceeded auto lookback before first complete collection; use manual date backfill"})
    return {"start": iso(start), "end": iso(now), "initial_floor": iso(floor), "uncovered": uncovered, "manual": False}


def _api_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%d%H%M")


def _query(start: datetime, end: datetime, categories: tuple[str, ...]) -> str:
    window = f"submittedDate:[{_api_time(start)} TO {_api_time(end)}]"
    if not categories:
        return window
    category_clause = " OR ".join(f"cat:{category}" for category in categories)
    return f"({category_clause}) AND {window}"


def _paper(result: Any) -> Paper:
    identifier = getattr(result, "entry_id", None)
    if not identifier and hasattr(result, "get_short_id"):
        identifier = result.get_short_id()
    authors = [str(getattr(author, "name", author)) for author in getattr(result, "authors", [])]
    return canonicalize_metadata(Paper(
        version_id=identifier,
        title=result.title.strip(),
        abstract=result.summary.strip(),
        categories=list(result.categories),
        published=iso(result.published),
        updated=iso(result.updated),
        authors=authors,
        pdf_url=str(getattr(result, "pdf_url", "") or ""),
    ))


class Collector:
    """One arxiv.Client, sequential searches, complete pagination, finite splits."""

    def __init__(self, config: dict, client: Any = None):
        self.config = config
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import arxiv

            settings = self.config["collection"]
            self._client = arxiv.Client(
                page_size=int(settings["page_size"]),
                delay_seconds=max(3.0, float(settings["delay_seconds"])),
                num_retries=3,
            )
        return self._client

    @staticmethod
    def _search(*, query: str | None = None, id_list: list[str] | None = None) -> Any:
        import arxiv

        kwargs: dict[str, Any] = {"max_results": None}
        if query is not None:
            kwargs["query"] = query
        if id_list is not None:
            kwargs["id_list"] = id_list
        return arxiv.Search(**kwargs)

    def collect(self, window: dict, *, apply_rules: bool = True) -> CollectionResult:
        """Collect rule candidates, or category metadata for isolated rule experiments.

        The optional metadata-only path shares pagination/deduplication and the
        configured record cap, but never consults application state or an LLM.
        """
        requested_start, requested_end = _moment(window["start"]), _moment(window["end"])
        if requested_end < requested_start:
            raise ValueError("window end precedes start")
        start = requested_start.replace(second=0, microsecond=0)
        end = requested_end.replace(second=0, microsecond=0)
        result = CollectionResult()
        categories = tuple(dict.fromkeys(self.config["rules"].get("categories", [])))
        shard_limit = min(int(self.config["collection"].get("shard_limit", 10000)), 10000)
        # The cap protects new downstream work. Previously recorded base IDs
        # still appear in this scan so their metadata/version can be observed,
        # but cannot repeatedly consume the discovery budget on rolling runs.
        known_ids = {normalize_id(value)[0] for value in window.get("known_ids", [])}
        candidate_limit = int(self.config["collection"].get("max_candidates", 2000))
        papers: dict[str, Paper] = {}
        new_ids: set[str] = set()
        # Process UTC time splits before category splits so cross listings can
        # still be resolved into a single base ID. All boundaries overlap by a
        # minute because the API's date syntax is inclusive and minute-grained.
        pending = [(start, end, categories, 0)]
        while pending:
            if len(result.shards) >= 256:
                result.complete = False
                result.errors.append("collection shard work limit reached; manual or next-run recovery required")
                for unvisited in pending:
                    result.shards.append({"start": iso(unvisited[0]), "end": iso(unvisited[1]), "categories": list(unvisited[2]), "status": "unvisited", "seen": 0})
                break
            lo, hi, cats, depth = pending.pop()
            shard = {"start": iso(lo), "end": iso(hi), "categories": list(cats), "status": "pending", "seen": 0}
            result.shards.append(shard)
            try:
                search = self._search(query=_query(lo, hi, cats))
                for raw in self.client.results(search):
                    shard["seen"] += 1
                    paper = _paper(raw)
                    published = parse_time(paper.published)
                    if published < requested_start or published > requested_end:
                        continue
                    # API date boundaries and category cross listings can
                    # overlap. Keep the newest version, never duplicate IDs.
                    category_match = not categories or bool(set(categories).intersection(paper.categories))
                    if (category_match and not apply_rules) or (apply_rules and match_rules(paper, self.config)):
                        existing = papers.get(paper.base_id)
                        if existing is None:
                            if paper.base_id not in known_ids and paper.base_id not in new_ids and len(new_ids) >= candidate_limit:
                                shard["status"] = "candidate_limit"
                                result.complete = False
                                result.errors.append(f"new-candidate cap {candidate_limit} exceeded; remaining coverage requires backfill")
                                break
                            papers[paper.base_id] = paper
                            if paper.base_id not in known_ids:
                                new_ids.add(paper.base_id)
                        else:
                            papers[paper.base_id] = merge_metadata(existing, paper)
                    if shard["seen"] >= shard_limit:
                        shard["status"] = "shard_limit"
                        break
                else:
                    shard["status"] = "complete"
            except Exception as exc:
                # A later page may fail after earlier pages were read. Those
                # candidates remain useful, but the shard cannot checkpoint.
                shard["status"] = "failed"
                shard["error"] = type(exc).__name__
            if shard["status"] == "candidate_limit":
                for unvisited in pending:
                    result.shards.append({"start": iso(unvisited[0]), "end": iso(unvisited[1]), "categories": list(unvisited[2]), "status": "unvisited", "seen": 0})
                pending.clear()
                continue
            if shard["status"] in {"shard_limit", "failed"}:
                children = self._split(lo, hi, cats)
                # Do not fan out persistent API errors without evidence of a
                # page/capacity problem; retry the full window on a later run.
                can_recover = shard["status"] == "shard_limit" or shard["seen"] >= self.config["collection"]["page_size"]
                if children and can_recover and depth < 24:
                    shard["status"] = "split"
                    pending.extend((a, b, c, depth + 1) for a, b, c in reversed(children))
                else:
                    result.complete = False
                    result.errors.append(
                        f"incomplete collection shard {_api_time(lo)}..{_api_time(hi)} "
                        f"({','.join(cats) or 'all categories'}): {shard.get('error', shard['status'])}"
                    )
        result.papers = sorted(papers.values(), key=lambda p: (p.published, p.base_id), reverse=True)
        return result

    @staticmethod
    def _split(start: datetime, end: datetime, categories: tuple[str, ...]) -> list[tuple[datetime, datetime, tuple[str, ...]]]:
        first_minute = start.replace(second=0, microsecond=0)
        last_minute = end.replace(second=0, microsecond=0)
        minutes = int((last_minute - first_minute).total_seconds() // 60)
        if minutes >= 1:
            if minutes == 1:
                return [(first_minute, first_minute, categories), (last_minute, last_minute, categories)]
            midpoint = first_minute + timedelta(minutes=minutes // 2)
            return [(start, midpoint, categories), (midpoint, end, categories)]
        if len(categories) > 1:
            middle = len(categories) // 2
            return [(start, end, categories[:middle]), (start, end, categories[middle:])]
        return []

    def by_ids(self, ids: Iterable[str]) -> list[Paper]:
        """Fetch explicit IDs independently of the normal date/rule window."""
        wanted = list(dict.fromkeys(normalize_id(value)[1] for value in ids))
        requested: dict[str, set[str]] = {}
        for identifier in wanted:
            base, _ = normalize_id(identifier)
            requested.setdefault(base, set()).add(identifier)
        found: dict[str, Paper] = {}
        for offset in range(0, len(wanted), 100):
            search = self._search(id_list=wanted[offset:offset + 100])
            for raw in self.client.results(search):
                paper = _paper(raw)
                if paper.base_id not in requested:
                    raise ProcessingError("arXiv returned an unrequested ID")
                allowed = requested[paper.base_id]
                if paper.version_id not in allowed and paper.base_id not in allowed:
                    raise ProcessingError(f"arXiv returned {paper.version_id} instead of a requested pinned version")
                found[paper.version_id] = paper
        output: list[Paper] = []
        for identifier in wanted:
            base, _ = normalize_id(identifier)
            if identifier == base:
                matches = (paper for paper in found.values() if paper.base_id == base)
                paper = max(matches, key=lambda item: item.updated, default=None)
            else:
                paper = found.get(identifier)
            if paper is None:
                raise ProcessingError(f"arXiv did not return requested ID {identifier}")
            if paper not in output:
                output.append(paper)
        return output
