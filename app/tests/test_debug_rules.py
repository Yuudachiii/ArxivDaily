"""Offline notebook experiments must retain negative examples and expose coverage."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace

import pytest

from arxivdaily.collection import Collector
from arxivdaily.config import load_config
from tools.debug_rules import (date_window, load_or_fetch, screen_snapshot,
                               compare_matches, inspect_paper, table_html, export_matches)


class MetadataClient:
    def __init__(self, items):
        self.items = items
        self.calls = 0

    def results(self, search):
        self.calls += 1
        cats = re.findall(r"cat:([\w.]+)", search.query)
        for item in self.items:
            if not cats or set(cats).intersection(item.categories):
                yield item


def raw(index, abstract, categories=None, version=1):
    moment = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    return SimpleNamespace(entry_id=f"https://arxiv.org/abs/2610.{index:05d}v{version}",
                           title=f"Paper {index}", summary=abstract, categories=categories or ["cs.CV"],
                           authors=[], published=moment, updated=moment, pdf_url="")


def experiment(tmp_path):
    config = load_config()
    config["rules"]["categories"] = ["cs.CV"]
    window = date_window(14, "2026-10-06")
    client = MetadataClient([raw(1, "Medical spatial reasoning segmentation."),
                             raw(2, "A dataset for pixel inference."),
                             raw(1, "Medical spatial reasoning segmentation.", version=2)])
    snapshot, path, reused = load_or_fetch(config, window, tmp_path, client=client)
    return config, window, client, snapshot, path


def test_previous_fourteen_calendar_days_are_explicit_and_use_local_timezone():
    window = date_window(14, now=datetime(2026, 10, 6, 22, tzinfo=timezone.utc))
    assert window["local_start_date"] == "2026-09-23"
    assert window["local_end_date"] == "2026-10-06"
    assert window["start"] == "2026-09-22T16:00:00+00:00"
    assert window["end"] == "2026-10-06T15:59:59.999999+00:00"


def test_raw_metadata_keeps_nonmatches_and_deduplicates_revisions(tmp_path):
    config, window, client, snapshot, path = experiment(tmp_path)
    assert snapshot["complete"] and len(snapshot["papers"]) == 2
    matches = screen_snapshot(snapshot, config, window=window)
    assert [row["version_id"] for row in matches] == ["2610.00001v2"]
    assert inspect_paper(snapshot, config, "2610.00002")["routes"] == []
    assert json.loads(path.read_text(encoding="utf-8"))["papers"] == snapshot["papers"]


def test_keyword_expansion_uses_cached_negative_examples_without_network(tmp_path):
    config, window, client, snapshot, path = experiment(tmp_path)
    before = screen_snapshot(snapshot, config)
    changed = deepcopy(config)
    changed["rules"]["groups"]["PIXEL"] = ["pixel inference"]
    changed["rules"]["routes"]["custom"] = [["PIXEL"]]
    reused_snapshot, reused_path, reused = load_or_fetch(changed, window, tmp_path, client=client)
    assert reused and reused_path == path and client.calls == 1
    after = screen_snapshot(reused_snapshot, changed)
    assert len(after) == 2
    assert compare_matches(before, after)["added"][0]["base_id"] == "2610.00002"
    assert inspect_paper(snapshot, changed, "2610.00002")["matched_groups"]["PIXEL"] == ["pixel inference"]


def test_broader_categories_need_new_fetch_but_narrower_can_filter_locally(tmp_path):
    config, window, client, snapshot, path = experiment(tmp_path)
    broader = deepcopy(config)
    broader["rules"]["categories"].append("cs.LG")
    with pytest.raises(ValueError, match="分类范围扩大"):
        screen_snapshot(snapshot, broader)
    _, another_path, reused = load_or_fetch(broader, window, tmp_path, client=client)
    assert not reused and another_path != path and client.calls == 2
    with pytest.raises(ValueError, match="分类范围扩大"):
        screen_snapshot(snapshot, {**config, "rules": {**config["rules"], "categories": []}})
    smaller = {**config, "rules": {**config["rules"], "categories": []}}
    all_snapshot = {**snapshot, "categories": []}
    assert screen_snapshot(all_snapshot, smaller)


def test_partial_fetch_is_preserved_with_explicit_incomplete_status(tmp_path):
    config = load_config()
    window = date_window(14, "2026-10-06")
    client = MetadataClient([raw(1, "Medical segmentation."), raw(2, "Non-matching text.")])
    snapshot, path, reused = load_or_fetch(config, window, tmp_path, max_papers=1, client=client)
    assert not snapshot["complete"] and snapshot["errors"] and len(snapshot["papers"]) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["complete"] is False
    load_or_fetch(config, window, tmp_path, max_papers=1, refresh=True, client=client)
    assert client.calls == 2


def test_actual_collector_still_filters_by_default_and_debug_is_opt_in(tmp_path):
    config, window, client, snapshot, path = experiment(tmp_path)
    assert len(Collector(config, client).collect(window).papers) == 1
    assert len(Collector(config, client).collect(window, apply_rules=False).papers) == 2


def test_result_table_escapes_titles_and_export_contains_all_matches(tmp_path):
    config, window, client, snapshot, path = experiment(tmp_path)
    rows = screen_snapshot(snapshot, config)
    rows[0]["title"] = "<script>unsafe</script>"
    table = table_html(rows)
    assert "<script>" not in table and "&lt;script&gt;" in table
    target = export_matches(rows, tmp_path / "results.csv")
    assert target.read_bytes().startswith(b"\xef\xbb\xbf")
    assert rows[0]["abstract"] in target.read_text(encoding="utf-8-sig")


def test_notebook_cells_execute_offline_with_metadata_fetch_substituted(tmp_path, monkeypatch):
    display_module = SimpleNamespace(HTML=lambda text: text, display=lambda *_: None)
    monkeypatch.setitem(sys.modules, "IPython", SimpleNamespace(display=display_module))
    monkeypatch.setitem(sys.modules, "IPython.display", display_module)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "ml/python.exe"))
    path = Path(__file__).resolve().parents[1] / "notebooks/arxiv_rules_debug.ipynb"
    notebook = json.loads(path.read_text(encoding="utf-8"))
    # Editors may save a conda interpreter under the generic python3 name.
    # The first executable cell checks the actual interpreter, not this label.
    assert notebook["metadata"]["kernelspec"]["language"] == "python"
    namespace = {}
    for index, cell in enumerate(notebook["cells"]):
        if cell["cell_type"] != "code":
            continue
        if index == 5:
            def offline_fetch(config, window, cache_dir, **kwargs):
                client = MetadataClient([raw(1, "We pre-train a large language model with instruction tuning.")])
                return load_or_fetch(config, date_window(14, "2026-10-06"), tmp_path, client=client)
            namespace["load_or_fetch"] = offline_fetch
            namespace["window"] = date_window(14, "2026-10-06")
        exec(compile("".join(cell["source"]), f"{path.name}:cell-{index}", "exec"), namespace)
    assert len(namespace["matches"]) == 1 and namespace["export_path"].is_file()
