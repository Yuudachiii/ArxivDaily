"""Resumable daily batch: stage checkpoints precede every external side effect."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import time
import uuid
from zoneinfo import ZoneInfo

from .collection import CollectionResult, plan_window
from .llm import validate_summary
from .models import Decision, DeliveryResult, DigestItem, Paper, ProcessingError, Summary, Translation, fingerprint, iso, parse_time, utcnow
from .render import render_archive, render_notification, server_title
from .rules import match_rules
from .state import PersistenceError, StateStore, body_metadata, confirmed, upsert_paper
from .translation import TranslationClient


class Pipeline:
    def __init__(self, config: dict, store: StateStore, collector, analysis, reader, delivery=None, *, now: datetime | None = None, real_send=False, clock=None, translator=None):
        self.config, self.store = config, store
        self.collector, self.analysis, self.reader, self.delivery = collector, analysis, reader, delivery
        self.now = now or utcnow()
        self.clock = clock or ((lambda: self.now) if now is not None else utcnow)
        self.real_send = real_send
        self.translator = translator or TranslationClient(config, analysis)
        self.day = self.now.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        self.quota_day = self.now.astimezone(ZoneInfo(config["delivery"]["quota_timezone"])).date().isoformat()
        self.run_id = self.now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        self.started = time.monotonic()
        self.analysis.run_deadline_monotonic = self.started + config["limits"]["run_seconds"]
        self.analysis.on_usage = self.checkpoint
        self.state: dict = {}
        self.body_cache = {}
        self.usage_cursor = len(getattr(self.analysis, "usage_records", []))
        self.analysis.usage_start_cursor = self.usage_cursor
        self.errors: list[str] = []
        self.notes: list[str] = []
        self.promotion_ids: set[str] = set()
        self.resend_ids: set[str] = set()
        self.retry_ids: set[str] = set()
        self.retry_stage = None
        self.daily_ids: set[str] | None = None

    def _manual_active(self, record: dict) -> bool:
        return bool(record.get("manual_retry_until") and self.now <= parse_time(record["manual_retry_until"]))

    def _prompt_fingerprint(self, task: str) -> str:
        getter = getattr(self.analysis, "prompt_fingerprint", None)
        return getter(task) if getter else "scripted-offline-" + task

    def _chain_limit(self, record: dict) -> int:
        normal = self.config["delivery"]["chain_attempts"]
        return max(normal,record.get("manual_delivery_attempt_limit",normal)) if self._manual_active(record) else normal

    def checkpoint(self) -> None:
        new_usage = getattr(self.analysis, "usage_records", [])[self.usage_cursor:]
        self.usage_cursor += len(new_usage)
        self.state["usage"].extend({"run_id": self.run_id, **u} for u in new_usage)
        self.store.save(self.state)

    def _budget(self) -> None:
        if time.monotonic() - self.started >= self.config["limits"]["run_seconds"]:
            raise ProcessingError("run_time_guard")
        cap = self.config["llm"].get("max_run_cost")
        if cap is not None:
            usage = [u for u in self.state["usage"] if u["run_id"] == self.run_id]
            prices = self.config["llm"].get("pricing", {})
            if any(prices.get(k, 0) <= 0 for k in ("cache_hit_per_million", "cache_miss_per_million", "output_per_million")):
                raise ProcessingError("cost_guard_requires_verified_prices")
            if any(u.get("usage_available") is False or u.get("estimated_cost") is None for u in usage):
                raise ProcessingError("run_cost_guard_usage_unknown")
            if sum(u.get("estimated_cost") or 0 for u in usage) >= cap:
                raise ProcessingError("run_cost_guard")

    def _stage(self, record: dict, stage: str, operation):
        count = record["attempts"].get(stage, 0)
        failure = record["failures"].get(stage)
        if count >= self.config["limits"]["stage_attempts"]:
            if stage != "title_translation":
                record["manual_required"] = True
            self.errors.append(f"{record['paper']['version_id']}:{stage}:attempts_exhausted")
            return None
        if failure and parse_time(failure["last_at"]).date() == self.now.date():
            # At most one cross-run stage recovery per UTC day, each request
            # already has its own finite transport retry budget.
            self.errors.append(f"{record['paper']['version_id']}:{stage}:recovery_deferred_after_failure")
            return None
        self._budget()
        record["attempts"][stage] = count + 1
        record["stage_started"] = {"stage": stage, "run_id": self.run_id, "at": iso(self.now)}
        self.checkpoint()
        try:
            value = operation()
        except PersistenceError:
            raise
        except Exception as exc:
            # Do not stringify request/response exceptions or signed URLs.
            detail = str(exc) if type(exc) is ProcessingError else type(exc).__name__
            detail = self.store.scrubber.text(detail)[:180]
            record["failures"][stage] = {"last_at": iso(self.now), "error": detail, "attempts": count + 1}
            self.errors.append(f"{record['paper']['version_id']}:{stage}:{detail}")
            record.pop("stage_started", None)
            self.checkpoint()
            return None
        record["failures"].pop(stage, None)
        record.pop("stage_started", None)
        return value

    def _body(self, record: dict):
        paper = Paper.from_dict(record["paper"])
        if self.resend_ids or record.get("resend_pending"):
            return False  # Explicit resend always reuses confirmed content.
        if self.promotion_ids and paper.base_id not in self.promotion_ids:
            return False
        if paper.base_id in self.body_cache:
            return self.body_cache[paper.base_id]
        def read():
            body = self.reader.read(paper)
            if not body.qualified or body.version_id != paper.version_id:
                raise ProcessingError("fulltext_gate_or_version_failed")
            return body
        body = self._stage(record, "body", read)
        if body is not None:
            self.body_cache[paper.base_id] = body
            record["body"] = body_metadata(body)
            self.checkpoint()
        return body

    def _bind_analysis_cache(self, record: dict) -> None:
        self.analysis.chunk_cache = record.setdefault("analysis_chunks", {})
        def persist_chunk(key, notes):
            record["analysis_chunks"][key] = notes
            self.checkpoint()
        self.analysis.on_chunk = persist_chunk

    def _daily(self, day: str | None = None) -> dict:
        return self.state["daily"].setdefault(day or self.day, {"full_ids": [], "review_ids": [], "send_attempts": 0, "query_reservations": 0, "normal_digests": [], "alerts": []})

    def _full_slot(self, key: str) -> bool:
        daily = self._daily()
        if key in daily["full_ids"]:
            return True
        if len(daily["full_ids"]) >= self.config["limits"]["full_daily_limit"]:
            return False
        daily["full_ids"].append(key)
        return True

    def _pending(self, record: dict) -> bool:
        return bool(record.get("pending_digest"))

    def _daily_paper_ids(self) -> list[str]:
        ids = self._daily().setdefault("paper_ids", [])
        attempted = []
        # Include deliveries made before the paper limit was introduced.
        for digest in self.state["digests"].values():
            if (digest.get("quota_day", digest.get("day")) == self.day
                    and digest["status"] in {"confirmed", "sending", "unknown"}):
                for item in digest["items"]:
                    key = Paper.from_dict(item["paper"]).base_id
                    if key not in attempted:
                        attempted.append(key)
        # Preserve actual attempts; release excess unused slots after a limit reduction.
        remaining = max(0, self.config["limits"]["daily_paper_limit"] - len(attempted))
        ids[:] = attempted + [key for key in ids if key not in attempted][:remaining]
        return ids

    def _daily_candidates(self, start=None) -> list[dict]:
        start = start or self.now - timedelta(days=self.config["collection"]["max_lookback_days"])
        records = []
        for record in self.state["papers"].values():
            paper = Paper.from_dict(record["paper"])
            if (start <= parse_time(paper.published) <= self.now and not confirmed(record)
                    and not record.get("manual_required") and record.get("stage") != "rejected"
                    and match_rules(paper, self.config)):
                records.append(record)
        return sorted(records, key=lambda r: (parse_time(r["paper"]["published"]), r["paper"]["version_id"]), reverse=True)

    def _daily_target(self, recent_ids=()) -> int:
        start = self.now - timedelta(days=self.config["collection"]["lookback_days"])
        recent = {Paper.from_dict(r["paper"]).base_id for r in self._daily_candidates(start)}
        recent.update(recent_ids)
        # Today's already assigned recent papers still count on a same-day rerun.
        recent.update(key for key in self._daily_paper_ids() if key in self.state["papers"]
                      and start <= parse_time(self.state["papers"][key]["paper"]["published"]) <= self.now)
        return min(self.config["limits"]["daily_paper_limit"], max(5, ((len(recent) + 4) // 5) * 5))

    def _collect_daily(self, window: dict) -> CollectionResult:
        reserved = self._daily_paper_ids()
        remaining = self.config["limits"]["daily_paper_limit"] - len(reserved)
        if remaining <= 0:
            return CollectionResult()
        excluded = set(reserved) | {key for key, r in self.state["papers"].items() if confirmed(r)}
        result = self.collector.collect({**window, "known_ids": list(self.state["papers"]),
                                         "excluded_ids": list(excluded), "limit": remaining})
        recent = {Paper.from_dict(r["paper"]).base_id for r in self._daily_candidates(parse_time(window["start"]))
                  if Paper.from_dict(r["paper"]).base_id not in excluded}
        recent.update(p.base_id for p in result.papers)
        older_start = self.now - timedelta(days=self.config["collection"]["max_lookback_days"])
        needed = self._daily_target(recent) - len(reserved) - len(recent)
        if result.complete and needed > 0 and older_start < parse_time(window["start"]):
            excluded.update(p.base_id for p in result.papers)
            older = self.collector.collect({**window, "start": iso(older_start), "end": window["start"],
                                           "known_ids": list(self.state["papers"]),
                                           "excluded_ids": list(excluded), "limit": needed})
            result.papers.extend(older.papers)
            result.shards.extend(older.shards)
            result.errors.extend(older.errors)
            result.complete = older.complete
            window["start"] = window["initial_floor"] = iso(older_start)
        return result

    def _allocate_daily(self) -> None:
        if self.config["selection"]["mode"] != "rules":
            return
        ids = self._daily_paper_ids()
        records = self._daily_candidates()
        target = self._daily_target()
        for record in records:
            key = Paper.from_dict(record["paper"]).base_id
            if key not in ids and len(ids) < target:
                ids.append(key)
        self.daily_ids = set(ids[:target]).intersection(Paper.from_dict(r["paper"]).base_id for r in records)
        self.checkpoint()

    def _eligible(self, record: dict) -> bool:
        paper = Paper.from_dict(record["paper"])
        if self.daily_ids is not None and paper.base_id not in self.daily_ids:
            return False
        if self.retry_ids and paper.base_id not in self.retry_ids:
            return False
        if paper.base_id in self.promotion_ids:
            return True
        if self._pending(record):
            return False
        if record.get("promotion_pending"):
            return True
        if confirmed(record):
            return False
        recovering = self.daily_ids is None or self._pending(record) or bool(record.get("failures"))
        if recovering and not self._manual_active(record) and self.now - parse_time(record["first_discovered"]) > timedelta(days=self.config["limits"]["recovery_days"]):
            if record.get("stage") not in {"rejected", "delivered"}:
                record["manual_required"] = True
                self.errors.append(f"{paper.version_id}:recovery_age_exhausted")
            return False
        if record.get("manual_required", False):
            self.errors.append(f"{paper.version_id}:manual_recovery_required")
            return False
        return True

    def _process(self) -> None:
        records = [r for r in self.state["papers"].values() if self._eligible(r) and not r.get("resend_pending")]
        if self.daily_ids is not None:
            records.sort(key=lambda r: (parse_time(r["paper"]["published"]), r["paper"]["version_id"]), reverse=True)
        for record in records:
            if record.get("decision"):
                continue
            paper = Paper.from_dict(record["paper"])
            decision = self._stage(record, "selection", lambda: self.analysis.select(paper, record["rule_hits"]))
            if decision is not None:
                record["decision"] = decision.to_dict()
                record["config_fingerprint"] = fingerprint(self.config)
                record["prompt_fingerprint"] = self._prompt_fingerprint("selection")
                record["model"] = getattr(self.analysis, "model_id", self.config["llm"]["filter"]["model"])
                record["stage"] = "rejected" if decision.status == "reject" else "selected" if decision.status == "select" else "uncertain"
                self.checkpoint()
        metadata_ids = set()
        for record in records:
            if record.get("promotion_pending"):
                record["presentation_modules"] = self._modules()
            modules = record.setdefault("presentation_modules", self._modules())
            record.setdefault("presentation_rule_hits", record.get("rule_hits", []).copy())
            if "llm_summary" in modules or "evidence" in modules:
                continue
            if record.get("resend_pending") or record.get("decision", {}).get("status") not in {"select", "uncertain"}:
                continue
            if record["decision"]["status"] == "uncertain":
                self.notes.append(f"{record['paper']['version_id']}：摘要终筛待确认，未进行全文复审。")
            record["intended_tier"] = "translation"  # Persisted metadata tier; does not require translation.
            record["stage"] = "ready"
            metadata_ids.add(record["paper"]["version_id"])
            self.checkpoint()
        records = [r for r in records if r["paper"]["version_id"] not in metadata_ids]
        daily = self._daily()
        uncertain = [r for r in records if r.get("decision", {}).get("status") == "uncertain"]
        uncertain.sort(key=lambda r: (-r["decision"].get("score", 0), r["paper"]["version_id"]))
        for record in uncertain:
            key = Paper.from_dict(record["paper"]).base_id
            manual_reuse = key in self.retry_ids and self.retry_stage in {"review","body"} and key in daily["review_ids"]
            if not manual_reuse and (key in daily["review_ids"] or len(daily["review_ids"]) >= self.config["limits"]["review_daily_limit"]):
                continue
            if not manual_reuse:
                daily["review_ids"].append(key)
            self.checkpoint()
            body = self._body(record)
            if body is None:
                continue
            self._bind_analysis_cache(record)
            decision = self._stage(record, "review", lambda: self.analysis.review(Paper.from_dict(record["paper"]), body, Decision.from_dict(record["decision"]), evidence="evidence" in record["presentation_modules"]))
            if decision is not None:
                record["decision"] = decision.to_dict()
                record["review_prompt_fingerprint"] = self._prompt_fingerprint("review")
                record["stage"] = "rejected" if decision.status == "reject" else "selected" if decision.status == "select" else "uncertain"
                self.checkpoint()
        selected = [r for r in records if r.get("decision", {}).get("status") == "select"]
        priorities = {route: index for index, route in enumerate(self.config["selection"]["route_priority"])}
        selected.sort(key=lambda r: (priorities.get(r["decision"]["route"], 9), -r["decision"].get("score", 0), r["paper"]["version_id"]))
        for record in selected:
            paper = Paper.from_dict(record["paper"])
            promotion = paper.base_id in self.promotion_ids or record.get("promotion_pending", False)
            tier = "full" if promotion else record.get("intended_tier", record["decision"]["tier"])
            if tier == "translation":
                tier = record["decision"]["tier"]
            if tier == "full" and not self._full_slot(paper.base_id):
                if record.get("summary") or any(stage != "title_translation" for stage in record.get("failures", {})) or promotion:
                    continue  # Recoverable full work is not quota overflow.
                tier = "light"
                record["tier_reason"] = "full_quota_overflow"
            record["intended_tier"] = tier
            if tier == "light":
                record["stage"] = "ready"
                self.checkpoint()
                continue
            self.checkpoint()  # Reserve shared daily full slot before summary.
            if record.get("summary"):
                record["stage"] = "summary_validated"
                continue
            body = self._body(record)
            if body is None:
                daily["full_ids"].remove(paper.base_id)
                self.checkpoint()
                continue
            self._bind_analysis_cache(record)
            def summarize():
                result = self.analysis.summarize(paper, body, evidence="evidence" in record["presentation_modules"])
                validate_summary(result, body, require_evidence="evidence" in record["presentation_modules"])
                return result
            summary = self._stage(record, "summary", summarize)
            if summary is not None:
                record["summary"] = summary.to_dict()
                record["body"] = body_metadata(body)
                record["stage"] = "summary_validated"
                record["summary_model"] = self.config["llm"]["summary"]["model"]
                record["summary_prompt_fingerprint"] = self._prompt_fingerprint("summary")
                record["summary_validated_at"] = iso(self.now)
                self.checkpoint()
            else:
                daily["full_ids"].remove(paper.base_id)
                self.checkpoint()

    def _modules(self) -> list[str]:
        return [key for key, enabled in self.config["presentation"].items() if enabled]

    def _index_titles(self) -> None:
        if (not self.config["presentation"]["title_translation"]
                and self.retry_stage != "title_translation"):
            return
        # Transport-only manual actions reuse content without new paid calls.
        if self.resend_ids or self.promotion_ids or (self.retry_ids and self.retry_stage != "title_translation"):
            return
        for key, record in self.state["papers"].items():
            if self.daily_ids is not None and key not in self.daily_ids:
                continue
            if self.retry_ids and key not in self.retry_ids:
                continue
            if record.get("title_zh") or record.get("translation", {}).get("title_zh"):
                continue
            paper = Paper.from_dict(record["paper"])
            result = self._stage(record, "title_translation", lambda: self.translator.translate(paper, title_only=True))
            if result is not None:
                record["title_zh"] = result.title_zh
                record["title_translation_prompt_fingerprint"] = self._prompt_fingerprint("title_translation") if self.config["translation"]["provider"] == "llm" else self.config["translation"]["provider"]
                self.checkpoint()

    def _prepare_presentation(self) -> None:
        for record in self.state["papers"].values():
            if not self._eligible(record) or record.get("resend_pending"):
                continue
            if record.get("stage") not in {"ready", "summary_validated"}:
                continue
            modules = record.setdefault("presentation_modules", self._modules())
            abstract_needed = "abstract_translation" in modules
            title_needed = "title_translation" in modules
            translated = record.get("translation", {})
            if (abstract_needed and not translated.get("abstract_zh")) or (
                title_needed and not (translated.get("title_zh") or record.get("title_zh"))
            ):
                stage = "translation" if abstract_needed else "title_translation"
                paper = Paper.from_dict(record["paper"])
                def translate():
                    result = self.translator.translate(paper, title_only=not abstract_needed)
                    if not isinstance(result, Translation) or (abstract_needed and not result.abstract_zh.strip()):
                        raise ProcessingError("translation_abstract_missing")
                    return result
                result = self._stage(record, stage, translate)
                if result is None:
                    record["stage"] = "presentation_pending"
                    self.checkpoint()
                    continue
                if abstract_needed:
                    record["translation"] = result.to_dict()
                record["title_zh"] = result.title_zh
                record["translation_provider"] = self.config["translation"]["provider"]
                record[stage + "_prompt_fingerprint"] = self._prompt_fingerprint(stage) if self.config["translation"]["provider"] == "llm" else self.config["translation"]["provider"]
            record["presentation_modules"] = modules.copy()
            self.checkpoint()

    @staticmethod
    def _item(record: dict, recovery_of="") -> DigestItem:
        tier = record["intended_tier"]
        return DigestItem(Paper.from_dict(record["paper"]), tier,
                          Summary.from_dict(record["summary"]) if tier == "full" else None,
                          recovery_of, Translation.from_dict(record["translation"]) if record.get("translation") else
                          Translation(record["title_zh"]) if record.get("title_zh") else None,
                          record.get("presentation_modules",
                              ["title", "source", "llm_summary"] if tier == "full" else
                              ["title", "title_translation", "abstract", "abstract_translation", "source"] if tier == "translation" else
                              ["title", "source"]), record.get("presentation_rule_hits", record.get("rule_hits", [])).copy())

    def _recover_items(self) -> list[DigestItem]:
        result = []
        for key, record in self.state["papers"].items():
            if self.daily_ids is not None and key not in self.daily_ids:
                continue
            if self.promotion_ids and key not in self.promotion_ids:
                continue
            if self.resend_ids and key not in self.resend_ids:
                continue
            if self.retry_ids and key not in self.retry_ids:
                continue
            previous = record.get("pending_digest")
            if not previous:
                continue
            digest = self.state["digests"][previous]
            if digest.get("run_id") == self.run_id:
                continue
            chain = self.state["chains"][record.get("delivery_chain", digest["chain_root"])]
            if chain["attempts"] >= self._chain_limit(record):
                record["manual_required"] = True
                self.errors.append(f"{key}:delivery_attempts_exhausted")
                continue
            if not self._manual_active(record) and self.now - parse_time(chain["created_at"]) > timedelta(days=self.config["limits"]["recovery_days"]):
                record["manual_required"] = True
                self.errors.append(f"{key}:delivery_recovery_age_exhausted")
                continue
            tier = record["intended_tier"]
            if tier == "full" and not self._full_slot(key):
                continue
            result.append(self._item(record, previous))
        return result

    def _send(self, items: list[DigestItem], *, alert=False, empty=False) -> str | None:
        if not items and not alert and not empty:
            return None
        attempt_time = self.clock()
        current_day = attempt_time.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        if current_day != self.day:
            self.day = current_day
        if self.daily_ids is not None:
            ids = self._daily_paper_ids()
            allowed = []
            for item in items:
                key = item.paper.base_id
                if key not in ids and len(ids) < self.config["limits"]["daily_paper_limit"]:
                    ids.append(key)
                if key in ids[:self.config["limits"]["daily_paper_limit"]]:
                    allowed.append(item)
            items = allowed
            items.sort(key=lambda i: (parse_time(i.paper.published), i.paper.base_id), reverse=True)
        items = [i for i in items if i.tier != "full" or self._full_slot(i.paper.base_id)]
        if not items and not alert and not empty:
            return None
        base = self.config["archive"]["public_base_url"].rstrip("/")
        if self.real_send and (not base or self.store.publisher is None):
            raise ProcessingError("real_delivery_requires_public_archive_and_checkpoint_publisher")
        base = base or "https://example.invalid/arxivdaily-state"
        notes = self.notes + ([f"本次存在 {len(set(self.errors))} 项处理或覆盖问题，状态中保留待恢复阶段。"] if self.errors else [])
        notes.append(f"[所有候选论文的长期索引]({base}/archive/papers.md)")
        offline = hasattr(self.analysis, "fixture")
        if offline:
            notes.append("脚本化离线运行：未调用真实模型或发送服务。")
        elif not self.real_send:
            notes.append("实际模型分析的 dry-run：未发送真实通知。")
        if any(self.state["papers"][i.paper.base_id].get("resend_pending") for i in items):
            notes.append("本批包含显式请求的历史内容重投，复用原验证结果。")
        markdown = render_archive(self.day, items, notes=notes) if not alert else f"# ArxivDaily 运行告警 · {self.day}\n\n" + "\n".join(f"- {e}" for e in sorted(set(self.errors))) + "\n"
        markdown = self.store.scrubber.text(markdown)
        digest_id = f"{self.run_id}-{'alert-' if alert else ''}{fingerprint(markdown)[:12]}"
        relative = f"archive/{self.day.replace('-', '/')}/{digest_id}.md"
        url = base + "/" + relative
        notification, modes = render_notification(self.day, items, url, self.config["delivery"]["body_bytes"], index_url=base + "/archive/papers.md") if not alert else (f"ArxivDaily 本次运行有失败，请查看[状态与告警]({url})。", {})
        notification = self.store.scrubber.text(notification)
        previous = sorted({i.recovery_of for i in items if i.recovery_of})
        recovery_roots = {self.state["papers"][i.paper.base_id].get("delivery_chain", self.state["digests"][i.recovery_of]["chain_root"]) for i in items if i.recovery_of}
        new_items = [i for i in items if not i.recovery_of]
        # This snapshot has its own root only when there are new entries (or an
        # alert). Recovery transmissions also increment every original root.
        if new_items or alert or empty:
            self.state["chains"][digest_id] = {"attempts": 0, "created_at": iso(self.now)}
            recovery_roots.add(digest_id)
        chain_root = digest_id if new_items or alert or empty else sorted(recovery_roots)[0]
        manifest = {"schema_version": 1, "digest_id": digest_id, "day": self.day, "run_id": self.run_id, "items": [{"paper": i.paper.to_dict(), "tier": i.tier, "summary": i.summary.to_dict() if i.summary else None, "translation": i.translation.to_dict() if i.translation else None, "modules": i.modules, "rule_hits": i.rule_hits, "recovery_of": i.recovery_of} for i in items], "rendered": modes, "archive_url": url, "content_hash": fingerprint(markdown), "notification_hash": fingerprint(notification), "recovery_of": previous, "chain_roots": sorted(recovery_roots), "alert": alert, "empty": empty}
        self.store.snapshot(self.day, digest_id, markdown, notification, manifest)
        self.state["digests"][digest_id] = {**manifest, "chain_root": chain_root, "created_at": iso(self.now), "status": "prepared"}
        self.checkpoint()  # Publisher must commit and verify the archive first.
        if self.delivery is None:
            return digest_id
        attempt_time = self.clock()
        if attempt_time.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() != self.day:
            self.errors.append("delivery_day_changed_during_archive_publish")
            self.state["digests"][digest_id]["status"] = "deferred"
            self.checkpoint()
            return digest_id
        quota_day = attempt_time.astimezone(ZoneInfo(self.config["delivery"]["quota_timezone"])).date().isoformat()
        quota = self._daily(quota_day)
        poll_reserve = self.config["delivery"]["poll_limit"] if self.config["delivery"].get("query_counts_quota") else 0
        required = 1 + poll_reserve
        if quota["send_attempts"] + quota["query_reservations"] + required > self.config["delivery"]["daily_limit"]:
            self.errors.append("delivery_daily_quota_exhausted")
            self.state["digests"][digest_id]["status"] = "deferred"
            self.checkpoint()
            return digest_id
        for root in recovery_roots:
            limits = [self._chain_limit(self.state["papers"][i.paper.base_id]) for i in items if i.recovery_of and self.state["papers"][i.paper.base_id].get("delivery_chain") == root]
            if self.state["chains"][root]["attempts"] >= max(limits or [self.config["delivery"]["chain_attempts"]]):
                raise ProcessingError("delivery_chain_attempts_exhausted")
        for root in recovery_roots:
            self.state["chains"][root]["attempts"] += 1
        quota["send_attempts"] += 1
        quota["query_reservations"] += poll_reserve
        digest = self.state["digests"][digest_id]
        digest.update({"status": "sending", "attempt_started": iso(attempt_time), "quota_day": quota_day, "attempt_run": self.run_id})
        if alert:
            self._daily()["alerts"].append(digest_id)
        elif new_items:
            self._daily()["normal_digests"].append(digest_id)
        for item in items:
            record = self.state["papers"][item.paper.base_id]
            record["_pre_attempt_chain"] = record.get("delivery_chain")
            if not item.recovery_of:
                record["delivery_chain"] = digest_id
            record["pending_digest"] = digest_id
            previous_kind = next((e["kind"] for e in reversed(record["events"]) if e["digest_id"] == item.recovery_of), "normal") if item.recovery_of else "normal"
            kind = "resend" if record.get("resend_pending") else "promotion" if item.paper.base_id in self.promotion_ids or record.get("promotion_pending") else previous_kind
            record["events"].append({"digest_id": digest_id, "kind": kind, "tier": item.tier, "rendered": modes[item.paper.base_id], "status": "sending", "at": iso(attempt_time), "recovery_of": item.recovery_of})
        self.checkpoint()  # Quota and attempt_started durable BEFORE POST.
        send_time = self.clock()
        if send_time.astimezone(ZoneInfo(self.config["delivery"]["quota_timezone"])).date().isoformat() != quota_day or send_time.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() != self.day:
            # The publisher can cross midnight. No POST has occurred, so
            # release the reservation and leave the already validated work.
            quota["send_attempts"] -= 1
            quota["query_reservations"] -= poll_reserve
            for root in recovery_roots:
                self.state["chains"][root]["attempts"] -= 1
            if alert:
                self._daily()["alerts"].remove(digest_id)
            elif new_items:
                self._daily()["normal_digests"].remove(digest_id)
            for item in items:
                record = self.state["papers"][item.paper.base_id]
                record["events"].pop()
                if item.recovery_of:
                    record["pending_digest"] = item.recovery_of
                else:
                    record.pop("pending_digest", None)
                prior_chain = record.pop("_pre_attempt_chain", None)
                if prior_chain:
                    record["delivery_chain"] = prior_chain
                else:
                    record.pop("delivery_chain", None)
            digest["status"] = "deferred"
            digest.pop("attempt_started", None)
            self.errors.append("delivery_day_changed_during_attempt_publish")
            self.checkpoint()
            return digest_id
        for item in items:
            self.state["papers"][item.paper.base_id].pop("_pre_attempt_chain", None)
        try:
            outcome = self.delivery.send("ArxivDaily 运行告警" if alert else server_title(self.day, self.config["translation"].get("label", "")), notification)
        except Exception:
            outcome = DeliveryResult("unknown", detail="transport_exception")
        status = "unknown" if outcome.status == "queued" else outcome.status
        digest.update({"status": status, "delivery": outcome.to_dict(), "finished_at": iso(self.clock())})
        for item in items:
            record = self.state["papers"][item.paper.base_id]
            record["events"][-1]["status"] = status
            if status == "confirmed":
                record.pop("pending_digest", None)
                record.pop("promotion_pending", None)
                record.pop("resend_pending", None)
                record["stage"] = "delivered"
            elif status == "failed":
                record["stage"] = "delivery_failed"
        if status in {"unknown", "failed"}:
            self.errors.append(f"delivery_{status}")
        self.checkpoint()
        return digest_id

    def run(self, papers: list[Paper] | None = None, *, manual_start=None, manual_end=None, promotion_ids=(), resend_ids=(), retry_stage=None, retry_ids=()) -> dict:
        self.promotion_ids = set(promotion_ids)
        self.resend_ids = set(resend_ids)
        self.retry_ids = set(retry_ids)
        self.retry_stage = retry_stage
        with self.store.locked():
            self.state = self.store.load()
            if retry_stage:
                if retry_stage not in {"selection","body","review","summary","translation","title_translation","delivery"} or not self.retry_ids:
                    raise ProcessingError("invalid_manual_recovery_request")
                for key in self.retry_ids:
                    record = self.state["papers"].get(key)
                    if record is None or (retry_stage != "title_translation" and confirmed(record) and not (record.get("promotion_pending") or record.get("resend_pending"))):
                        raise ProcessingError("manual_recovery_requires_undelivered_existing_record")
                    if retry_stage == "delivery" and not record.get("pending_digest"):
                        raise ProcessingError("manual_delivery_requires_pending_snapshot")
                    record.setdefault("manual_retry_events",[]).append({"stage":retry_stage,"run_id":self.run_id,"at":iso(self.now),"prior_attempts":dict(record["attempts"])})
                    record["manual_retry_until"] = iso(self.now+timedelta(days=self.config["limits"]["recovery_days"]))
                    record.pop("manual_required",None)
                    if retry_stage == "delivery":
                        chain = self.state["chains"][record["delivery_chain"]]
                        record["manual_delivery_attempt_limit"] = chain["attempts"]+self.config["delivery"]["chain_attempts"]
                    else:
                        if record.get("pending_digest") and retry_stage != "title_translation":
                            raise ProcessingError("pending_delivery_must_recover_delivery_only")
                        if not record.get("failures",{}).get(retry_stage) and not record["attempts"].get(retry_stage):
                            raise ProcessingError("manual_retry_requires_previously_attempted_stage")
                        record["attempts"][retry_stage] = 0
                        record["failures"].pop(retry_stage,None)
                        if retry_stage in {"summary","review"}:
                            record["attempts"]["body"] = 0
            for digest in self.state["digests"].values():
                if digest["status"] == "sending":
                    digest["status"] = "unknown"
            for record in self.state["papers"].values():
                for event in record["events"]:
                    if event["status"] == "sending":
                        event["status"] = "unknown"
            run = {"run_id": self.run_id, "started_at": iso(self.now), "day": self.day, "config_fingerprint": fingerprint(self.config), "coverage_complete": papers is not None}
            self.state["runs"].append(run)
            self.checkpoint()
            if papers is None:
                window = plan_window(self.now, self.state, manual_start, manual_end, initial_days=self.config["collection"]["initial_days"], lookback_days=self.config["collection"]["lookback_days"])
                self.state["collection"].setdefault("initial_floor", window["initial_floor"])
                run["window"] = window
                if window["uncovered"] and self.config["selection"]["mode"] != "rules":
                    self.notes.append("自动补采之外存在未覆盖区间，请使用 --start/--end 人工补采。")
                    self.errors.append("collection_gap_outside_14_days")
                self.checkpoint()
                try:
                    result = (self._collect_daily(window) if self.config["selection"]["mode"] == "rules" and not window["manual"]
                              else self.collector.collect({**window,"known_ids":list(self.state["papers"])}))
                except PersistenceError:
                    raise
                except Exception as exc:
                    result = CollectionResult(complete=False, errors=[f"collector_failed:{type(exc).__name__}"])
                papers = result.papers
                run.update({"coverage_complete": result.complete, "shards": result.shards, "collection_errors": result.errors})
                self.errors.extend(f"collection:{e}" for e in result.errors)
                if not result.complete:
                    self.errors.append("collection_incomplete")
                else:
                    if not window.get("manual"):
                        self.state["collection"]["last_complete_end"] = window["end"]
            discovered_ids = set()
            for paper in papers:
                hits = match_rules(paper, self.config)
                if hits or paper.base_id in self.promotion_ids:
                    upsert_paper(self.state, paper, self.now, hits)
                    discovered_ids.add(paper.base_id)
            run["discovered_papers"] = len({p.base_id for p in papers})
            run["discovered_candidates"] = len(discovered_ids)
            self.checkpoint()  # Collection complete checkpoint independent of LLM.
            self._allocate_daily()
            for key in self.promotion_ids:
                if key not in self.state["papers"]:
                    raise ProcessingError("promotion_requires_existing_record")
                record = self.state["papers"][key]
                if record.get("decision", {}).get("status") != "select":
                    raise ProcessingError("promotion_requires_selected_paper")
                if record.get("pending_digest"):
                    raise ProcessingError("promotion_requires_no_pending_delivery")
                events = record["events"]
                if not any(e["status"] == "confirmed" and e["tier"] == "light" for e in events) or any(e["status"] == "confirmed" and e["tier"] == "full" for e in events):
                    raise ProcessingError("promotion_requires_confirmed_light_only; use --resend for existing full content")
                record["intended_tier"] = "full"
                record["promotion_pending"] = True
                record.pop("manual_required", None)
            for key in self.resend_ids:
                record = self.state["papers"].get(key)
                if record is None or not confirmed(record) or record.get("pending_digest"):
                    raise ProcessingError("resend_requires_existing_confirmed_content_without_pending_delivery")
                if record["intended_tier"] == "full" and not record.get("summary"):
                    raise ProcessingError("resend_requires_validated_summary")
                record["resend_pending"] = True
                record["stage"] = "summary_validated" if record["intended_tier"] == "full" else "ready"
            # Reserve recovery slots before new summaries: all share one day.
            recoveries = [] if retry_stage == "title_translation" else self._recover_items()
            try:
                if retry_stage != "title_translation":
                    self._process()
                    self._prepare_presentation()
                self._index_titles()
            except PersistenceError:
                raise
            except ProcessingError as exc:
                self.errors.append(self.store.scrubber.text(str(exc)))
            self.day = self.clock().astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            recovered_ids = {i.paper.base_id for i in recoveries}
            new = []
            normal_already_sent = self.daily_ids is None and any(self.state["digests"][d]["status"] == "confirmed" for d in self._daily()["normal_digests"])
            for key, record in self.state["papers"].items():
                if self.daily_ids is not None and key not in self.daily_ids:
                    continue
                if retry_stage == "title_translation":
                    continue
                if self.promotion_ids and key not in self.promotion_ids:
                    continue
                if self.resend_ids and key not in self.resend_ids:
                    continue
                if self.retry_ids and key not in self.retry_ids:
                    continue
                promotion = key in self.promotion_ids or record.get("promotion_pending", False) or record.get("resend_pending",False)
                if key in recovered_ids or record.get("pending_digest") or (confirmed(record) and not promotion):
                    continue
                if normal_already_sent and not promotion:
                    continue
                if record.get("stage") not in {"ready", "summary_validated"}:
                    continue
                tier = record["intended_tier"]
                if tier == "full" and not self._full_slot(key):
                    continue
                new.append(self._item(record))
            digest_id = self._send(recoveries + new)
            if not digest_id and self.errors and not self._daily()["alerts"]:
                digest_id = self._send([], alert=True)
            waiting_ready = any(r.get("stage") in {"ready", "summary_validated"} and not confirmed(r)
                                for key, r in self.state["papers"].items()
                                if self.daily_ids is None or key in self.daily_ids)
            if not digest_id and not waiting_ready and not self.errors and run["coverage_complete"] and not (self.promotion_ids or self.resend_ids or self.retry_ids or manual_start or manual_end) and (self.daily_ids is None or not self._daily_paper_ids()):
                digest_id = self._send([], empty=True)
            actual = self.state["digests"][digest_id]["items"] if digest_id else []
            digest = self.state["digests"].get(digest_id,{})
            run.update({"completed_at": iso(self.clock()), "delivery_day": self.day, "digest_id": digest_id, "delivery_status": digest.get("status","none"), "delivery_kind": "alert" if digest.get("alert") else "empty" if digest.get("empty") else "digest" if digest_id else "none", "errors": sorted(set(self.errors)), "full": sum(i["tier"] == "full" for i in actual), "light": sum(i["tier"] == "light" for i in actual), "translation": sum(i["tier"] == "translation" for i in actual), "recovery": sum(bool(i.get("recovery_of")) for i in actual), "silent": not digest_id and not self.errors})
            self.checkpoint()
            return self.store.scrubber.clean(run)
