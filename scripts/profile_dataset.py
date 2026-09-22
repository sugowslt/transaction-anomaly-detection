"""Stream the AI Hub CSV files and summarize their labels and schemas."""

import csv
import json
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
GROUPS = (
    ("training", "카드거래"),
    ("training", "전자금융공동망"),
    ("validation", "카드거래"),
    ("validation", "전자금융공동망"),
)


def profile_file(path: Path) -> dict:
    labels = Counter()
    types = Counter()
    descriptions = Counter()
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        columns = reader.fieldnames or []
        required = {"이상거래여부", "이상거래유형", "이상거래설명"}
        if not required.issubset(columns):
            raise ValueError(f"Missing label columns: {path}")
        for row in reader:
            labels[row["이상거래여부"]] += 1
            types[row["이상거래유형"]] += 1
            descriptions["present" if row["이상거래설명"] else "empty"] += 1
    return {
        "file": path.name,
        "rows": sum(labels.values()),
        "labels": dict(sorted(labels.items())),
        "anomaly_types": dict(sorted(types.items())),
        "description_presence": dict(sorted(descriptions.items())),
        "columns": columns,
    }


def main() -> None:
    results = []
    for split, category in GROUPS:
        directory = DATA / split / category
        files = sorted(directory.glob("*.csv"))
        if not files:
            raise FileNotFoundError(f"No CSV files in {directory}")
        file_profiles = [profile_file(path) for path in files]
        schemas = {tuple(item["columns"]) for item in file_profiles}
        labels = Counter()
        types = Counter()
        for item in file_profiles:
            labels.update(item["labels"])
            types.update(item["anomaly_types"])
        results.append({
            "split": split,
            "category": category,
            "files": len(files),
            "rows": sum(item["rows"] for item in file_profiles),
            "labels": dict(sorted(labels.items())),
            "anomaly_types": dict(sorted(types.items())),
            "schema_variants": len(schemas),
            "columns": file_profiles[0]["columns"],
            "by_file": file_profiles,
        })
    destination = ROOT / "reports" / "data_profile.json"
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(
        json.dumps({"groups": results}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"groups": len(results), "rows": sum(item["rows"] for item in results)}))


if __name__ == "__main__":
    main()
