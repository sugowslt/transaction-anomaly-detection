import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from release_assets import KINDS, model_version, verify_report_versions


class ReleaseAssetsTests(unittest.TestCase):
    def test_reports_must_match_the_packaged_model_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "models").mkdir()
            (root / "reports").mkdir()
            for kind in KINDS:
                (root / "models" / f"{kind}_baseline.skops").write_bytes(kind.encode())
                for suffix in ("", "_audit"):
                    (root / "reports" / f"{kind}_baseline{suffix}.json").write_text(
                        json.dumps({"model_version": model_version(kind, root)}), encoding="utf-8"
                    )
            tradeoff = {"models": {kind: {"model_version": model_version(kind, root)} for kind in KINDS}}
            (root / "reports" / "threshold_tradeoff.json").write_text(json.dumps(tradeoff), encoding="utf-8")
            verify_report_versions(root)

            (root / "models" / "bank_baseline.skops").write_bytes(b"changed model")
            with self.assertRaisesRegex(ValueError, "bank_baseline.json model_version does not match"):
                verify_report_versions(root)

            (root / "reports" / "bank_baseline.json").write_text(
                json.dumps({"model_version": model_version("bank", root)}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "bank_baseline_audit.json model_version does not match"):
                verify_report_versions(root)

            (root / "reports" / "bank_baseline_audit.json").write_text(
                json.dumps({"model_version": model_version("bank", root)}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "threshold_tradeoff.json bank model_version does not match"):
                verify_report_versions(root)

    def test_report_without_model_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "models").mkdir()
            (root / "reports").mkdir()
            (root / "reports" / "threshold_tradeoff.json").write_text("{}", encoding="utf-8")
            (root / "models" / "card_baseline.skops").write_bytes(b"card")
            (root / "reports" / "card_baseline.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "card_baseline.json model_version does not match"):
                verify_report_versions(root)


if __name__ == "__main__":
    unittest.main()
