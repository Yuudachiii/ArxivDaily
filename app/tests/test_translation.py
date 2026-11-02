"""Metadata-only delivery, MT adapters and the persistent paper index."""
from copy import deepcopy
from datetime import timedelta
import json

import httpx
import pytest

from arxivdaily.config import load_config
from arxivdaily.delivery import MockDelivery
from arxivdaily.llm import AnalysisClient
from arxivdaily.models import DigestItem, Paper, ProcessingError, Translation
from arxivdaily.pipeline import Pipeline
from arxivdaily.render import render_archive, render_notification
from arxivdaily import translation as translation_module
from arxivdaily.translation import TranslationClient
from test_pipeline import NOW, system
from test_llm import FakeClient, FakeResponse, paper


def test_translation_workflow_never_reads_fulltext_and_includes_uncertain(tmp_path):
    cfg, fixture, papers, analysis, reader, store, delivery = system(tmp_path)
    cfg["presentation"] = load_config()["presentation"]
    report = Pipeline(cfg, store, None, analysis, reader, delivery, now=NOW).run(papers)
    assert report["translation"] == 3 and report["full"] == report["light"] == 0
    assert reader.calls == analysis.calls["review"] == analysis.calls["summary"] == 0
    assert not report["errors"]
    assert analysis.calls["selection"] == 4
    state = store.load()
    digest = state["digests"][report["digest_id"]]
    for entry in digest["items"]:
        assert entry["translation"]["abstract_zh"] and entry["summary"] is None
        assert entry["paper"]["abstract"] in delivery.sent[0][1]
    md = (store.directory / "archive/papers.md").read_text(encoding="utf-8")
    assert md.count("| [arXiv ") == 4  # Rejected candidates are indexed too.
    assert "待翻译" not in md
    assert len(analysis.translation_calls) == 4


def test_translation_unknown_delivery_recovers_cached_content_after_mode_switch(tmp_path):
    cfg, fixture, papers, analysis, reader, store, delivery = system(tmp_path, "unknown")
    cfg["presentation"] = load_config()["presentation"]
    first = Pipeline(cfg, store, None, analysis, reader, delivery, now=NOW).run(papers[:2])
    snapshot = store.directory / f"archive/2026/10/02/{first['digest_id']}.md"
    before = snapshot.read_bytes()
    calls = deepcopy(analysis.translation_calls)
    cfg["presentation"].update(llm_summary=True, abstract=False, abstract_translation=False)
    second = Pipeline(cfg, store, None, analysis, reader, MockDelivery(), now=NOW + timedelta(days=1)).run([])
    assert second["translation"] == second["recovery"] == 2
    assert analysis.translation_calls == calls and reader.calls == 0
    assert snapshot.read_bytes() == before


def test_translation_failure_retries_only_missing_content(tmp_path):
    cfg, fixture, papers, analysis, reader, store, delivery = system(tmp_path)
    cfg["presentation"] = load_config()["presentation"]
    class Translator:
        broken = True
        def translate(self, p, *, title_only=False):
            if self.broken and not title_only:
                raise ProcessingError("translation_provider_unavailable")
            return Translation("中文标题", "" if title_only else "完整摘要译文")
    translator = Translator()
    first = Pipeline(cfg, store, None, analysis, reader, delivery, now=NOW, translator=translator).run(papers[:1])
    assert first["delivery_kind"] == "alert"
    record = store.load()["papers"][papers[0].base_id]
    assert "translation" in record["failures"] and record["title_zh"]
    translator.broken = False
    second = Pipeline(cfg, store, None, analysis, reader, delivery, now=NOW + timedelta(days=1), translator=translator).run([])
    assert not second["errors"] and second["translation"] == 1
    assert analysis.calls["selection"] == 1 and reader.calls == 0


def test_empty_run_pushes_notice_and_reserves_quota_before_send(tmp_path):
    cfg, fixture, papers, analysis, reader, store, delivery = system(tmp_path)
    class InspectDelivery(MockDelivery):
        def send(self, title, body):
            saved = store.load()
            assert saved["daily"]["2026-10-02"]["send_attempts"] == 1
            assert any(d["status"] == "sending" and d["empty"] for d in saved["digests"].values())
            assert "今天未发现新增工作" in body
            return super().send(title, body)
    report = Pipeline(cfg, store, None, analysis, reader, InspectDelivery(), now=NOW).run([])
    assert report["delivery_kind"] == "empty" and report["delivery_status"] == "confirmed"
    assert analysis.calls == {"selection": 0, "review": 0, "summary": 0}


