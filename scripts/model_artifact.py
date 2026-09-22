"""Load only the reviewed model types from the distributable skops artifact."""

import skops.io as sio

from train_card_baseline import ROOT


MODEL_PATH = ROOT / "models" / "card_baseline.skops"
TRUSTED_TYPES = (
    "functools.partial",
    "sklearn.ensemble._hist_gradient_boosting.predictor.TreePredictor",
    "sklearn.utils.validation.check_array",
)


def load_artifact():
    unknown = set(sio.get_untrusted_types(file=MODEL_PATH))
    unexpected = unknown - set(TRUSTED_TYPES)
    if unexpected:
        raise ValueError(f"Unexpected model types: {sorted(unexpected)}")
    return sio.load(MODEL_PATH, trusted=TRUSTED_TYPES)
