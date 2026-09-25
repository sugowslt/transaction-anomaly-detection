import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_release import DEMOS, verify_demo


class CheckReleaseTests(unittest.TestCase):
    def test_packaged_demo_scores_match_the_models(self):
        root = Path(__file__).resolve().parents[1]
        for _, name, amount_field, scorer in DEMOS:
            with self.subTest(name=name):
                self.assertEqual(8, verify_demo(root / "reports" / name, amount_field, scorer))

    def test_stale_demo_score_is_rejected(self):
        root = Path(__file__).resolve().parents[1]
        _, name, amount_field, scorer = DEMOS[1]
        examples = json.loads((root / "reports" / name).read_text(encoding="utf-8"))
        examples[0]["riskScore"] = 0.5
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / name
            path.write_text(json.dumps(examples, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "TRANSFER-01"):
                verify_demo(path, amount_field, scorer)


if __name__ == "__main__":
    unittest.main()
