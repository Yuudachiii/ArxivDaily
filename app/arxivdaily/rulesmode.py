"""Deterministic rule analysis for the LLM-free daily path.

``RulesAnalysis`` implements the subset of the ``AnalysisClient`` interface
that the pipeline and translation client actually use.  It makes no network
calls of its own: selection is decided by the keyword routes that the
collector already evaluated, and the model-only stages (review/summary/LLM
translation) fail explicitly if configuration ever routes work to them.
"""
from __future__ import annotations

import re
import time
from typing import Callable

import httpx

from .models import Decision, Paper, ProcessingError, fingerprint


_ROUTE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


class RulesAnalysis:
    """AnalysisClient-compatible adapter with no model or DeepSeek dependency."""

    model_id = "rules"

    def __init__(self, config: dict, client: httpx.Client | None = None):
        self.config = config
        self.client = client or httpx.Client()
        self.usage_records: list[dict] = []
        self.usage_start_cursor = 0
        self.run_seconds = float(config.get("limits", {}).get("run_seconds", 2400))
        self.deadline_monotonic: float | None = None
        self.run_deadline_monotonic: float | None = None
        self.chunk_cache: dict[str, list[dict]] = {}
        self.on_usage: Callable[[], None] | None = None
        self.on_chunk: Callable[[str, list[dict]], None] | None = None
        self.calls = {"selection": 0, "review": 0, "summary": 0}

    def prompt_fingerprint(self, task: str) -> str:
        return fingerprint({"mode": "rules", "routes": self.config["rules"]["routes"]})

    def select(self, paper: Paper, hits: list[str]) -> Decision:
        """Accept every rule candidate as a translation-tier digest item."""
        self.calls["selection"] += 1
        hit_routes = [name for name in hits if isinstance(name, str) and _ROUTE_NAME.fullmatch(name)]
        route = hit_routes[0] if hit_routes else "rules"
        reason = "确定性规则命中：" + "、".join(hit_routes) if hit_routes else "确定性规则模式保留候选"
        return Decision(
            status="select", route=route, tier="translation", reason=reason,
            abstract_evidence=[], uncertainty=[],
            rl_is_method=False, dpo_is_method=False,
            theory_evidence=False, transfer_evidence=False, score=0.0,
        )

    def review(self, paper: Paper, body, decision: Decision, *, evidence: bool = True) -> Decision:
        raise ProcessingError("llm_disabled")

    def summarize(self, paper: Paper, body, *, evidence: bool = True):
        raise ProcessingError("llm_disabled")

    def translate(self, paper: Paper, *, title_only: bool = False):
        raise ProcessingError("llm_disabled")

    def _begin_stage(self, role: str) -> None:
        if role == "translation":
            configured = self.config.get("translation", {}).get("stage_seconds", 900)
        else:
            configured = 180 if role == "filter" else 900
        seconds = min(float(configured), self.run_seconds)
        self.deadline_monotonic = time.monotonic() + seconds

    def _guard(self) -> None:
        now = time.monotonic()
        if self.deadline_monotonic is not None and now >= self.deadline_monotonic:
            raise ProcessingError("rules_stage_time_guard")
        if self.run_deadline_monotonic is not None and now >= self.run_deadline_monotonic:
            raise ProcessingError("rules_run_time_guard")

    def _remaining_seconds(self, configured: float) -> float:
        deadlines = [value for value in (self.deadline_monotonic, self.run_deadline_monotonic) if value is not None]
        return min([configured] + [max(0.0, value - time.monotonic()) for value in deadlines])
