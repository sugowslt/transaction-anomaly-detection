"""Load only the reviewed model types from the distributable skops artifact."""

import skops.io as sio

from release_assets import ROOT, model_version


MODEL_PATHS = {
    "card": ROOT / "models" / "card_baseline.skops",
    "bank": ROOT / "models" / "bank_baseline.skops",
}
TRUSTED_TYPES = (
    "functools.partial",
    "sklearn.ensemble._hist_gradient_boosting.predictor.TreePredictor",
    "sklearn.utils.validation.check_array",
)


def load_artifact(kind: str = "card"):
    path = MODEL_PATHS[kind]
    unknown = set(sio.get_untrusted_types(file=path))
    unexpected = unknown - set(TRUSTED_TYPES)
    if unexpected:
        raise ValueError(f"Unexpected model types: {sorted(unexpected)}")
    return sio.load(path, trusted=TRUSTED_TYPES)