def test_empty_notice_does_not_hide_incomplete_collection_or_quota_limit(tmp_path):
    from arxivdaily.collection import CollectionResult
    cfg, fixture, papers, analysis, reader, store, delivery = system(tmp_path)
    class Collector:
        def collect(self, window):
            return CollectionResult(complete=False, errors=["source_failed"])
    report = Pipeline(cfg, store, Collector(), analysis, reader, delivery, now=NOW).run()
    assert report["delivery_kind"] == "alert" and "今天未发现新增工作" not in delivery.sent[0][1]
    cfg["delivery"]["daily_limit"] = 1
    second = Pipeline(cfg, store, None, analysis, reader, delivery, now=NOW + timedelta(hours=1)).run([])
    assert second["delivery_status"] == "deferred" and "delivery_daily_quota_exhausted" in second["errors"]
    assert len(delivery.sent) == 1


def test_index_backfills_old_state_deduplicates_and_escapes_titles(tmp_path):
    from arxivdaily.state import upsert_paper
    cfg, fixture, papers, analysis, reader, store, delivery = system(tmp_path)
    state = store.load()
    p = Paper.from_dict({**papers[0].to_dict(), "title": "A | B\n<mask>"})
    at = NOW.replace(hour=17)  # Next calendar day in Shanghai.
    upsert_paper(state, p, at, ["route"])
    record = state["papers"][p.base_id]
    record["stage"] = "rejected"
    record["decision"] = fixture["decisions"][papers[2].base_id]
    store.path.write_text(json.dumps(state), encoding="utf-8")  # Pre-feature state has no index.
    Pipeline(cfg, store, None, analysis, reader, delivery, now=at).run([])
    md = (store.directory / "archive/papers.md").read_text(encoding="utf-8")
    assert "2026-10-03" in md and "A &#124; B &lt;mask&gt;" in md
    state = store.load()
    upsert_paper(state, Paper.from_dict({**p.to_dict(), "version_id": p.base_id + "v2"}), at + timedelta(days=1), [])
    store.save(state)
    md = (store.directory / "archive/papers.md").read_text(encoding="utf-8")
    assert md.count("| [arXiv ") == 1 and p.url in md


def test_translation_notification_moves_whole_abstract_to_archive_when_large():
    p = paper()
    item = DigestItem(p, "translation", translation=Translation("掩码论文", "完整译文。" * 2000))
    body, modes = render_notification("2026-10-07", [item], "https://example.org/digest.md", 1200)
    assert len(body.encode("utf-8")) <= 1200
    assert modes[p.base_id] == "translation_archive_link_only"
    assert "完整译文。" not in body
    assert item.translation.abstract_zh in render_archive("2026-10-07", [item])


def test_llm_translation_disables_thinking_and_accounts_for_usage(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "translation-test-canary")
    cfg = load_config()
    fake = FakeClient([FakeResponse({"title_zh": "掩码论文", "abstract_zh": "我们使用掩码预测器。"})])
    analysis = AnalysisClient(cfg, fake)
    result = TranslationClient(cfg, analysis).translate(paper())
    payload = fake.calls[0][1]["json"]
    assert payload["thinking"] == {"type": "disabled"} and "reasoning_effort" not in payload
    supplied = json.loads(payload["messages"][1]["content"])
    assert supplied == {"title": paper().title, "abstract": paper().abstract}
    assert result.abstract_zh and analysis.usage_records[0]["role"] == "translation"
    assert analysis.usage_records[0]["estimated_cost"] > 0


@pytest.mark.parametrize("provider", ["deepl", "libretranslate"])
def test_mt_official_payloads_and_no_llm_usage(provider, monkeypatch):
    cfg = load_config()
    cfg["translation"].update(provider=provider, base_url="https://translate.example.org")
    monkeypatch.setenv("TRANSLATION_API_KEY", "mt-key")
    requests = []
    def respond(request):
        requests.append(request)
        data = json.loads(request.content)
        if provider == "deepl":
            assert request.headers["Authorization"] == "DeepL-Auth-Key mt-key"
            assert data == {"text": [paper().title, paper().abstract], "source_lang": "EN", "target_lang": "ZH"}
            return httpx.Response(200, json={"translations": [{"text": "标题"}, {"text": "摘要"}]})
        assert data == {"q": [paper().title, paper().abstract], "source": "en", "target": "zh", "format": "text", "api_key": "mt-key"}
        return httpx.Response(200, json={"translatedText": ["标题", "摘要"]})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    analysis = AnalysisClient(cfg, client)
    result = TranslationClient(cfg, analysis).translate(paper())
    assert result == Translation("标题", "摘要") and not analysis.usage_records
    assert len(requests) == 1


