#!/usr/bin/env python3
"""Build a reviewer-friendly Recce PR comment from Recce and dbt artifacts."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable


CHECK_TYPES = {"row_count_diff", "profile_diff"}


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def key_map(row: dict[str, Any]) -> dict[str, Any]:
    return {str(k).lower().replace(" ", "_"): v for k, v in row.items()}


def rows_from(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    if not isinstance(value, dict):
        return []
    for row_key in ("rows", "records", "data", "values"):
        data = value.get(row_key)
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return data
        columns = value.get("columns", value.get("headers", value.get("column_names")))
        if isinstance(columns, list) and isinstance(data, list):
            names = [c.get("name", c.get("column", "")) if isinstance(c, dict) else str(c) for c in columns]
            if data and isinstance(data[0], (list, tuple)):
                return [dict(zip(names, row)) for row in data]
        if isinstance(columns, list) and isinstance(data, dict):
            names = [c.get("name", c.get("column", "")) if isinstance(c, dict) else str(c) for c in columns]
            length = max((len(v) for v in data.values() if isinstance(v, list)), default=0)
            return [{name: data.get(name, [None] * length)[i] for name in names} for i in range(length)]
    if any(k in value for k in ("column_name", "row_count", "base_row_count", "current_row_count")):
        return [value]
    return []


def walk(value: Any, context: dict[str, Any] | None = None) -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    context = context or {}
    if isinstance(value, dict):
        local = dict(context)
        kind = value.get("type", value.get("check_type", value.get("task_type")))
        if isinstance(kind, str) and kind.lower().replace(" ", "_") in CHECK_TYPES:
            local["kind"] = kind.lower().replace(" ", "_")
        label = str(value.get("name", "")).lower()
        if "row count" in label:
            local["kind"] = "row_count_diff"
        elif "profile diff" in label:
            local["kind"] = "profile_diff"
        params = value.get("params") if isinstance(value.get("params"), dict) else {}
        for key in ("model", "node_name", "node_id"):
            candidate = params.get(key, value.get(key))
            if isinstance(candidate, str) and candidate:
                local["model"] = candidate
                break
        if "base" in value and "current" in value:
            yield value, local
        for child in value.values():
            yield from walk(child, local)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child, context)


def number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value if math.isfinite(float(value)) else None
    if isinstance(value, str):
        try:
            n = float(value.replace(",", ""))
            return int(n) if n.is_integer() else n
        except ValueError:
            return None
    return None


def display(value: Any) -> str:
    n = number(value)
    if n is not None:
        return f"{n:,.2f}" if isinstance(n, float) and not n.is_integer() else f"{int(n):,}"
    return "N/A" if value is None else str(value)


def model_name(value: Any, manifest_names: dict[str, str]) -> str:
    if not isinstance(value, str):
        return "Unknown model"
    if value in manifest_names:
        return manifest_names[value]
    if value.startswith(("model.", "seed.", "snapshot.")):
        return value.rsplit(".", 1)[-1]
    return value


def profile_values(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for row in records:
        data = key_map(row)
        col = data.get("column_name", data.get("column", data.get("name")))
        if not col:
            continue
        values = {}
        for metric in ("avg", "min", "max", "null_count", "row_count"):
            if metric in data:
                values[metric] = data[metric]
        if "null_count" not in values and "row_count" in data and "not_null_proportion" in data:
            total, prop = number(data["row_count"]), number(data["not_null_proportion"])
            if total is not None and prop is not None:
                values["null_count"] = round(total * (1 - prop))
        out[str(col)] = values
    return out


def changed_value(left: Any, right: Any) -> bool:
    l_num, r_num = number(left), number(right)
    if l_num is not None and r_num is not None:
        return not math.isclose(float(l_num), float(r_num), rel_tol=1e-9, abs_tol=1e-9)
    return left != right


def row_count_value(row: dict[str, Any]) -> Any:
    data = key_map(row)
    return next((data[k] for k in ("row_count", "count", "rows", "value") if k in data), None)


def state_metrics(
    state: Any, manifest_names: dict[str, str]
) -> tuple[dict[str, tuple[Any, Any]], list[tuple[str, str, Any, Any, str]]]:
    row_counts: dict[str, tuple[Any, Any]] = {}
    profile_counts: dict[str, tuple[Any, Any]] = {}
    profile_diffs: dict[tuple[str, str, str], tuple[str, str, Any, Any, str]] = {}
    for pair, ctx in walk(state):
        kind = ctx.get("kind", "")
        model = model_name(ctx.get("model"), manifest_names)
        base, current = pair["base"], pair["current"]
        b_rows, c_rows = rows_from(base), rows_from(current)
        if kind == "profile_diff" or any("column_name" in key_map(r) for r in b_rows + c_rows):
            b_profile, c_profile = profile_values(b_rows), profile_values(c_rows)
            for col in sorted(set(b_profile) & set(c_profile)):
                for metric in ("avg", "min", "max", "null_count"):
                    bv, cv = b_profile[col].get(metric), c_profile[col].get(metric)
                    if bv is not None and cv is not None and changed_value(bv, cv):
                        label = "Mean" if metric == "avg" else metric
                        profile_diffs[(model, col, metric)] = (model, col, bv, cv, label)
            b_count = next((r.get("row_count") for r in b_profile.values() if r.get("row_count") is not None), None)
            c_count = next((r.get("row_count") for r in c_profile.values() if r.get("row_count") is not None), None)
            if b_count is not None and c_count is not None:
                profile_counts[model] = (b_count, c_count)

        # Recce versions serialize row-count checks either as paired numeric values
        # or as one table per side with model/name and row_count columns.
        has_row_counts = any(
            any(k in key_map(r) for k in ("row_count", "base_row_count", "current_row_count")) for r in b_rows + c_rows
        )
        if kind == "row_count_diff" or has_row_counts:
            b_by_model, c_by_model = {}, {}
            for side_rows, target in ((b_rows, b_by_model), (c_rows, c_by_model)):
                for row in side_rows:
                    data = key_map(row)
                    node = data.get("model", data.get("node_name", data.get("node_id", ctx.get("model"))))
                    side_model = model_name(node, manifest_names)
                    count = row_count_value(row)
                    if count is not None:
                        target[side_model] = count
            for side_model in set(b_by_model) & set(c_by_model):
                row_counts[side_model] = (b_by_model[side_model], c_by_model[side_model])
            for row in b_rows + c_rows:
                data = key_map(row)
                b = next(
                    (
                        data[k]
                        for k in ("base_row_count", "base_count", "base_rows", "row_count_base", "base")
                        if k in data
                    ),
                    None,
                )
                c = next(
                    (
                        data[k]
                        for k in ("current_row_count", "current_count", "current_rows", "row_count_current", "current")
                        if k in data
                    ),
                    None,
                )
                if b is not None and c is not None:
                    node = data.get("model", data.get("node_name", data.get("node_id", ctx.get("model"))))
                    row_counts[model_name(node, manifest_names)] = (b, c)
            if not b_rows and not c_rows:
                b_num, c_num = number(base), number(current)
                if b_num is not None and c_num is not None:
                    row_counts[model] = (b_num, c_num)
    for model, counts in profile_counts.items():
        row_counts.setdefault(model, counts)
    return row_counts, list(profile_diffs.values())


def manifest_diff(base: Any, current: Any) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    b_nodes = {k: v for k, v in (base or {}).get("nodes", {}).items() if v.get("resource_type") == "model"}
    c_nodes = {k: v for k, v in (current or {}).get("nodes", {}).items() if v.get("resource_type") == "model"}
    names = {k: v.get("name", k.rsplit(".", 1)[-1]) for k, v in {**b_nodes, **c_nodes}.items()}
    changed, added, removed = [], [], []
    for uid in sorted(set(b_nodes) | set(c_nodes)):
        if uid not in b_nodes:
            added.append(uid)
        elif uid not in c_nodes:
            removed.append(uid)
        else:
            b, c = b_nodes[uid], c_nodes[uid]
            b_checksum = (b.get("checksum") or {}).get("checksum")
            c_checksum = (c.get("checksum") or {}).get("checksum")
            if b_checksum != c_checksum or b.get("depends_on", {}).get("nodes") != c.get("depends_on", {}).get("nodes"):
                changed.append(uid)
    children: dict[str, set[str]] = {}
    for node in list(b_nodes.values()) + list(c_nodes.values()):
        child = node.get("unique_id")
        if not child:
            continue
        for parent in node.get("depends_on", {}).get("nodes", []):
            if parent.startswith("model."):
                children.setdefault(parent, set()).add(child)
    impacted = set(changed + added + removed)
    queue = list(impacted)
    while queue:
        for child in children.get(queue.pop(0), set()):
            if child not in impacted:
                impacted.add(child)
                queue.append(child)
    labels = {**names}
    downstream_edges = []
    for parent in impacted:
        for child in children.get(parent, set()):
            if child in impacted:
                downstream_edges.append((labels.get(parent, parent), labels.get(child, child)))
    return (
        [labels.get(x, x) for x in changed],
        [labels.get(x, x) for x in sorted(impacted)],
        sorted(set(downstream_edges)),
    )


def lineage_markdown(changed: list[str], impacted: list[str], edges: list[tuple[str, str]]) -> str:
    lines = ["## Lineage & Blast Radius", ""]
    if not changed:
        lines += ["No changed dbt model was found between Base and Current.", ""]
        return "\n".join(lines)
    lines.append("**Changed models:** " + ", ".join(f"`{x}`" for x in changed))
    lines.append("")
    lines.append("**Affected models (including downstream):** " + ", ".join(f"`{x}`" for x in impacted))
    lines += ["", "```mermaid", "flowchart LR"]
    ids = {name: f"m{i}" for i, name in enumerate(impacted)}
    changed_set = set(changed)
    for name in impacted:
        css = ":::changed" if name in changed_set else ":::downstream"
        lines.append(f'  {ids[name]}["{name}"]{css}')
    for parent, child in edges:
        if parent in ids and child in ids:
            lines.append(f"  {ids[parent]} --> {ids[child]}")
    lines += [
        "  classDef changed fill:#ffe0e0,stroke:#c00,stroke-width:2px",
        "  classDef downstream fill:#e8f1ff,stroke:#4682b4",
        "```",
        "",
    ]
    return "\n".join(lines)


def row_count_markdown(counts: dict[str, tuple[Any, Any]]) -> str:
    lines = ["### Row Count", "", "| Model | Base Rows | Current Rows | Diff (%) |", "|---|---:|---:|---:|"]
    if not counts:
        lines.append("| No row-count values were found in the Recce state | — | — | — |")
    for model, (base, current) in sorted(counts.items()):
        b, c = number(base), number(current)
        pct = "N/A"
        if b is not None and c is not None:
            diff = c - b
            pct_value = (diff / b * 100) if b else (0 if diff == 0 else None)
            pct = f"{diff:+,.0f} ({pct_value:+.2f}%)" if pct_value is not None else f"{diff:+,.0f} (N/A)"
        lines.append(f"| `{model}` | {display(base)} | {display(current)} | {pct} |")
    return "\n".join(lines)


def profile_markdown(diffs: list[tuple[str, str, Any, Any, str]]) -> str:
    lines = ["### Top Column Diffs", "", "| Model | Column | Metric | Base | Current |", "|---|---|---|---:|---:|"]
    ordered = sorted(diffs, key=lambda item: (item[0], item[1], item[4]))
    if not ordered:
        lines.append("| No changed avg/min/max/null-count metrics were found | — | — | — | — |")
    for model, col, base, current, metric in ordered[:20]:
        lines.append(f"| `{model}` | `{col}` | `{metric}` | {display(base)} | {display(current)} |")
    if len(ordered) > 20:
        lines.append(f"| … | … | {len(ordered) - 20} more changed metrics omitted | … | … |")
    return "\n".join(lines)


def tests_markdown(run_results: Any, exit_code: str) -> str:
    results = (run_results or {}).get("results", []) if isinstance(run_results, dict) else []
    tests = [r for r in results if str(r.get("unique_id", "")).startswith("test.")]
    passed = sum(str(r.get("status", "")).lower() in {"pass", "passed"} for r in tests)
    failed = sum(str(r.get("status", "")).lower() in {"fail", "error", "runtime error"} for r in tests)
    skipped = sum(str(r.get("status", "")).lower() in {"skipped", "warn"} for r in tests)
    overall = ("NOT RUN" if not tests else "PASS") if exit_code == "0" and failed == 0 else "FAIL"
    lines = [
        "## dbt Test Results",
        "",
        f"**Status: {overall}** — {passed} passed, {failed} failed, {skipped} skipped/warn.",
        "",
    ]
    if tests:
        lines += ["| Test | Status | Failures |", "|---|---|---:|"]
        for result in tests:
            name = result.get("unique_id", "test").rsplit(".", 1)[-1]
            status = str(result.get("status", "unknown")).upper()
            lines.append(f"| `{name}` | {status} | {display(result.get('failures'))} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--base-manifest", type=Path, required=True)
    parser.add_argument("--current-manifest", type=Path, required=True)
    parser.add_argument("--run-results", type=Path, required=True)
    parser.add_argument("--dbt-test-exit-code", default="1")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    summary = args.summary.read_text(encoding="utf-8") if args.summary.exists() else "Recce summary was unavailable."
    base_manifest = load_json(args.base_manifest, {})
    current_manifest = load_json(args.current_manifest, {})
    names = {
        uid: node.get("name", uid.rsplit(".", 1)[-1])
        for manifest in (base_manifest, current_manifest)
        for uid, node in manifest.get("nodes", {}).items()
        if node.get("resource_type") == "model"
    }
    changed, impacted, edges = manifest_diff(base_manifest, current_manifest)
    counts, profiles = state_metrics(load_json(args.state, {}), names)

    parts = [
        summary.rstrip(),
        lineage_markdown(changed, impacted, edges),
        "## Detailed Row Count & Profile Diff",
        "",
        "<details><summary>📊 Chi tiết số liệu Row Count & Profile Diff</summary>",
        "",
        row_count_markdown(counts),
        "",
        profile_markdown(profiles),
        "",
        "</details>",
        "",
        tests_markdown(load_json(args.run_results, {}), args.dbt_test_exit_code),
    ]
    args.output.write_text("\n\n".join(parts).rstrip() + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
