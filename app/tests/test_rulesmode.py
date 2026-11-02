"""Rules mode: deterministic selection plus translation-tier delivery, no model calls."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from arxivdaily.config import default_config_path, load_config
from arxivdaily.delivery import MockDelivery
from arxivdaily.models import Paper, ProcessingError, Translation
from arxivdaily.pipeline import Pipeline
from arxivdaily.render import server_title
from arxivdaily.rulesmode import RulesAnalysis
from arxivdaily.simulation import FixtureReader, load_fixture
from arxivdaily.state import StateStore

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


def test_rules_mode_has_no_daily_paper_cap_and_archives_full_detail(tmp_path):
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