def test_mt_retries_rate_limits_but_never_uses_paid_fallback():
    cfg = load_config()
    cfg["translation"].update(provider="libretranslate", base_url="http://localhost:5000", attempts=2)
    replies = [httpx.Response(429), httpx.Response(200, json={"translatedText": ["标题"]})]
    client = httpx.Client(transport=httpx.MockTransport(lambda request: replies.pop(0)))
    analysis = AnalysisClient(cfg, client)
    sleeps = []
    assert TranslationClient(cfg, analysis, sleep=sleeps.append).translate(paper(), title_only=True).title_zh == "标题"
    assert len(sleeps) == 1 and not analysis.usage_records
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(403)))
    with pytest.raises(ProcessingError, match="nonretryable"):
        TranslationClient(cfg, AnalysisClient(cfg, client)).translate(paper())


def test_google_free_provider_needs_no_key_and_parses_nested_segments(monkeypatch):
    monkeypatch.delenv("TRANSLATION_API_KEY", raising=False)
    cfg = load_config()
    cfg["translation"].update(provider="google_free", request_interval=0)
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.host == "translate.googleapis.com"
        assert request.url.path == "/translate_a/single"
        assert request.url.params["client"] == "gtx" and request.url.params["sl"] == "en"
        assert request.url.params["tl"] == "zh-CN" and request.url.params["dt"] == "t"
        text = request.url.params["q"]
        return httpx.Response(200, json=[[[f"译:{text}", text, None], ["补充", text, None]], None, "en"])

    client = httpx.Client(transport=httpx.MockTransport(respond))
    from arxivdaily.rulesmode import RulesAnalysis
    analysis = RulesAnalysis(cfg, client)
    result = TranslationClient(cfg, analysis).translate(paper())
    assert result == Translation("译:" + paper().title + "补充", "译:" + paper().abstract + "补充")
    assert len(requests) == 2  # one request per text
    assert not analysis.usage_records and analysis.calls == {"selection": 0, "review": 0, "summary": 0}


class FakeTranslators:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def translate_text(self, text, *, translator, from_language, to_language, **kwargs):
        self.calls.append((text, translator, from_language, to_language))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_microsoft_free_provider_uses_translators_bing_engine(monkeypatch):
    monkeypatch.delenv("TRANSLATION_API_KEY", raising=False)
    cfg = load_config()
    cfg["translation"].update(provider="microsoft_free")
    fake = FakeTranslators(["标题", "摘要"])
    monkeypatch.setattr(translation_module, "_load_translators", lambda: fake)
    from arxivdaily.rulesmode import RulesAnalysis
    analysis = RulesAnalysis(cfg)
    result = TranslationClient(cfg, analysis).translate(paper())
    assert result == Translation("标题", "摘要")
    assert fake.calls == [
        (paper().title, "bing", "en", "zh"),
        (paper().abstract, "bing", "en", "zh"),
    ]


def test_microsoft_free_retries_library_errors_without_region_settings(monkeypatch):
    cfg = load_config()
    cfg["translation"].update(provider="microsoft_free", attempts=2)
    fake = FakeTranslators([RuntimeError("engine busy"), "标题"])
    monkeypatch.setattr(translation_module, "_load_translators", lambda: fake)
    from arxivdaily.rulesmode import RulesAnalysis
    analysis = RulesAnalysis(cfg)
    sleeps = []
    result = TranslationClient(cfg, analysis, sleep=sleeps.append).translate(paper(), title_only=True)
    assert result.title_zh == "标题"
    assert len(sleeps) == 1 and len(fake.calls) == 2


def test_microsoft_free_missing_dependency_is_a_processing_error(monkeypatch):
    cfg = load_config()
    cfg["translation"].update(provider="microsoft_free", attempts=3)

    def missing():
        raise ProcessingError("translation_dependency_missing")

    monkeypatch.setattr(translation_module, "_load_translators", missing)
    from arxivdaily.rulesmode import RulesAnalysis
    with pytest.raises(ProcessingError, match="dependency_missing"):
        TranslationClient(cfg, RulesAnalysis(cfg)).translate(paper())


def test_google_free_retries_rate_limits_and_forbidden_without_llm_fallback():
    cfg = load_config()
    cfg["translation"].update(provider="google_free", attempts=2, request_interval=0)
    for status in (429, 403):
        replies = [httpx.Response(status), httpx.Response(200, json=[[["标题", "title", None]]])]
        client = httpx.Client(transport=httpx.MockTransport(lambda request: replies.pop(0)))
        from arxivdaily.rulesmode import RulesAnalysis
        analysis = RulesAnalysis(cfg, client)
        sleeps = []
        assert TranslationClient(cfg, analysis, sleep=sleeps.append).translate(paper(), title_only=True).title_zh == "标题"
        assert len(sleeps) == 1 and not analysis.usage_records


