from __future__ import annotations

import re
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from arxivdaily.collection import Collector, plan_window
from arxivdaily.config import load_config
from arxivdaily.models import ProcessingError


def raw(index: int, published: datetime, categories=None):
    return types.SimpleNamespace(
        entry_id=f"https://arxiv.org/abs/2610.{index:05d}v1",
        title=f"Paper {index}",
        summary="Medical image segmentation with spatial reasoning.",
        categories=categories or ["stat.ML", "cs.CV"],
        authors=[types.SimpleNamespace(name="A. Researcher")],
        published=published,
        updated=published,
        pdf_url=f"https://arxiv.org/pdf/2610.{index:05d}v1",
    )


class Search:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeClient:
    def __init__(self, items, fail_after=None):
        self.items = items
        self.fail_after = fail_after
        self.calls = []

    def results(self, search):
        self.calls.append(search)
        if hasattr(search, "id_list"):
            ids = set(search.id_list)
            selected = [x for x in self.items if x.entry_id.split("/")[-1] in ids]
        else:
            date_parts = re.search(r"submittedDate:\[(\d{12}) TO (\d{12})\]", search.query).groups()
            start, end = (datetime.strptime(x, "%Y%m%d%H%M").replace(tzinfo=timezone.utc) for x in date_parts)
            cats = re.findall(r"cat:([\w.]+)", search.query)
            selected = [x for x in self.items if start <= x.published <= end + timedelta(minutes=1) and (not cats or set(cats).intersection(x.categories))]
        for index, item in enumerate(selected):
            if self.fail_after is not None and index == self.fail_after:
                self.fail_after = None
                raise RuntimeError("page interrupted")
            yield item


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.module = patch.dict(sys.modules, {"arxiv": types.SimpleNamespace(Search=Search)})
        self.module.start()
        self.addCleanup(self.module.stop)
        self.config = load_config()
        self.start = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.end = self.start + timedelta(days=1)
        self.window = {"start": self.start.isoformat(), "end": self.end.isoformat()}

    def test_250_results_later_page_cross_listing_and_dedup(self):
        items = [raw(i, self.start + timedelta(minutes=i)) for i in range(1, 251)]
        items.append(items[249])
        client = FakeClient(items)
        collected = Collector(self.config, client).collect(self.window)
        self.assertTrue(collected.complete)
        self.assertEqual(len(collected.papers), 250)
        self.assertIn("2610.00250", {p.base_id for p in collected.papers})
        self.assertIsNone(client.calls[0].max_results)
        self.assertIn("submittedDate:", client.calls[0].query)

    def test_interrupted_page_retains_discovered_and_recovers_shards(self):
        items = [raw(i, self.start + timedelta(minutes=i)) for i in range(1, 251)]
        client = FakeClient(items, fail_after=200)
        collected = Collector(self.config, client).collect(self.window)
        self.assertTrue(collected.complete)
        self.assertEqual(len(collected.papers), 250)
        self.assertIn("split", {s["status"] for s in collected.shards})

    def test_upstream_metadata_merge_preserves_newest_version_and_cross_listings(self):
        old = raw(1, self.start + timedelta(minutes=1), ["cs.CV"])
        new = raw(1, old.published, ["cs.AI"])
        new.entry_id = new.entry_id.replace("v1", "v2")
        new.updated = old.updated + timedelta(days=1)
        new.title = "Revised medical paper"
        new.authors = [types.SimpleNamespace(name="B. Researcher")]
        result = Collector(self.config, FakeClient([new, old])).collect(self.window)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.papers), 1)
        merged = result.papers[0]
        self.assertEqual(merged.version_id, "2610.00001v2")
        self.assertEqual(merged.title, "Revised medical paper")
        self.assertEqual(set(merged.categories), {"cs.CV", "cs.AI"})
        self.assertEqual(set(merged.authors), {"A. Researcher", "B. Researcher"})

    def test_unrecoverable_failure_and_cap_never_claim_complete(self):
        client = FakeClient([raw(i, self.start + timedelta(minutes=i)) for i in range(1, 5)], fail_after=0)
        failed = Collector(self.config, client).collect(self.window)
        self.assertFalse(failed.complete)
        self.assertTrue(failed.errors)
        self.config["collection"]["max_candidates"] = 2
        capped = Collector(self.config, FakeClient([raw(i, self.start + timedelta(minutes=i)) for i in range(1, 5)])).collect(self.window)
        self.assertFalse(capped.complete)
        self.assertEqual(len(capped.papers), 2)
        self.assertEqual(capped.shards[0]["status"], "candidate_limit")
        exact = Collector(self.config, FakeClient([raw(i, self.start + timedelta(minutes=i)) for i in range(1, 3)])).collect(self.window)
        self.assertTrue(exact.complete)

    def test_third_page_failure_in_unsplittable_shard_preserves_partial_and_incomplete(self):
        self.config["rules"]["categories"] = ["cs.CV"]
        self.config["collection"]["page_size"] = 100
        moment = self.start + timedelta(minutes=1)
        items = [raw(i, moment, categories=["cs.CV"]) for i in range(1, 251)]
        client = FakeClient(items, fail_after=200)
        collected = Collector(self.config, client).collect({"start": moment.isoformat(), "end": moment.isoformat()})
        self.assertFalse(collected.complete)
        self.assertEqual(len(collected.papers), 200)
        self.assertIn("2610.00200", {paper.base_id for paper in collected.papers})
        self.assertEqual(collected.shards[0]["status"], "failed")

    def test_new_candidate_cap_advances_past_known_ids_on_rolling_runs(self):
        self.config["collection"]["max_candidates"] = 2
        items = [raw(i, self.start + timedelta(minutes=i)) for i in range(1, 6)]
        first = Collector(self.config, FakeClient(items)).collect(self.window)
        self.assertFalse(first.complete)
        self.assertEqual({paper.base_id for paper in first.papers}, {"2610.00001", "2610.00002"})

        second_window = {**self.window, "known_ids": [paper.version_id for paper in first.papers]}
        second = Collector(self.config, FakeClient(items)).collect(second_window)
        self.assertFalse(second.complete)
        self.assertEqual({paper.base_id for paper in second.papers}, {"2610.00001", "2610.00002", "2610.00003", "2610.00004"})
        self.assertEqual(len({paper.base_id for paper in second.papers} - {paper.base_id for paper in first.papers}), 2)

        revised = raw(1, self.start + timedelta(minutes=1))
        revised.entry_id = revised.entry_id.replace("v1", "v2")
        revised.updated += timedelta(days=1)
        final_window = {**self.window, "known_ids": [paper.base_id for paper in second.papers]}
        final = Collector(self.config, FakeClient(items + [revised])).collect(final_window)
        self.assertTrue(final.complete)
        self.assertEqual(len(final.papers), 5)
        self.assertEqual(next(paper.version_id for paper in final.papers if paper.base_id == "2610.00001"), "2610.00001v2")

    def test_saturated_shard_splits_and_preserves_candidates(self):
        self.config["collection"]["shard_limit"] = 3
        items = [raw(i, self.start + timedelta(hours=i)) for i in range(1, 6)]
        collected = Collector(self.config, FakeClient(items)).collect(self.window)
        self.assertTrue(collected.complete)
        self.assertEqual(len(collected.papers), 5)
        self.assertIn("split", {s["status"] for s in collected.shards})

    def test_explicit_id_replay_ignores_window_and_rules(self):
        item = raw(1, self.start, categories=["stat.ML"])
        client = FakeClient([item])
        collected = Collector(self.config, client).by_ids(["2610.00001v1", "2610.00001v1"])
        self.assertEqual(len(collected), 1)
        self.assertIsNone(client.calls[0].max_results)

    def test_one_library_client_enforces_three_second_minimum(self):
        made = []

        def make_client(**kwargs):
            made.append(kwargs)
            return FakeClient([])

        with patch.dict(sys.modules, {"arxiv": types.SimpleNamespace(Search=Search, Client=make_client)}):
            self.config["collection"]["delay_seconds"] = 1
            collector = Collector(self.config)
            self.assertTrue(collector.collect(self.window).complete)
            with self.assertRaises(ProcessingError):
                collector.by_ids(["2610.00001"])
        self.assertEqual(len(made), 1)
        self.assertGreaterEqual(made[0]["delay_seconds"], 3)

    def test_pinned_id_rejects_latest_version_substitution(self):
        latest = raw(1, self.start)
        latest.entry_id = latest.entry_id.replace("v1", "v2")
        client = FakeClient([latest])
        # This harness models the API returning v2 for a v1 request.
        client.results = lambda search: iter([latest])
        with self.assertRaises(ProcessingError):
            Collector(self.config, client).by_ids(["2610.00001v1"])

    def test_window_first_run_rolling_gap_and_manual(self):
        now = datetime(2026, 10, 21, tzinfo=timezone.utc)
        first = plan_window(now, {})
        self.assertEqual(first["start"], (now - timedelta(days=7)).isoformat())
        state = {"collection": {"initial_floor": (now - timedelta(days=30)).isoformat(), "last_complete_end": (now - timedelta(days=20)).isoformat()}}
        late = plan_window(now, state)
        self.assertEqual(late["start"], (now - timedelta(days=14)).isoformat())
        self.assertEqual(len(late["uncovered"]), 1)
        self.assertEqual(late["uncovered"][0]["end"], late["start"])
        manual = plan_window(now, state, "2026-09-01", "2026-09-03")
        self.assertEqual(manual["end"], "2026-09-03T23:59:59.999999+00:00")
        self.assertTrue(manual["manual"])
        stalled = plan_window(now, {"collection": {"initial_floor": (now - timedelta(days=20)).isoformat()}})
        self.assertEqual(len(stalled["uncovered"]), 1)

    def test_daily_one_day_window_covers_only_the_previous_day(self):
        now = datetime(2026, 10, 21, 2, tzinfo=timezone.utc)  # 10:00 Beijing
        yesterday = now - timedelta(days=1)
        state = {"collection": {"initial_floor": yesterday.isoformat(), "last_complete_end": yesterday.isoformat()}}
        window = plan_window(now, state, initial_days=1, lookback_days=1)
        self.assertEqual(window["start"], yesterday.isoformat())
        self.assertEqual(window["end"], now.isoformat())
        self.assertEqual(window["uncovered"], [])
        first = plan_window(now, {}, initial_days=1, lookback_days=1)
        self.assertEqual(first["start"], yesterday.isoformat())
        # A skipped day is reported as a gap, never processed as a multi-day backlog.
        stale = {"collection": {"initial_floor": (now - timedelta(days=30)).isoformat(),
                                "last_complete_end": (now - timedelta(days=3)).isoformat()}}
        skipped = plan_window(now, stale, initial_days=1, lookback_days=1)
        self.assertEqual(skipped["start"], yesterday.isoformat())
        self.assertEqual(len(skipped["uncovered"]), 1)

    def test_expanded_window_revisits_before_the_saved_one_day_floor(self):
        now = datetime(2026, 11, 3, 2, 42, tzinfo=timezone.utc)
        yesterday = (now - timedelta(days=1)).isoformat()
        state = {"collection": {"initial_floor": yesterday, "last_complete_end": now.isoformat()}}
        window = plan_window(now, state, initial_days=7, lookback_days=7)
        self.assertEqual(window["start"], (now - timedelta(days=7)).isoformat())
        self.assertEqual(window["end"], now.isoformat())
        self.assertEqual(window["uncovered"], [])
        manual = plan_window(now, state, "2026-10-29", "2026-10-30", initial_days=7, lookback_days=7)
        self.assertEqual(manual["initial_floor"], yesterday)
        self.assertEqual(manual["start"], "2026-10-29T00:00:00+00:00")
        self.assertEqual(state["collection"]["initial_floor"], yesterday)


    def test_late_visible_submission_before_prior_checkpoint_is_revisited(self):
        now = datetime(2026, 10, 21, tzinfo=timezone.utc)
        state = {"collection": {"initial_floor": (now - timedelta(days=30)).isoformat(), "last_complete_end": (now - timedelta(days=1)).isoformat()}}
        window = plan_window(now, state)
        late_paper = raw(1, now - timedelta(days=10))
        collected = Collector(self.config, FakeClient([late_paper])).collect(window)
        self.assertTrue(collected.complete)
        self.assertEqual([paper.base_id for paper in collected.papers], ["2610.00001"])


