"""Patch Recce profile aggregates to SQL Server-compatible T-SQL."""

from importlib import import_module
from pathlib import Path


profile_module = import_module("recce.tasks.profile")
profile_path = Path(profile_module.__file__)
source = profile_path.read_text(encoding="utf-8")
patches = [
    (
        "~ 'else count(distinct ' ~ adapter.quote(column_name) ~ ') = count(*) end'",
        "~ 'else case when count(distinct ' ~ adapter.quote(column_name) ~ ') "
        "= count(*) then cast(1 as bit) else cast(0 as bit) end end'",
        "profile uniqueness",
    ),
    (
        "{%- elif db_type == 'sqlserver' -%}\n"
        "        {%- set agg_median = 'percentile_cont(' ~ adapter.quote(column_name) ~ ', 0.5) over ()' -%}",
        "{%- elif db_type == 'sqlserver' -%}\n"
        "        {%- set agg_median =\n"
        "            '(select max(profile_median) from (select percentile_cont(0.5) within group (order by '\n"
        "            ~ adapter.quote(column_name) ~ ') over () as profile_median from ' ~ relation\n"
        "            ~ ') as profile_median_values)'\n"
        "        -%}",
        "profile median",
    ),
]

changed = False
for old, new, label in patches:
    if old in source:
        source = source.replace(old, new)
        changed = True
    elif new not in source:
        raise SystemExit(f"Unsupported Recce profile template: {label} expression was not found")

if changed:
    profile_path.write_text(source, encoding="utf-8")
    print(f"Applied SQL Server profile_diff compatibility patch to {profile_path}")
else:
    print("Recce profile_diff already has SQL Server-compatible aggregates")