def test_google_free_pauses_between_requests(monkeypatch):
    cfg = load_config()
    cfg["translation"].update(provider="google_free", request_interval=1.0)
    client = httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=[[["标题", "title", None]]])))
    from arxivdaily.rulesmode import RulesAnalysis
    analysis = RulesAnalysis(cfg, client)
    sleeps = []
    translator = TranslationClient(cfg, analysis, sleep=sleeps.append)
    translator.translate(paper(), title_only=True)  # first request has no wait
    translator.translate(paper(), title_only=True)  # second request waits for the interval
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 1.0


@pytest.mark.parametrize("text", ['[workflow]\nmode="bad"', '[translation]\nprovider="unknown"', '[translation]\nprovider="deepl"\nbase_url="http://localhost:5000"'])
def test_invalid_translation_config_is_rejected(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_local_libretranslate_config_is_allowed(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[translation]\nprovider="libretranslate"\nbase_url="http://localhost:5000"', encoding="utf-8")
    assert load_config(path)["translation"]["provider"] == "libretranslate"


@pytest.mark.parametrize("provider", ["google_free", "microsoft_free"])
def test_keyless_free_provider_config_is_allowed(tmp_path, provider):
    path = tmp_path / "config.toml"
    path.write_text(f'[translation]\nprovider="{provider}"\nlabel="谷歌"\n', encoding="utf-8")
    config = load_config(path)
    assert config["translation"]["provider"] == provider
    assert config["translation"]["label"] == "谷歌"


@pytest.mark.parametrize("text", [
    '[selection]\nmode = "auto"',
    '[translation]\nlabel = "' + "x" * 33 + '"',
    '[translation]\nrequest_interval = -0.5',
    '[translation]\nrequest_interval = 61',
])
def test_invalid_selection_mode_or_translation_label_is_rejected(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_empty_notice_does_not_block_new_papers_later_same_day(tmp_path):
    cfg, fixture, papers, analysis, reader, store, delivery = system(tmp_path)
    cfg["presentation"] = load_config()["presentation"]
    Pipeline(cfg, store, None, analysis, reader, delivery, now=NOW).run([])
    report = Pipeline(cfg, store, None, analysis, reader, delivery, now=NOW + timedelta(hours=1)).run(papers[:1])
    assert report["translation"] == 1 and len(delivery.sent) == 2


def test_pending_uncertain_translation_keeps_metadata_workflow_after_switch(tmp_path):
    cfg, fixture, papers, analysis, reader, store, delivery = system(tmp_path)
    cfg["presentation"] = load_config()["presentation"]
    uncertain = papers[3]
    class Translator:
        failed = True
        def translate(self, p, *, title_only=False):
            if self.failed and not title_only:
                raise ProcessingError("temporarily_unavailable")
            return Translation("待确认候选", "" if title_only else "摘要全文翻译")
    translator = Translator()
    Pipeline(cfg, store, None, analysis, reader, delivery, now=NOW, translator=translator).run([uncertain])
    translator.failed = False
    cfg["presentation"].update(llm_summary=True, abstract=False, abstract_translation=False)
    report = Pipeline(cfg, store, None, analysis, reader, delivery, now=NOW + timedelta(days=1), translator=translator).run([])
    assert report["translation"] == 1 and reader.calls == 0
    assert store.load()["papers"][uncertain.base_id]["decision"]["status"] == "uncertain"


def test_title_retry_updates_index_without_resending_confirmed_paper(tmp_path):
    cfg, fixture, papers, analysis, reader, store, delivery = system(tmp_path)
    Pipeline(cfg, store, None, analysis, reader, delivery, now=NOW).run(papers[:1])
    state = store.load()
    record = state["papers"][papers[0].base_id]
    record.pop("title_zh")
    record["attempts"]["title_translation"] = 3
    record["failures"]["title_translation"] = {"last_at": NOW.isoformat(), "error": "unavailable"}
    store.save(state)
    calls = dict(analysis.calls)
    report = Pipeline(cfg, store, None, analysis, reader, delivery, now=NOW + timedelta(hours=1)).run([], retry_stage="title_translation", retry_ids=[papers[0].base_id])
    assert not report["errors"] and not report["digest_id"] and len(delivery.sent) == 1
    assert store.load()["papers"][papers[0].base_id]["title_zh"]
    assert analysis.calls == calls
