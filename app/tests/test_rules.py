from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from arxivdaily.config import load_config
from arxivdaily.models import Paper
from arxivdaily.rules import match_rules


STAMP = datetime(2026, 10, 1, tzinfo=timezone.utc).isoformat()
TEST_TMP = Path(__file__).resolve().parents[1] / "tmp" / "tests"
TEST_TMP.mkdir(parents=True, exist_ok=True)


def paper(title: str, abstract: str, categories: list[str] | None = None) -> Paper:
    return Paper("2610.00001v1", title, abstract, categories or ["cs.CV"], STAMP, STAMP)


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()

    def test_title_only_is_not_a_default_rule_hit(self):
        item = paper("Medical spatial reasoning segmentation", "An image dataset is introduced.")
        self.assertEqual(match_rules(item, self.config), [])
        self.config["rules"]["title_enabled"] = True
        self.assertIn("medical_reasoning", match_rules(item, self.config))

    def test_cross_listed_category_and_multiple_routes(self):
        item = paper("Untitled", "We use DPO for medical vessel segmentation and topology optimization.", ["stat.ML", "cs.CV"])
        self.assertEqual(match_rules(item, self.config), ["medical_objective", "transferable_objective"])

    def test_hyphens_case_and_acronym_boundaries(self):
        item = paper("Untitled", "Medical pixel-level perception with position-reasoning and DPO.")
        self.assertIn("medical_reasoning", match_rules(item, self.config))
        unrelated = paper("Untitled", "A setup is used for medical segmentation with spatial reasoning.")
        self.assertNotIn("medical_objective", match_rules(unrelated, self.config))

    def test_objective_recall_does_not_require_theory_or_transfer_words(self):
        item = paper("Untitled", "We introduce a topology loss for vessel segmentation and report structural validity.")
        self.assertIn("transferable_objective", match_rules(item, self.config))
        theoretical = paper("Untitled", "A theoretical topology objective for segmentation derives a new constraint without experiments.")
        self.assertIn("transferable_objective", match_rules(theoretical, self.config))

    def test_real_replay_metadata_matches_expected_candidate_routes(self):
        manifest_path = Path(__file__).resolve().parents[1] / "fixtures" / "sources" / "manifest.json"
        if not manifest_path.exists():
            self.skipTest("version-pinned replay metadata unavailable")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        papers = {entry["version_id"]: Paper.from_dict(entry["metadata"]) for entry in manifest["papers"]}
        self.assertEqual(set(papers), {"2308.00692v3", "2504.11008v2", "2505.11872v4", "2603.19169v1"})
        self.assertIn("general_reasoning", match_rules(papers["2308.00692v3"], self.config))
        self.assertIn("medical_reasoning", match_rules(papers["2504.11008v2"], self.config))
        self.assertIn("medical_reasoning", match_rules(papers["2505.11872v4"], self.config))
        self.assertIn("medical_objective", match_rules(papers["2603.19169v1"], self.config))

    def test_exclusion_is_abstract_only_and_routes_are_configurable(self):
        self.config["rules"]["exclude"] = ["survey"]
        item = paper("A survey", "Medical image segmentation with spatial reasoning.")
        self.assertIn("medical_reasoning", match_rules(item, self.config))
        item.abstract += " This is a survey."
        self.assertEqual(match_rules(item, self.config), [])
        self.config["rules"]["routes"]["general_reasoning"] = []
        self.assertNotIn("general_reasoning", match_rules(paper("x", "Spatial reasoning for segmentation."), self.config))

    def test_custom_group_merge_and_no_secret_values(self):
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as temp:
            path = Path(temp) / "config.toml"
            path.write_text('[rules.groups]\nSEG = ["segmentation"]\nCUSTOM = ["pixel inference"]\n[rules.routes]\nnew = [["SEG", "CUSTOM"]]\n', encoding="utf-8")
            config = load_config(path)
            # Supplied routes replace the built-in scheme; groups stay extensible.
            self.assertIn("CUSTOM", config["rules"]["groups"])
            self.assertIn("SEG", config["rules"]["groups"])
            self.assertEqual(set(config["rules"]["routes"]), {"new"})
            self.assertEqual(config["selection"]["route_priority"], ["new"])
            self.assertIn("new", match_rules(paper("x", "Pixel inference for segmentation."), config))
            path.write_text('[llm.filter]\napi_key = "should-not-be-here"\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_config(path)

    def test_business_caps_and_free_delivery_limits_are_validated(self):
        bad_values = [
            '[collection]\ninitial_days = 8\n',
            '[collection]\nlookback_days = 15\n',
            '[collection]\npage_size = 2001\n',
            '[collection]\nmax_candidates = 10001\n',
            '[collection]\ndelay_seconds = 2.9\n',
            '[limits]\nfull_daily_limit = 11\n',
            '[limits]\nreview_daily_limit = -1\n',
            '[limits]\nstage_attempts = 0\n',
            '[limits]\nrecovery_days = 15\n',
            '[delivery]\ndaily_limit = 6\n',
            '[delivery]\npoll_limit = 4\n',
            '[delivery]\nchain_attempts = 4\n',
            '[delivery]\nbody_bytes = 32769\n',
            '[delivery]\nquery_counts_quota = 1\n',
            '[delivery]\nquota_timezone = "Invalid/Nowhere"\n',
            '[llm.pricing]\ncache_hit_per_million = 0.5\n',
            '[llm.filter]\nmodel = "other-model"\n',
            '[rules]\ncategories = ["cs.CV OR all:electron"]\n',
        ]
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as temp:
            path = Path(temp) / "config.toml"
            for value in bad_values:
                with self.subTest(value=value):
                    path.write_text(value, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_config(path)
            path.write_text('[limits]\nreview_daily_limit = 0\n[delivery]\npoll_limit = 0\n[llm.pricing]\npolicy = "custom"\ncache_hit_per_million = 0.5\n', encoding="utf-8")
            self.assertEqual(load_config(path)["limits"]["review_daily_limit"], 0)

    def test_llm_and_fulltext_resource_settings_are_configurable_and_bounded(self):
        bad_values = [
            '[llm.filter]\neffort = "low"\n',
            '[llm.summary]\neffort = "high"\n',
            '[llm.filter]\nprotocol = "responses"\n',
            '[llm.filter]\nbase_url = "http://api.example.com"\n',
            '[llm.filter]\nbase_url = "https://user:pass@api.example.com"\n',
            '[llm.filter]\nbase_url = "https://api.example.com?key=secret"\n',
            '[llm.filter]\nbase_url = "https://api.example.com#fragment"\n',
            '[llm.filter]\nbase_url = "https://127.0.0.1"\n',
            '[llm.filter]\nmax_tokens = 0\n',
            '[llm.filter]\ninput_chars = 100\n',
            '[llm.filter]\nattempts = 0\n',
            '[llm.filter]\ntimeout = 0\n',
            '[llm.filter]\nstage_seconds = 0\n',
            '[llm.summary]\nmax_section_calls = 0\n',
            '[fulltext]\ntimeout = 0\n',
            '[fulltext]\nattempts = 0\n',
            '[fulltext]\nmin_chars = 0\n',
            '[fulltext]\nchunk_chars = 1\n',
            '[fulltext]\nmax_download_bytes = 100\n',
            '[fulltext]\nmax_visual_pages = -1\n',
        ]
        with tempfile.TemporaryDirectory(dir=TEST_TMP) as temp:
            path = Path(temp) / "config.toml"
            for value in bad_values:
                with self.subTest(value=value):
                    path.write_text(value, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_config(path)
            path.write_text('[llm.filter]\nstage_seconds = 90\n[llm.summary]\nmax_section_calls = 32\n[fulltext]\nmax_visual_pages = 4\n', encoding="utf-8")
            custom = load_config(path)
            self.assertEqual(custom["llm"]["filter"]["stage_seconds"], 90)
            self.assertEqual(custom["llm"]["summary"]["max_section_calls"], 32)
            self.assertEqual(custom["fulltext"]["max_visual_pages"], 4)


if __name__ == "__main__":
    unittest.main()