class ArxivClientPaginationTests(unittest.TestCase):
    """Use arxiv.py's own Client.results pagination with fake Atom pages."""

    def _run(self, *, interrupt_third_page=False):
        try:
            import arxiv
        except ImportError:
            self.skipTest("arxiv package not installed")
        config = load_config()
        base = datetime(2026, 10, 1, tzinfo=timezone.utc)
        items = [raw(index, base + timedelta(minutes=index)) for index in range(1, 251)]
        client = arxiv.Client(page_size=100, delay_seconds=3.0, num_retries=0)
        pages = []
        failed = False

        def fake_page(url, first_page=True, _try_index=0):
            nonlocal failed
            arguments = parse_qs(urlparse(url).query)
            start = int(arguments["start"][0])
            query = arguments["search_query"][0]
            dates = re.search(r"submittedDate:\[(\d{12}) TO (\d{12})\]", query).groups()
            low, high = (datetime.strptime(value, "%Y%m%d%H%M").replace(tzinfo=timezone.utc) for value in dates)
            selected = [item for item in items if low <= item.published <= high]
            pages.append((start, len(selected)))
            if interrupt_third_page and start == 200 and not failed:
                failed = True
                raise RuntimeError("synthetic third-page interruption")
            return types.SimpleNamespace(results=selected[start:start + 100], header=types.SimpleNamespace(total_results=len(selected)))

        client._parse_feed = fake_page
        window = {"start": base.isoformat(), "end": (base + timedelta(days=1)).isoformat()}
        collected = Collector(config, client).collect(window)
        self.assertEqual(len(collected.papers), 250)
        self.assertIn("2610.00250", {paper.base_id for paper in collected.papers})
        self.assertIn((200, 250), pages)
        self.assertTrue(collected.complete)
        return collected

    def test_real_client_results_reaches_third_page(self):
        self._run()

    def test_real_client_results_interruption_recovers_by_split(self):
        collected = self._run(interrupt_third_page=True)
        self.assertIn("split", {shard["status"] for shard in collected.shards})


if __name__ == "__main__":
    unittest.main()
