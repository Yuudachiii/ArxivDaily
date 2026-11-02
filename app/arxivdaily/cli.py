"""Explicit offline/live entrypoints. Credentials are read only from configured env names."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import uuid

from .collection import Collector
from .config import default_config_path, load_config
from .delivery import MockDelivery, ServerChan
from .fulltext import FulltextReader
from .llm import AnalysisClient
from .models import ProcessingError, normalize_id, parse_time
from .pipeline import Pipeline
from .prompts import load_prompts, prompt_fingerprint
from .rules import match_rules
from .rulesmode import RulesAnalysis
from .simulation import FixtureAnalysis, FixtureReader, load_fixture
from .state import SecretScrubber, StateStore, command_publisher


PROVIDERS = ["llm", "deepl", "libretranslate", "google_free", "microsoft_free"]


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="ArxivDaily: 摘要规则、两角色终筛、正文证据与每日简报")
    subs = result.add_subparsers(dest="command", required=True)
    check = subs.add_parser("config", help="check effective configuration and prompts without external requests")
    check.add_argument("--config", default=None)
    check.add_argument("--show", action="store_true", help="print all effective settings")
    check.add_argument("--abstract", default=None, help="preview keyword rule hits for this abstract")
    check.add_argument("--title", default="")
    check.add_argument("--categories", nargs="*", default=None)
    for command in ("run", "simulate", "replay"):
        p = subs.add_parser(command)
        p.add_argument("--config", default=None)
        p.add_argument("--translation-provider", choices=PROVIDERS, default=None)
        p.add_argument("--state-dir", default=".state")
        p.add_argument("--fixture", default=None)
        p.add_argument("--live", action="store_true", help="enable actual arXiv/fulltext/paid model requests")
        p.add_argument("--send", action="store_true", help="explicit actual delivery; implies --live")
        p.add_argument("--checkpoint-command", default=None, help="synchronous publisher command or JSON argv")
        p.add_argument("--report", default="tmp/reports/run.json")
        p.add_argument("--now", default=None, help="timezone-aware fixture clock (offline only)")
        p.add_argument("--mock-status", choices=["confirmed", "failed", "unknown", "queued"], default="confirmed")
        p.add_argument("--promote", nargs="*", default=[], help="explicit promotion of existing selected light records; requires --send")
        p.add_argument("--resend", nargs="*", default=[], help="explicitly resend existing confirmed content; requires --send")
        p.add_argument("--retry-stage", choices=["selection","body","review","summary","translation","title_translation","delivery"], default=None, help="explicit bounded manual recovery; default is an isolated dry run")
        p.add_argument("--retry-ids", nargs="*", default=[])
        p.add_argument("--start", default=None)
        p.add_argument("--end", default=None)
        p.add_argument("--ids", nargs="*", default=[])
    return result


def check_configuration(args) -> int:
    try:
        path = Path(args.config).resolve() if args.config else default_config_path()
        config = load_config(path)
        prompts = load_prompts(config)
        output = {"status": "ok", "config": str(path),
                  "presentation_modules": [key for key, enabled in config["presentation"].items() if enabled],
                  "enabled_routes": [name for name, clauses in config["rules"]["routes"].items() if clauses],
                  "prompt_fingerprints": {task: prompt_fingerprint(prompts, task) for task in prompts},
                  "external_requests": False}
        if args.show:
            output["effective_config"] = config
        if args.abstract is not None:
            from .models import Paper
            paper = Paper("2501.00001v1", args.title or "Rule preview", args.abstract,
                          args.categories if args.categories is not None else config["rules"]["categories"],
                          "2025-01-01T00:00:00+00:00", "2025-01-01T00:00:00+00:00")
            output["rule_hits"] = match_rules(paper, config)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 2


def _translation_key_names(config: dict) -> list[str]:
    provider = config["translation"]["provider"]
    return {
        "llm": [config["llm"]["filter"]["api_key_env"]],
        "deepl": [config["translation"]["api_key_env"]],
        "libretranslate": [],
        "google_free": [],
        "microsoft_free": [],
    }.get(provider, [])


def required_credentials(config: dict, *, retry_stage: str | None = None, resend_ids=(), promotion_ids=()) -> list[str]:
    """Return the credential env names the requested --send path actually uses."""
    sendkey = config["delivery"]["sendkey_env"]
    if retry_stage == "delivery" or resend_ids:
        return [sendkey]
    mode = config["selection"]["mode"]
    needs_translation = config["presentation"]["title_translation"] or config["presentation"]["abstract_translation"]
    translation_keys = _translation_key_names(config) if needs_translation else []
    if retry_stage in {"translation", "title_translation"}:
        return sorted({sendkey, *translation_keys})
    if retry_stage in {"body", "review", "summary"} or promotion_ids:
        if mode == "rules":
            raise ValueError("selection.mode = rules cannot run the LLM summary stages")
        return sorted({config["llm"]["summary"]["api_key_env"], sendkey})
    needs_fulltext = config["presentation"]["llm_summary"] or config["presentation"]["evidence"]
    if mode == "rules":
        if needs_fulltext:
            raise ValueError("selection.mode = rules does not support llm_summary or evidence")
        return sorted({sendkey, *translation_keys})
    names = {config["llm"]["filter"]["api_key_env"], sendkey, *translation_keys}
    if needs_fulltext:
        names.add(config["llm"]["summary"]["api_key_env"])
    return sorted(names)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if args.command == "config":
        return check_configuration(args)
    app_dir = Path(__file__).resolve().parents[1]
    report_path = Path(args.report).resolve()
    scrubber = SecretScrubber()
    report = {}
    code = 0
    try:
        config = load_config(args.config if args.config else default_config_path())
        load_prompts(config)  # Fail before collection or paid requests if a prompt is missing.
        if args.translation_provider:
            config["translation"]["provider"] = args.translation_provider
        mode = config["selection"]["mode"]
        if mode == "rules" and config["translation"]["provider"] == "llm":
            # Rules mode must never reach a model.  The committed config keeps
            # provider="llm" as the legacy/test default, so live rules runs fall
            # back to the keyless Google endpoint unless one is chosen explicitly.
            if args.translation_provider == "llm":
                raise ValueError("translation provider llm is unavailable when selection.mode = rules; use google_free or microsoft_free")
            config["translation"]["provider"] = "google_free"
        names = [config["delivery"]["sendkey_env"]]
        if mode == "llm":
            names += [config["llm"][r]["api_key_env"] for r in ("filter", "summary")]
        names.append(config["translation"]["api_key_env"])
        secrets = [os.environ.get(n, "") for n in names]
        scrubber = SecretScrubber(secrets)
        live = args.live or args.send
        if args.command == "simulate" and live:
            raise ValueError("simulate cannot call live services")
        if live and args.now:
            raise ValueError("fixture clock cannot be used for live service quota accounting")
        if bool(args.start) != bool(args.end):
            raise ValueError("both --start and --end are required for backfill")
        if (args.promote or args.resend) and not args.send:
            raise ValueError("promotion or resend requires explicit --send")
        needs_fulltext = config["presentation"]["llm_summary"] or config["presentation"]["evidence"]
        if mode == "rules" and needs_fulltext:
            raise ValueError("selection.mode = rules does not support llm_summary or evidence")
        if args.promote and not needs_fulltext:
            raise ValueError("fulltext promotion requires llm_summary or evidence")
        if sum(bool(x) for x in (args.promote,args.resend,args.retry_stage)) > 1:
            raise ValueError("choose only one of --promote, --resend, or --retry-stage")
        ids = [i for group in args.ids for i in group.split(",") if i]
        promotion_ids = [normalize_id(i)[0] for i in args.promote]
        resend_ids = [normalize_id(i)[0] for i in args.resend]
        retry_ids = [normalize_id(i)[0] for group in args.retry_ids for i in group.split(",") if i]
        if bool(args.retry_stage) != bool(retry_ids) or (args.retry_stage and args.command != "run"):
            raise ValueError("manual recovery requires run --retry-stage STAGE --retry-ids ID...")
        if args.command == "replay" and not ids:
            raise ValueError("replay requires --ids")
        if args.command != "replay" and ids:
            raise ValueError("use replay for independent paper IDs")
        publisher = command_publisher(args.checkpoint_command) if args.send and args.checkpoint_command else None
        if args.send and (publisher is None or not config["archive"]["public_base_url"]):
            raise ValueError("--send requires public_base_url and --checkpoint-command")
        if args.send:
            required_names = required_credentials(config, retry_stage=args.retry_stage, resend_ids=resend_ids, promotion_ids=promotion_ids)
            missing = sorted({name for name in required_names if not os.environ.get(name)})
            if missing:
                raise ValueError("missing required credential environment variables: " + ", ".join(missing))
        # Dry runs use their own space even if the normal state-dir was supplied.
        isolated = not args.send and args.command != "simulate"
        if args.command == "simulate":
            state_dir = app_dir / "runs" / "simulation" if args.state_dir == ".state" else Path(args.state_dir).resolve()
            if not state_dir.is_relative_to(app_dir / "runs"):
                raise ValueError("simulation state must be under app/runs/, separate from deployment state")
        else:
            state_dir = app_dir / "runs" / ("dry-" + uuid.uuid4().hex) if isolated else Path(args.state_dir).resolve()
        if not state_dir.is_relative_to(app_dir):
            raise ValueError("state directory must be inside app/ to keep temporary files in the workspace")
        store = StateStore(state_dir, secrets=secrets, publisher=publisher)
        if args.retry_stage and isolated:
            original_dir = Path(args.state_dir).resolve()
            if not original_dir.is_relative_to(app_dir) or not (original_dir / "state.json").is_file():
                raise ValueError("manual recovery requires an existing state.json inside app/")
            store.save(StateStore(original_dir,secrets=secrets).load())
        papers = None
        if live:
            collector = Collector(config)
            analysis = RulesAnalysis(config) if mode == "rules" else AnalysisClient(config)
            reader = FulltextReader(config, app_dir / "tmp" / "fulltext")
            if ids:
                papers = collector.by_ids(ids)
            delivery = ServerChan(config) if args.send else None
        else:
            fixture_path = Path(args.fixture).resolve() if args.fixture else app_dir / "fixtures" / ("representative.json" if args.command == "replay" else "demo.json")
            fixture, papers = load_fixture(fixture_path)
            if ids:
                wanted = {normalize_id(i)[0] for i in ids}
                papers = [p for p in papers if p.base_id in wanted]
                if {p.base_id for p in papers} != wanted:
                    raise ValueError("requested IDs missing from replay fixture")
                for identifier in ids:
                    base_id, version_id = normalize_id(identifier)
                    if version_id != base_id and not any(p.version_id == version_id for p in papers):
                        raise ValueError("requested pinned version missing from replay fixture")
            collector = None
            analysis = FixtureAnalysis(fixture)
            reader = FixtureReader(config, fixture, fixture_path, app_dir / "tmp" / "fixture-fulltext")
            delivery = MockDelivery(args.mock_status)
        now = parse_time(args.now) if args.now else None
        if args.retry_stage or resend_ids or promotion_ids:
            papers = []
        report = Pipeline(config, store, collector, analysis, reader, delivery, now=now, real_send=args.send).run(papers, manual_start=args.start, manual_end=args.end, promotion_ids=promotion_ids, resend_ids=resend_ids, retry_stage=args.retry_stage, retry_ids=retry_ids)
        report.update({"mode": "live" if live else "scripted_offline", "real_delivery": args.send, "state_dir": str(state_dir), "dry_run_isolated": isolated, "selection_mode": config["selection"]["mode"], "translation_provider": config["translation"]["provider"], "model_calls": getattr(analysis, "calls", None), "validation_scope": "scripted fixture behavior; not actual provider quality" if not live else "actual provider requests; consult stage status"})
        code = 1 if report.get("errors") else 0
    except Exception as exc:
        code = 2
        # Controlled local errors are informative; transport objects never logged.
        detail = str(exc) if isinstance(exc, (ValueError, FileNotFoundError, ProcessingError)) else type(exc).__name__
        report = {"error": scrubber.text(detail)[:240], "real_delivery": bool(args.send), "status": "failed"}
    report = scrubber.clean(report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
