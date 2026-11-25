#!/usr/bin/env python3
"""Generate Recce presets for changed and downstream table models."""

import argparse
import json
from pathlib import Path


def read_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def impacted_table_models(base: dict, current: dict) -> list[str]:
    base_nodes = {k: v for k, v in base.get("nodes", {}).items() if v.get("resource_type") == "model"}
    current_nodes = {k: v for k, v in current.get("nodes", {}).items() if v.get("resource_type") == "model"}
    changed = set()
    for unique_id, node in current_nodes.items():
        previous = base_nodes.get(unique_id)
        if previous is None:
            changed.add(unique_id)
            continue
        old_checksum = (previous.get("checksum") or {}).get("checksum")
        new_checksum = (node.get("checksum") or {}).get("checksum")
        old_deps = previous.get("depends_on", {}).get("nodes", [])
        new_deps = node.get("depends_on", {}).get("nodes", [])
        if old_checksum != new_checksum or old_deps != new_deps:
            changed.add(unique_id)

    children = {}
    for node in list(base_nodes.values()) + list(current_nodes.values()):
        child = node.get("unique_id")
        if not child:
            continue
        for parent in node.get("depends_on", {}).get("nodes", []):
            if parent.startswith("model."):
                children.setdefault(parent, set()).add(child)

    impacted = set(changed)
    queue = list(changed)
    while queue:
        parent = queue.pop(0)
        for child in children.get(parent, set()):
            if child not in impacted:
                impacted.add(child)
                queue.append(child)

    models = []
    for unique_id in sorted(impacted):
        node = current_nodes.get(unique_id)
        materialized = (node.get("config") or {}).get("materialized") if node else None
        if node and materialized in {"table", "incremental"}:
            models.append(node.get("name", unique_id.rsplit(".", 1)[-1]))
    return sorted(set(models))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-manifest", type=Path, required=True)
    parser.add_argument("--current-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    models = impacted_table_models(read_manifest(args.base_manifest), read_manifest(args.current_manifest))
    lines = [
        "# Generated from Base/Current manifests by scripts/generate_recce_config.py",
        "checks:",
        "  - name: Row count diff for changed tables",
        "    description: Compare row counts for changed table models and downstream tables.",
        "    type: row_count_diff",
        "    params:",
        "      select: state:modified+,config.materialized:table",
        "      view_mode: changed_models",
    ]
    for model in models:
        lines.extend(
            [
                f"  - name: Profile diff — {model}",
                f"    description: Compare every column in impacted table model {model}.",
                "    type: profile_diff",
                "    params:",
                f"      model: {model}",
                # Deliberately omit `columns`; Recce profiles the full model.
            ]
        )
    if "gld_sales_summary" in models:
        lines.extend(
            [
                "  - name: Sales summary revenue metrics diff",
                "    description: Compare total revenue sum, mean, and null count between Base and Current.",
                "    type: query_diff",
                "    params:",
                "      sql_template: |",
                "        select",
                "          sum(total_revenue) as total_revenue_sum,",
                "          avg(cast(total_revenue as float)) as total_revenue_mean,",
                "          sum(case when total_revenue is null then 1 else 0 end) as total_revenue_null_count",
                "        from {{ ref('gld_sales_summary') }}",
            ]
        )
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Generated profile_diff checks for {len(models)} impacted table model(s): {', '.join(models) or 'none'}")


if __name__ == "__main__":
    main()
