import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import parser
from anki_export import _dedupe_cards, build_apkg
from critic import _evidence_exists, _numbers_consistent, critic_pass
from qa import _bm25_score, _load_doc_data


class TestP14CostFilter(unittest.TestCase):
    def test_skip_section_blacklist(self):
        sections = [
            {"title": "References", "page_start": 10, "page_texts": [(10, "[1] foo")]},
            {"title": "Method", "page_start": 3, "page_texts": [(3, "we propose")]},
        ]
        chunks = parser.build_chunks(sections)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["section"], "Method")


class TestP21Critic(unittest.TestCase):
    def test_evidence_exists(self):
        self.assertTrue(_evidence_exists("accuracy is 95%", "The accuracy is 95% on test set."))
        self.assertFalse(_evidence_exists("accuracy is 99%", "The accuracy is 95% on test set."))

    def test_numbers_consistent(self):
        self.assertTrue(_numbers_consistent("accuracy is 95%", "accuracy is 95%"))
        self.assertFalse(_numbers_consistent("dataset has 1000 images", "dataset has 500 images"))

    def test_critic_pass(self):
        import asyncio
        main_points = [
            {"point": "Accuracy is 95%", "evidence": "accuracy 95%", "page": 1, "page_verified": True},
            {"point": "Dataset has 1000 images", "evidence": "dataset 500 images", "page": 2, "page_verified": True},
        ]
        chunks = [
            {"page_from": 1, "page_to": 1, "text": "accuracy 95%"},
            {"page_from": 2, "page_to": 2, "text": "dataset 500 images"},
        ]
        out = asyncio.run(critic_pass(main_points, chunks))
        self.assertEqual(out[0]["critic_status"], "verified")
        self.assertEqual(out[1]["critic_status"], "rejected")


class TestP22SSE(unittest.TestCase):
    def test_analyze_emits_events(self):
        import asyncio
        from analyzer import analyze

        events = []
        async def cb(t, p):
            events.append((t, p.get("progress")))

        doc = {
            "doc_id": "demo_doc",
            "title": "Demo",
            "author": "",
            "n_pages": 2,
            "sections": [{"title": "Sec", "page_start": 1, "page_end": 2}],
            "chunks": [{"section": "Sec", "page_from": 1, "page_to": 2, "text": "demo text"}],
        }
        report = asyncio.run(analyze(doc, "demo", "", "gpt-4o-mini", event_cb=cb))
        self.assertIn("main_points", report)


class TestP23QA(unittest.TestCase):
    def test_bm25_ranking(self):
        scores = _bm25_score("method accuracy", [
            "we propose a method with high accuracy.",
            "the dataset contains images.",
        ])
        self.assertGreater(scores[0], scores[1])


class TestP24Anki(unittest.TestCase):
    def test_dedupe(self):
        cards = [
            {"front": "Q1", "back": "A1"},
            {"front": " q1 ", "back": "A2"},
            {"front": "Q2", "back": "B"},
        ]
        out = _dedupe_cards(cards)
        self.assertEqual(len(out), 2)

    def test_build_apkg(self):
        report = {
            "title": "Test",
            "cards": [
                {"front": "F1", "back": "B1", "page": 1, "section": "Intro"},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "t.apkg"
            build_apkg(report, "doc", out)
            self.assertTrue(out.exists() and out.stat().st_size > 100)


if __name__ == "__main__":
    unittest.main()
