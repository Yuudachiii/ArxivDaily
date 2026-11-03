"""Rules mode: deterministic selection plus translation-tier delivery, no model calls."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arxivdaily.config import default_config_path, load_config
from arxivdaily.collection import CollectionResult
from arxivdaily.delivery import MockDelivery
from arxivdaily.models import Paper, ProcessingError, Translation
from arxivdaily.pipeline import Pipeline
from arxivdaily.render import server_title
from arxivdaily.rulesmode import RulesAnalysis
from arxivdaily.rules import match_rules
from arxivdaily.simulation import FixtureReader, load_fixture
from arxivdaily.state import StateStore, upsert_paper

APP = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 2, 7, 17, tzinfo=timezone.utc)


def _config():
    config = load_config()
    config["selection"]["mode"] = "rules"
    config["translation"]["provider"] = "google_free"
    config["translation"]["label"] = ""
    config["presentation"] = {
        "title": True, "title_translation": True,
        "abstract": True, "abstract_translation": True,
        "llm_summary": False, "evidence": False,
        "source": True, "screening_routes": True,
    }
    return config


class StubTranslator:
    def __init__(self):
        self.calls = 0

    def translate(self, paper, *, title_only=False):
        self.calls += 1
        return Translation("中文标题：" + paper.title, "" if title_only else "中文摘要：" + paper.abstract)


def test_rules_mode_reaches_translation_digest_without_model_calls(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    config = _config()
    fixture, papers = load_fixture(APP / "fixtures" / "demo.json")
    analysis = RulesAnalysis(config)
    translator = StubTranslator()
    reader = FixtureReader(config, fixture, APP / "fixtures" / "demo.json", tmp_path / "raw")
    store = StateStore(tmp_path / "state")
    delivery = MockDelivery("confirmed")
    # A paper outside the categories/keywords must never reach selection.
    unrelated = Paper("2603.19169v1", "Unrelated", "No configured keyword here.", ["hep-th"],
                      "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00")
    report = Pipeline(config, store, None, analysis, reader, delivery, now=NOW, translator=translator).run(papers + [unrelated])
    assert not report["errors"]
    assert report["translation"] == 4 and report["full"] == report["light"] == 0
    assert analysis.calls == {"selection": 4, "review": 0, "summary": 0}
    assert reader.calls == 0 and translator.calls == 4
    assert len(delivery.sent) == 1
    state = store.load()
    for record in state["papers"].values():
        assert record["decision"]["status"] == "select"
        assert record["decision"]["tier"] == "translation"
        assert record["model"] == "rules"
        assert record["translation"]["abstract_zh"]
    assert config["selection"]["mode"] == "rules"
    assert config["translation"]["provider"] == "google_free"


def test_rules_analysis_route_tier_and_model_only_stages(tmp_path):
    config = _config()
    analysis = RulesAnalysis(config)
    paper = Paper("2501.00001v1", "Title", "Abstract", ["cs.CL"],
                  "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00")
    decision = analysis.select(paper, ["llm_training", "llm_core"])
    assert decision.status == "select" and decision.tier == "translation" and decision.route == "llm_training"
    assert decision.score == 0.0
    fallback = analysis.select(paper, [])
    assert fallback.route == "rules"
    for stage in ("review", "summarize", "translate"):
        with pytest.raises(ProcessingError, match="llm_disabled"):
            if stage == "review":
                analysis.review(paper, None, decision)
            elif stage == "summarize":
                analysis.summarize(paper, None)
            else:
                analysis.translate(paper)


def test_label_is_forwarded_to_push_title(tmp_path):
    config = _config()
    config["translation"]["label"] = "谷歌"
    fixture, papers = load_fixture(APP / "fixtures" / "demo.json")
    analysis = RulesAnalysis(config)
    reader = FixtureReader(config, fixture, APP / "fixtures" / "demo.json", tmp_path / "raw")
    store = StateStore(tmp_path / "state")
    delivery = MockDelivery("confirmed")
    Pipeline(config, store, None, analysis, reader, delivery, now=NOW, translator=StubTranslator()).run(papers[:1])
    assert delivery.sent[0][0] == server_title("2026-10-02", "谷歌")


def test_rules_mode_archives_full_detail_within_daily_limit(tmp_path):
    config = load_config(default_config_path())
    config["translation"]["provider"] = "google_free"
    # A tiny full-text quota must be irrelevant: rules mode never uses the full tier.
    config["limits"]["full_daily_limit"] = 1
    papers = [
        Paper(f"2502.{90000 + index}v1", f"Large language model paper {index}",
              f"We train a large language model with instruction tuning and method {index}.",
              ["cs.CL"], "2026-10-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00")
        for index in range(12)
    ]
    analysis = RulesAnalysis(config)
    translator = StubTranslator()
    store = StateStore(tmp_path / "state")
    delivery = MockDelivery("confirmed")
    report = Pipeline(config, store, None, analysis, None, delivery, now=NOW, translator=translator).run(papers)
    assert not report["errors"]
    assert report["translation"] == 12 and report["full"] == report["light"] == 0
    assert analysis.calls["selection"] == 12
    assert len(delivery.sent) == 1
    state = store.load()
    digest = state["digests"][report["digest_id"]]
    assert len(digest["items"]) == 12
    assert {record["intended_tier"] for record in state["papers"].values()} == {"translation"}
    archive = (store.directory / f"archive/2026/10/02/{report['digest_id']}.md").read_text(encoding="utf-8")
    for paper in papers:
        assert paper.title in archive
        assert "中文摘要：" + paper.abstract in archive


def _llm_paper(index, age):
    published = (NOW - age).isoformat()
    return Paper(f"2610.{index:05d}v1", f"Large language model {index}",
                 "A large language model for reasoning and instruction tuning.", ["cs.CL"], published, published)


@pytest.mark.parametrize("provider", ["google_free", "microsoft_free"])
def test_each_channel_limits_processing_to_thirty_and_defers_rest(tmp_path, provider):
    config = load_config(default_config_path())
    config["translation"]["provider"] = provider
    papers = [_llm_paper(i, timedelta(days=1, minutes=i)) for i in range(65)]
    store = StateStore(tmp_path / provider)
    translator = StubTranslator()
    analysis = RulesAnalysis(config)
    delivery = MockDelivery("confirmed")
    def run(day):
        return Pipeline(config, store, None, analysis, None, delivery, now=NOW + timedelta(days=day),
                        translator=translator).run(list(reversed(papers)))
    first = run(0)
    assert first["translation"] == 30 and not first["errors"]
    assert analysis.calls["selection"] == translator.calls == 30
    state = store.load()
    assert [item["paper"]["version_id"] for item in state["digests"][first["digest_id"]]["items"]] == [p.version_id for p in papers[:30]]
    assert all(not state["papers"][p.base_id].get("decision") for p in papers[30:])
    again = run(0)
    assert again["translation"] == 0 and translator.calls == 30
    assert run(1)["translation"] == 30 and translator.calls == 60
    assert run(2)["translation"] == 5 and translator.calls == 65


@pytest.mark.parametrize("recent_count,expected_queries", [(35, 1), (8, 2)])
def test_recent_week_is_filled_with_newest_month_papers_only_when_needed(tmp_path, recent_count, expected_queries):
    config = load_config(default_config_path())
    config["translation"]["provider"] = "google_free"
    papers = ([_llm_paper(i, timedelta(days=2, minutes=i)) for i in range(recent_count)]
              + [_llm_paper(100 + i, timedelta(days=9, minutes=i)) for i in range(35)]
              + [_llm_paper(200, timedelta(days=31))])
    class Source:
        def __init__(self): self.windows = []
        def collect(self, window):
            self.windows.append(window.copy())
            selected = [p for p in papers if window["start"] <= p.published <= window["end"]
                        and p.base_id not in window["excluded_ids"]]
            selected.sort(key=lambda p: p.published, reverse=True)
            return CollectionResult(papers=selected[:window["limit"]])
    source = Source()
    translator = StubTranslator()
    store = StateStore(tmp_path / "daily")
    report = Pipeline(config, store, source, RulesAnalysis(config), None, MockDelivery("confirmed"),
                      now=NOW, translator=translator).run()
    assert not report["errors"] and report["translation"] == 30
    assert len(source.windows) == expected_queries
    if expected_queries == 2:
        assert source.windows[1]["start"] == (NOW - timedelta(days=30)).isoformat()
        assert source.windows[1]["end"] == source.windows[0]["start"]
        assert source.windows[1]["limit"] == 22
    sent = store.load()["digests"][report["digest_id"]]["items"]
    assert [item["paper"]["version_id"] for item in sent] == [p.version_id for p in papers[:30]]
    assert translator.calls == 30


def test_committed_rules_require_language_model_specific_terms():
    config = load_config(default_config_path())
    for abstract in ("A vision foundation model for segmentation.", "We prune a base model for image recognition.",
                     "Reinforcement learning agents for robotics."):
        paper = _llm_paper(1, timedelta(days=1))
        paper.title, paper.abstract = "Computer vision research", abstract
        assert match_rules(paper, config) == []
    for abstract in ("We optimize large language models.", "We study LLM reasoning.",
                     "A multimodal large language model with visual instruction tuning."):
        paper = _llm_paper(2, timedelta(days=1))
        paper.title, paper.abstract = "Language research", abstract
        assert match_rules(paper, config)


def test_cached_unsent_papers_wait_without_translation_and_expire_after_month(tmp_path):
    config = load_config(default_config_path())
    config["translation"]["provider"] = "google_free"
    store = StateStore(tmp_path / "queued")
    state = store.load()
    papers = [_llm_paper(i, timedelta(days=20, minutes=i)) for i in range(35)]
    papers.append(_llm_paper(100, timedelta(days=31)))
    analysis = RulesAnalysis(config)
    for paper in papers:
        record = upsert_paper(state, paper, NOW - timedelta(days=20), match_rules(paper, config))
        record["decision"] = analysis.select(paper, record["rule_hits"]).to_dict()
        record["stage"] = "ready"
        record["intended_tier"] = "translation"
    store.save(state)
    translator = StubTranslator()
    report = Pipeline(config, store, None, RulesAnalysis(config), None, MockDelivery("confirmed"),
                      now=NOW, translator=translator).run([])
    assert not report["errors"] and report["translation"] == 30
    assert translator.calls == 30
    assert not store.load()["papers"][papers[-1].base_id].get("translation")


def test_same_day_partial_digest_only_uses_the_remaining_paper_slots(tmp_path):
    config = load_config(default_config_path())
    config["translation"]["provider"] = "google_free"
    store = StateStore(tmp_path / "partial")
    analysis, translator, delivery = RulesAnalysis(config), StubTranslator(), MockDelivery("confirmed")
    papers = [_llm_paper(i, timedelta(days=1, minutes=i)) for i in range(40)]
    def run(items):
        return Pipeline(config, store, None, analysis, None, delivery, now=NOW, translator=translator).run(items)
    assert run(papers[:10])["translation"] == 10
    assert run(papers)["translation"] == 20
    assert translator.calls == 30
    assert run(papers)["translation"] == 0
    assert len(store.load()["daily"]["2026-10-02"]["paper_ids"]) == 30
