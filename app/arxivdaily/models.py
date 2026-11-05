"""Versioned domain contracts shared by collection, analysis and delivery."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any


class ProcessingError(RuntimeError):
    """A failed stage, never a semantic rejection of a paper."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timezone-aware datetime required")
    return value.astimezone(timezone.utc).isoformat()


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone-aware timestamp required")
    return result.astimezone(timezone.utc)


def normalize_id(value: str) -> tuple[str, str]:
    value = re.sub(r"^https?://(?:export\.)?arxiv\.org/(?:abs|pdf|html)/", "", value.strip())
    value = value.removesuffix(".pdf")
    match = re.fullmatch(r"(\d{4}\.\d{4,5}|[a-zA-Z][a-zA-Z.\-]*/\d{7})(v[1-9]\d*)?", value)
    if not match:
        raise ValueError("invalid arXiv ID")
    return match[1], value


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass
class Paper:
    version_id: str
    title: str
    abstract: str
    categories: list[str]
    published: str
    updated: str
    authors: list[str] = field(default_factory=list)
    pdf_url: str = ""

    def __post_init__(self) -> None:
        _, self.version_id = normalize_id(self.version_id)
        parse_time(self.published)
        parse_time(self.updated)
        if not self.title.strip() or not self.abstract.strip():
            raise ValueError("paper title and abstract are required")

    @property
    def base_id(self) -> str:
        return normalize_id(self.version_id)[0]

    @property
    def url(self) -> str:
        return f"https://arxiv.org/abs/{self.version_id}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Paper:
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


# Legacy LLM route names kept for old saved fixtures/state; rules mode only
# needs any syntactic route label (the configured rule name or "rules").
ROUTES = {"medical_reasoning", "medical_objective", "general_rl", "transferable_objective", "general_reasoning", "irrelevant"}
_ROUTE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


@dataclass
class Decision:
    status: str
    route: str
    tier: str
    reason: str
    abstract_evidence: list[str]
    uncertainty: list[str]
    rl_is_method: bool
    dpo_is_method: bool
    theory_evidence: bool
    transfer_evidence: bool
    score: float = 0
    review_evidence: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in {"select", "reject", "uncertain"} or self.tier not in {"full", "light", "translation"} or not isinstance(self.route, str) or not _ROUTE_NAME.fullmatch(self.route):
            raise ProcessingError("invalid selection enum")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ProcessingError("selection reason missing")
        for key in ("rl_is_method", "dpo_is_method", "theory_evidence", "transfer_evidence"):
            if type(getattr(self, key)) is not bool:
                raise ProcessingError("selection flags must be boolean")
        for key in ("abstract_evidence", "uncertainty"):
            value = getattr(self, key)
            if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
                raise ProcessingError("selection evidence must be string arrays")
        if not isinstance(self.review_evidence, list) or any(not isinstance(e, dict) or not all(isinstance(e.get(k), str) and e[k] for k in ("locator", "quote", "source_url")) for e in self.review_evidence):
            raise ProcessingError("invalid fulltext review evidence")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)) or not 0 <= self.score <= 100:
            raise ProcessingError("invalid relevance score")
        if self.status == "select" and self.route == "irrelevant":
            raise ProcessingError("irrelevant route cannot be selected")
        if self.status == "select" and self.tier != "translation":
            # Legacy model-decision rules; deterministic rules mode uses the
            # translation tier and must not inherit these route semantics.
            if self.route == "general_reasoning":
                self.tier = "light"
            if self.route == "general_rl" and not (self.rl_is_method or self.dpo_is_method):
                raise ProcessingError("general RL route requires a method contribution")
            if self.route == "transferable_objective" and not (self.theory_evidence and self.transfer_evidence):
                raise ProcessingError("objective route requires theory and transfer evidence")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Decision:
        try:
            return cls(**data)
        except (TypeError, ValueError) as exc:
            raise ProcessingError("invalid selection schema") from exc


@dataclass
class Section:
    locator: str
    text: str
    kind: str = "section"


@dataclass
class Figure:
    label: str
    locator: str
    caption: str
    image_path: str = ""
    required: bool = False


@dataclass
class Body:
    version_id: str
    source_url: str
    source_type: str
    sections: list[Section]
    figures: list[Figure] = field(default_factory=list)
    quality: dict = field(default_factory=dict)

    @property
    def qualified(self) -> bool:
        return self.quality.get("passed") is True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Body:
        return cls(data["version_id"], data["source_url"], data["source_type"], [Section(**s) for s in data["sections"]], [Figure(**s) for s in data.get("figures", [])], data.get("quality", {}))


@dataclass
class Evidence:
    field: str
    locator: str
    quote: str
    source_url: str
    label: str = ""


@dataclass
class Summary:
    background: str
    contribution: str
    method: str
    experiments: str
    conclusion: str
    evidence: list[Evidence]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Summary:
        try:
            values = dict(data)
            values["evidence"] = [Evidence(**e) for e in values["evidence"]]
            result = cls(**values)
            if any(not isinstance(getattr(result, key), str) or not getattr(result, key).strip() for key in ("background", "contribution", "method", "experiments", "conclusion")):
                raise ValueError("empty summary field")
            return result
        except (TypeError, ValueError, KeyError) as exc:
            raise ProcessingError("invalid summary schema") from exc


@dataclass
class Translation:
    title_zh: str
    abstract_zh: str = ""
    fallback_provider: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.title_zh, str) or not self.title_zh.strip() or not isinstance(self.abstract_zh, str):
            raise ProcessingError("translation_schema_invalid")

    def to_dict(self) -> dict:
        result = asdict(self)
        if not self.fallback_provider:
            result.pop("fallback_provider")
        return result

    @classmethod
    def from_dict(cls, data: dict) -> Translation:
        try:
            return cls(**data)
        except (TypeError, ValueError):
            raise ProcessingError("translation_schema_invalid") from None


@dataclass
class DigestItem:
    paper: Paper
    tier: str
    summary: Summary | None = None
    recovery_of: str = ""
    translation: Translation | None = None
    modules: list[str] | None = None
    rule_hits: list[str] = field(default_factory=list)


@dataclass
class DeliveryResult:
    status: str
    pushid: str = ""
    detail: str = ""
    polls: int = 0

    def __post_init__(self) -> None:
        if self.status not in {"queued", "confirmed", "failed", "unknown"}:
            raise ValueError("invalid delivery result")

    def to_dict(self) -> dict:
        return asdict(self)
