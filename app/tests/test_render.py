from datetime import date

import pytest

from arxivdaily.models import DigestItem, Evidence, Paper, Summary
from arxivdaily.render import render_archive, render_notification, server_title


def item(number: int, tier: str, *, chinese: int = 0) -> DigestItem:
    p = Paper(f"2601.{number:05d}v1", f"English Paper {number}", "Abstract", ["cs.CV"],
              "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00")
    s = None
    if tier == "full":
        s = Summary("背景" + "中" * chinese, "贡献", "方法", "实验", "结论",
                    [Evidence("experiments", "Table 2", "result", p.url)])
    return DigestItem(p, tier, s)


def test_server_title_appends_short_variant_label_only():
    assert server_title("2026-10-02") == "arXiv 每日简报 2026-10-02"
    assert server_title("2026-10-02", "谷歌") == "arXiv 每日简报 2026-10-02 · 谷歌"
    assert server_title("2026-10-02", "") == "arXiv 每日简报 2026-10-02"
    assert server_title("2026-10-02", "x" * 33) == "arXiv 每日简报 2026-10-02"


def test_archive_shows_summary_without_internal_evidence_and_light_only_title_link():
    full = item(1, "full")
    archive = render_archive(date(2026, 10, 2), [full, item(2, "light")])
    assert "**方法：** 方法" in archive
    assert "证据定位" not in archive and "Table 2" not in archive
    assert full.summary.evidence[0].locator == "Table 2"
    body, _ = render_notification("2026-10-02", [full], "https://example.org/a.md")
    assert "**方法：** 方法" in body
    assert "证据定位" not in body and "Table 2" not in body
    light_section = archive.split("## 轻量档", 1)[1]
    assert "English Paper 2" in light_section
    assert "Abstract" not in light_section and "相关理由" not in light_section
    assert server_title("2026-10-02") == "arXiv 每日简报 2026-10-02"


def test_byte_cap_moves_whole_full_paper_to_archive_link():
    items = [item(1, "full", chinese=1000), item(2, "light")]
    archive_url = "https://example.org/archive/2026-10-02.md"
    body, modes = render_notification("2026-10-02", items, archive_url, max_bytes=500)
    assert len(body.encode("utf-8")) <= 500
    assert modes[items[0].paper.base_id] == "archive_link_only"
    assert "背景中" not in body and archive_url in body
    assert items[0].tier == "full" and items[0].summary is not None


def test_light_overflow_is_listed_as_count_in_archive():
    items = [item(n, "light") for n in range(1, 20)]
    body, modes = render_notification("2026-10-02", items, "https://example.org/a.md", max_bytes=250)
    assert len(body.encode("utf-8")) <= 250
    assert "轻量档仅列于公开存档" in body
    assert "light_archive_only" in modes.values()


def test_single_giant_full_title_uses_compact_archive_label():
    full = item(7, "full", chinese=2000)
    full.paper.title = "English " + "VeryLong " * 900
    body, modes = render_notification("2026-10-02", [full], "https://example.org/a.md", max_bytes=350)
    assert len(body.encode("utf-8")) <= 350
    assert modes[full.paper.base_id] == "archive_link_only"
    assert f"arXiv {full.paper.base_id}" in body
    assert full.paper.title not in body


def test_recovery_warning_and_rejected_duplicate_id():
    old = item(1, "full")
    old.recovery_of = "digest-old"
    body, _ = render_notification("2026-10-02", [old], "https://example.org/a.md")
    assert "可能重复" in body
    assert "**方法：** 方法" in body and "证据定位" not in body and "Table 2" not in body
    with pytest.raises(ValueError, match="duplicate"):
        render_notification("2026-10-02", [old, old], "https://example.org/a.md")


def test_mixed_digest_visibly_separates_recovery_from_new_papers():
    new = item(1, "light")
    retried = item(2, "full")
    retried.recovery_of = "old-digest"
    items = [new, retried]
    archive = render_archive("2026-10-02", items)
    body, modes = render_notification("2026-10-02", items, "https://example.org/a.md")
    assert "## 轻量档" in archive and "## 补发完整档" in archive
    assert archive.index(new.paper.title) < archive.index("## 补发完整档") < archive.index(retried.paper.title)
    assert "## 补发完整档" in body and "可能重复" in body
    assert "证据定位" not in archive and "Table 2" not in archive
    assert "证据定位" not in body and "Table 2" not in body
    assert modes[retried.paper.base_id] == "rendered_inline_full"
