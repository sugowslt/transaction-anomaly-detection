"""Exact grouped score attribution through the original classifier.

Coalitions replace entire named feature groups with normal-fit aggregates.
These are model explanations relative to that reference, not causal facts or
validated fraud probabilities. No tree representation is copied or simplified.
"""

from __future__ import annotations

import math
from collections import Counter

import numpy as np


METHOD = "exact-group-shapley-score-v1"
GROUP_LABELS = {
    "amount": "현재·과거 금액과 금액 대비",
    "transaction_conditions": "시간대·이체 방식·자금 종류",
    "sender_patterns": "출금 빈도와 수신처 반복",
    "elapsed_history": "이전 거래 이후 경과 기간",
    "intraday_outflow": "당일·최근 24시간 출금 패턴",
    "sender_inflow": "출금계좌의 과거 입금 흐름",
    "recipient_flow": "수신계좌의 과거 입출금 흐름",
}


def feature_groups(names):
    names = tuple(names)
    if not names or len(names) != len(set(names)):
        raise ValueError("Explanation feature names must be nonempty and unique")
    groups = {key: [] for key in GROUP_LABELS}
    for index, name in enumerate(names):
        lower = name.lower()
        if "amount" in lower or "net_" in lower:
            group = "amount"
        elif lower.startswith("days_since"):
            group = "elapsed_history"
        elif lower.startswith("graph_recipient"):
            group = "recipient_flow"
        elif lower.startswith("graph_sender"):
            group = "sender_inflow" if "inflow" in lower else "intraday_outflow"
        elif lower in {"time_bucket", "fund_code", "channel_code", "same_institution", "day_of_week"} or any(
            token in lower for token in ("same_channel", "same_fund", "same_time")
        ):
            group = "transaction_conditions"
        elif lower.startswith(("sender_", "recipient_")):
            group = "sender_patterns"
        else:
            raise ValueError(f"No reviewed explanation group for {name}")
        groups[group].append(index)
    return [(key, indices) for key, indices in groups.items() if indices]


def make_normal_reference(x, y, names, categorical_indices=(), chunk_rows=100_000):
    """Mean/mode over every normal fit row, bounded memory, no source rows."""
    names = tuple(names)
    if len(x) != len(y) or x.ndim != 2 or x.shape[1] != len(names) or chunk_rows <= 0:
        raise ValueError("Invalid reference fit matrix")
    feature_groups(names)
    categorical = tuple(categorical_indices)
    if len(set(categorical)) != len(categorical) or any(index < 0 or index >= len(names) for index in categorical):
        raise ValueError("Invalid categorical reference indices")
    sums = np.zeros(len(names), dtype=np.float64)
    modes = {index: Counter() for index in categorical}
    count = 0
    for start in range(0, len(y), chunk_rows):
        stop = min(start + chunk_rows, len(y))
        block = np.asarray(x[start:stop])[np.asarray(y[start:stop]) == 0]
        if not np.isfinite(block).all():
            raise ValueError("Nonfinite normal reference features")
        sums += block.sum(axis=0, dtype=np.float64)
        count += len(block)
        for index in categorical:
            values, frequencies = np.unique(block[:, index], return_counts=True)
            modes[index].update({float(value): int(frequency) for value, frequency in zip(values, frequencies)})
    if count == 0:
        raise ValueError("Reference requires normal fit rows")
    reference = sums / count
    for index in categorical:
        reference[index] = min(modes[index], key=lambda value: (-modes[index][value], value))
    return {
        "feature_names": list(names), "values": reference.tolist(),
        "normal_fit_rows": count, "categorical_indices": list(categorical),
        "source": "all normal fit rows only; numeric mean and categorical mode; no evaluation labels",
    }


def explain_score(model, snapshot, reference, expected_score=None):
    names = tuple(reference["feature_names"])
    if set(snapshot) != set(names):
        raise ValueError("Explanation snapshot/reference contract mismatch")
    current = np.asarray([snapshot[name] for name in names], dtype=np.float32)
    baseline = np.asarray(reference["values"], dtype=np.float32)
    if current.shape != baseline.shape or not np.isfinite(current).all() or not np.isfinite(baseline).all():
        raise ValueError("Explanation requires finite matching vectors")
    groups = feature_groups(names)
    size = len(groups)
    vectors = np.tile(baseline, (1 << size, 1))
    for mask in range(1 << size):
        for group_index, (_, indices) in enumerate(groups):
            if mask & (1 << group_index):
                vectors[mask, indices] = current[indices]
    # Both endpoints and all intermediate coalitions use this exact model.
    scores = np.asarray(model.predict_proba(vectors)[:, 1], dtype=np.float64)
    if scores.shape != (1 << size,) or not np.isfinite(scores).all() or (scores < 0).any() or (scores > 1).any():
        raise ValueError("Invalid model explanation scores")
    actual = float(scores[-1])
    if expected_score is not None and not math.isclose(actual, expected_score, abs_tol=1e-8, rel_tol=1e-7):
        raise ValueError("Explained score differs from original model score")
    contributions = []
    denominator = math.factorial(size)
    for group_index, (key, indices) in enumerate(groups):
        contribution = 0.0
        for mask in range(1 << size):
            if mask & (1 << group_index):
                continue
            members = mask.bit_count()
            weight = math.factorial(members) * math.factorial(size - members - 1) / denominator
            contribution += weight * (scores[mask | (1 << group_index)] - scores[mask])
        contributions.append({
            "group": key, "label": GROUP_LABELS[key], "contribution": float(contribution),
            "features": [names[index] for index in indices],
        })
    reconstructed = float(scores[0]) + sum(item["contribution"] for item in contributions)
    if not math.isclose(reconstructed, actual, abs_tol=1e-10, rel_tol=1e-9):
        raise ValueError("Model explanation does not reconstruct its score")
    return {
        "method": METHOD, "outputUnit": "model_score_0_to_1",
        "baselineScore": float(scores[0]), "explainedScore": actual,
        "reconstructionError": abs(reconstructed - actual),
        "coalitionsEvaluated": len(scores), "normalReferenceRows": int(reference["normal_fit_rows"]),
        "groups": contributions,
        "interpretation": "Relative to normal-fit mean/mode reference; depends on grouping and correlated features; not causal or proof of fraud",
    }
