"""Optional publication exports. Importing analysis does not require plotting libraries."""

import json
from importlib import import_module
from pathlib import Path
from typing import Any


def pareto_plot(analysis_dir: Path, destination: Path) -> None:
    """Render cache-aware cost upper bound vs graded pass@1; no synthetic evidence."""
    plt: Any = import_module("matplotlib.pyplot")
    rows = [json.loads(line) for line in (analysis_dir / "runs.jsonl").read_text().splitlines()]
    labels = sorted({(row["arm"], row["variant"]) for row in rows})
    figure, axes = plt.subplots(figsize=(7, 4.5))
    count = 0
    for arm, variant in labels:
        selected = [row for row in rows if (row["arm"], row["variant"]) == (arm, variant)]
        # Never hide a missing grade or an unmeasured cost from a plotted arm.
        if not selected or any(
            row["resolved"] is None or not row["cost_measured"] for row in selected
        ):
            continue
        cost = sum(row["cost_upper_usd"] for row in selected) / len(selected)
        success = sum(row["resolved"] for row in selected) / len(selected)
        axes.scatter([cost], [success])
        axes.annotate(f"{arm}/{variant} (n={len(selected)})", (cost, success))
        count += 1
    axes.set(
        xlabel="Cache-aware API cost upper bound / task (USD)",
        ylabel="Graded pass@1",
        title=f"Dev exploratory comparison; {count} complete arms; cloud cost excluded",
    )
    axes.set_ylim(0, 1)
    figure.tight_layout()
    figure.savefig(destination, dpi=180)
    plt.close(figure)
