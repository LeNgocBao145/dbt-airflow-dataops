"""Validate Colibri's column lineage artifact and normalize its HTML output."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def validate_column_lineage(manifest_path: Path) -> int:
    """Return the number of column-level edges or fail if none were generated."""
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Colibri manifest not found: {manifest_path}")

    report = json.loads(manifest_path.read_text(encoding="utf-8"))
    edges = report.get("lineage", {}).get("edges", [])
    column_edges = [edge for edge in edges if edge.get("sourceColumn") and edge.get("targetColumn")]
    if not column_edges:
        raise ValueError(
            "Colibri generated no column-level lineage. "
            "Check that dbt compile ran successfully before dbt docs generate."
        )
    return len(column_edges)


def normalize_html(html_path: Path) -> bool:
    """Trim trailing whitespace from generated HTML; return whether it changed."""
    if not html_path.is_file():
        raise FileNotFoundError(f"Colibri HTML not found: {html_path}")

    html = html_path.read_text(encoding="utf-8")
    normalized = "\n".join(line.rstrip() for line in html.splitlines()) + "\n"
    if normalized == html:
        return False
    with html_path.open("w", encoding="utf-8", newline="\n") as output:
        output.write(normalized)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--html",
        type=Path,
        default=Path("pages/lineage/index.html"),
        help="Path to Colibri's generated HTML file.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("pages/lineage/colibri-manifest.json"),
        help="Path to Colibri's generated lineage JSON.",
    )
    args = parser.parse_args()

    try:
        column_edges = validate_column_lineage(args.manifest)
        changed = normalize_html(args.html)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")

    print(f"Colibri column lineage edges: {column_edges}")
    print(f"HTML whitespace normalized: {'yes' if changed else 'no changes needed'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
