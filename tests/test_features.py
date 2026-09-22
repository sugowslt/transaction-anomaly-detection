import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from train_card_baseline import CATEGORICAL, features, period


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.row = {
            "통합승인금액": "25000", "카드이용한도금액": "1000000",
            "승인시간대": "14", "경과일수_최종이용일자": "3",
            "전월_매출건수": "8", "전월_매출금액": "200000",
            "가맹점누적매출금액_구간화": "2", "연령": "4",
            "할부가능개월수": "12", "이상거래여부": "0",
            "이상거래유형": "", "이상거래설명": "",
            "카드KEY": "CD1", "가맹점KEY": "ST1",
        }
        self.row.update({name: "A" for name in CATEGORICAL})

    def test_labels_and_identifiers_do_not_change_features(self):
        mappings = {name: {} for name in CATEGORICAL}
        original = features(self.row, mappings, fit=True)
        changed = dict(self.row)
        changed.update({
            "이상거래여부": "1", "이상거래유형": "fraud",
            "이상거래설명": "different", "카드KEY": "CD2", "가맹점KEY": "ST2",
        })
        self.assertEqual(original, features(changed, mappings, fit=False))

    def test_unknown_category_is_missing_without_changing_mapping(self):
        mappings = {name: {} for name in CATEGORICAL}
        features(self.row, mappings, fit=True)
        changed = dict(self.row, 국내해외여부="new")
        vector = features(changed, mappings, fit=False)
        self.assertTrue(math.isnan(vector[10]))
        self.assertEqual(mappings["국내해외여부"], {"A": 0})

    def test_period_from_quarter_filename(self):
        filename = Path("21-2_카드데이터_2024_3분기.csv")
        self.assertEqual(period(filename), (2024, 3))


if __name__ == "__main__":
    unittest.main()
