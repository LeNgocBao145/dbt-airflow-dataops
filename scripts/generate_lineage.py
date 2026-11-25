import json
import re
from pathlib import Path

try:
    import sqlglot
    from sqlglot.lineage import lineage

    HAS_SQLGLOT = True
except ImportError:
    HAS_SQLGLOT = False

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DBT_DIR = PROJECT_ROOT / "dbt"
TARGET_DIR = DBT_DIR / "target"
OUTPUT_HTML = PROJECT_ROOT / "pages" / "lineage" / "index.html"


def clean_sql(sql_text: str) -> str:
    """Loại bỏ dbt jinja tags cơ bản để sqlglot parse được"""
    # Thay thế config(...) thành rỗng
    cleaned = re.sub(r"\{\{\s*config\([^)]*\)\s*\}\}", "", sql_text, flags=re.DOTALL)
    # Thay thế {{ ref('model_name') }} thành model_name
    cleaned = re.sub(r"\{\{\s*ref\(['\"]([a-zA-Z0-9_]+)['\"]\)\s*\}\}", r"\1", cleaned)
    # Thay thế {{ source('source_name', 'table_name') }} thành table_name
    cleaned = re.sub(r"\{\{\s*source\(['\"][^'\"]+['\"],\s*['\"]([a-zA-Z0-9_]+)['\"]\)\s*\}\}", r"\1", cleaned)
    return cleaned


def extract_select_columns_fallback(sql_text: str):
    """Fallback parser nếu không có sqlglot: tìm các cột output và các bảng nguồn"""
    cleaned = clean_sql(sql_text)
    # Tìm bảng nguồn từ mệnh đề FROM và JOIN
    sources = set(re.findall(r"(?:from|join)\s+([a-zA-Z0-9_]+)", cleaned, re.IGNORECASE))
    return list(sources)


def build_lineage_graph():
    models_dir = DBT_DIR / "models"
    sql_files = list(models_dir.glob("**/*.sql"))

    nodes = []
    edges = []
    seen_nodes = set()
    seen_edges = set()

    # Phân loại tầng cho màu sắc
    def get_layer(name: str):
        if name.startswith("brnz_") or "bronze" in name:
            return "bronze"
        elif name.startswith("slvr_") or "silver" in name:
            return "silver"
        elif name.startswith("gld_") or "gold" in name:
            return "gold"
        return "other"

    print(f"Parsing {len(sql_files)} dbt models...")

    for file_path in sql_files:
        model_name = file_path.stem
        layer = get_layer(model_name)

        # Node cấp Model/Table
        table_node_id = f"table_{model_name}"
        if table_node_id not in seen_nodes:
            nodes.append(
                {"data": {"id": table_node_id, "label": f"{model_name} ({layer})", "type": "table", "layer": layer}}
            )
            seen_nodes.add(table_node_id)

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw_sql = f.read()
            cleaned_sql = clean_sql(raw_sql)

            # 1. Tìm các bảng nguồn (Table-level edges)
            source_tables = re.findall(r"(?:from|join)\s+([a-zA-Z0-9_]+)", cleaned_sql, re.IGNORECASE)
            for src in set(source_tables):
                src_node_id = f"table_{src}"
                if src_node_id not in seen_nodes and (
                    src.startswith("brnz_") or src.startswith("slvr_") or src.startswith("raw_")
                ):
                    src_layer = get_layer(src)
                    nodes.append(
                        {
                            "data": {
                                "id": src_node_id,
                                "label": f"{src} ({src_layer})",
                                "type": "table",
                                "layer": src_layer,
                            }
                        }
                    )
                    seen_nodes.add(src_node_id)

                edge_id = f"e_{src_node_id}_{table_node_id}"
                if edge_id not in seen_edges and src_node_id in seen_nodes:
                    edges.append(
                        {"data": {"id": edge_id, "source": src_node_id, "target": table_node_id, "type": "table_edge"}}
                    )
                    seen_edges.add(edge_id)

            # 2. Phân tích Column-level Lineage (CLL)
            if HAS_SQLGLOT:
                try:
                    parsed = sqlglot.parse_one(cleaned_sql, read="tsql")
                    # Lấy danh sách alias / output columns
                    for expression in parsed.expressions:
                        col_name = expression.alias_or_name
                        if not col_name or col_name == "*":
                            continue

                        col_node_id = f"col_{model_name}_{col_name}"
                        if col_node_id not in seen_nodes:
                            nodes.append(
                                {
                                    "data": {
                                        "id": col_node_id,
                                        "label": col_name,
                                        "parent": table_node_id,
                                        "type": "column",
                                        "layer": layer,
                                    }
                                }
                            )
                            seen_nodes.add(col_node_id)

                        # Trace nguồn gốc cột bằng sqlglot.lineage
                        try:
                            col_lineage = lineage(col_name, cleaned_sql, dialect="tsql")
                            for source_node in col_lineage.walk():
                                if source_node.name != col_name and source_node.source_name:
                                    src_col_id = f"col_{source_node.source_name}_{source_node.name}"
                                    if src_col_id in seen_nodes:
                                        col_edge_id = f"e_{src_col_id}_{col_node_id}"
                                        if col_edge_id not in seen_edges:
                                            edges.append(
                                                {
                                                    "data": {
                                                        "id": col_edge_id,
                                                        "source": src_col_id,
                                                        "target": col_node_id,
                                                        "type": "column_edge",
                                                    }
                                                }
                                            )
                                            seen_edges.add(col_edge_id)
                        except Exception as err:
                            print(f"[WARN] Failed to trace column lineage for {model_name}.{col_name}: {err}")
                except Exception as err:
                    print(f"[WARN] Failed to parse SQL with sqlglot for model {model_name}: {err}")
            else:
                # Fallback: Trích xuất các cột cơ bản bằng regex
                col_matches = re.findall(r"(?:as\s+)?([a-zA-Z0-9_]+)\s*[,|\n]", cleaned_sql, re.IGNORECASE)
                for col in set(col_matches[:10]):  # lấy top 10 cột đặc trưng
                    if col.lower() in ["select", "from", "where", "group", "by", "order", "join", "on", "as", "and"]:
                        continue
                    col_node_id = f"col_{model_name}_{col}"
                    if col_node_id not in seen_nodes:
                        nodes.append(
                            {
                                "data": {
                                    "id": col_node_id,
                                    "label": col,
                                    "parent": table_node_id,
                                    "type": "column",
                                    "layer": layer,
                                }
                            }
                        )
                        seen_nodes.add(col_node_id)
        except Exception as e:
            print(f"Error reading {file_path}: {e}")

    return {"nodes": nodes, "edges": edges}


def generate_html(graph_data):
    OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    elements_json = json.dumps(graph_data["nodes"] + graph_data["edges"], indent=2)

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Interactive Column-Level Lineage</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.28.1/cytoscape.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/dagre@0.8.5/dist/dagre.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/cytoscape-dagre@2.5.0/cytoscape-dagre.min.js"></script>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            margin: 0;
            padding: 0;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            height: 100vh;
            background: #f8fafc;
        }}
        header {{
            background: #1e293b;
            color: white;
            padding: 12px 24px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        header h1 {{
            margin: 0;
            font-size: 1.15rem;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .controls {{
            display: flex;
            gap: 10px;
        }}
        button {{
            background: #3b82f6;
            color: white;
            border: none;
            padding: 6px 14px;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 500;
            transition: background 0.2s;
        }}
        button:hover {{ background: #2563eb; }}
        #cy {{
            flex: 1;
            width: 100%;
            height: 100%;
        }}
        .legend {{
            position: absolute;
            bottom: 20px;
            left: 20px;
            background: white;
            padding: 12px 18px;
            border-radius: 8px;
            box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);
            font-size: 0.85rem;
            z-index: 10;
            line-height: 1.6;
        }}
        .legend-item {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .dot {{
            width: 12px;
            height: 12px;
            border-radius: 3px;
        }}
    </style>
</head>
<body>
    <header>
        <h1>🔍 Interactive Column-Level Lineage <span>(DataOps Project)</span></h1>
        <div class="controls">
            <button onclick="cy.fit(null, 40)">Fit View</button>
            <button onclick="resetHighlight()">Reset Highlight</button>
            <a href="../dbt-docs/"
               style="color: #93c5fd; text-decoration: none; align-self: center; font-size: 0.9rem; margin-left: 10px;">
                ➔ Go to DBT Docs
            </a>
        </div>
    </header>

    <div id="cy"></div>

    <div class="legend">
        <strong>Pipeline Layers:</strong>
        <div class="legend-item"><span class="dot" style="background: #cd7f32;"></span> Bronze (Raw Ingestion)</div>
        <div class="legend-item"><span class="dot" style="background: #94a3b8;"></span> Silver (Cleaned / Joined)</div>
        <div class="legend-item"><span class="dot" style="background: #eab308;"></span> Gold (Marts / Metrics)</div>
        <div style="margin-top: 6px; font-size: 0.75rem; color: #64748b;">
            Click node to highlight upstream/downstream
        </div>
    </div>

    <script>
        const elements = {elements_json};

        const cy = cytoscape({{
            container: document.getElementById('cy'),
            elements: elements,
            boxSelectionEnabled: false,
            autounselectify: false,
            style: [
                {{
                    selector: 'node[type = "table"]',
                    style: {{
                        'background-color': '#f1f5f9',
                        'label': 'data(label)',
                        'text-valign': 'top',
                        'text-halign': 'center',
                        'font-weight': 'bold',
                        'font-size': '13px',
                        'color': '#0f172a',
                        'border-width': 2,
                        'border-color': '#cbd5e1',
                        'padding': '12px'
                    }}
                }},
                {{
                    selector: 'node[type = "column"]',
                    style: {{
                        'background-color': '#ffffff',
                        'label': 'data(label)',
                        'font-size': '11px',
                        'color': '#334155',
                        'border-width': 1,
                        'border-color': '#94a3b8',
                        'width': 'label',
                        'padding': '6px',
                        'shape': 'roundrectangle'
                    }}
                }},
                {{
                    selector: 'node[layer = "bronze"][type = "table"]',
                    style: {{ 'border-color': '#cd7f32', 'background-color': '#fdf8f4' }}
                }},
                {{
                    selector: 'node[layer = "silver"][type = "table"]',
                    style: {{ 'border-color': '#64748b', 'background-color': '#f8fafc' }}
                }},
                {{
                    selector: 'node[layer = "gold"][type = "table"]',
                    style: {{ 'border-color': '#eab308', 'background-color': '#fefce8' }}
                }},
                {{
                    selector: 'edge',
                    style: {{
                        'width': 2,
                        'line-color': '#cbd5e1',
                        'target-arrow-color': '#cbd5e1',
                        'target-arrow-shape': 'triangle',
                        'curve-style': 'bezier'
                    }}
                }},
                {{
                    selector: '.highlighted',
                    style: {{
                        'background-color': '#3b82f6',
                        'line-color': '#3b82f6',
                        'target-arrow-color': '#3b82f6',
                        'border-color': '#1d4ed8',
                        'border-width': 3,
                        'color': '#1e3a8a',
                        'font-weight': 'bold',
                        'z-index': 999
                    }}
                }},
                {{
                    selector: '.faded',
                    style: {{
                        'opacity': 0.15
                    }}
                }}
            ],
            layout: {{
                name: 'dagre',
                rankDir: 'LR',
                nodeSep: 30,
                rankSep: 80,
                padding: 40
            }}
        }});

        function resetHighlight() {{
            cy.elements().removeClass('highlighted faded');
        }}

        cy.on('tap', 'node', function(evt) {{
            const node = evt.target;
            resetHighlight();
            const connected = node.outgoers().union(node.incomers()).union(node);
            cy.elements().difference(connected).addClass('faded');
            connected.addClass('highlighted');
        }});

        cy.on('tap', function(evt) {{
            if (evt.target === cy) {{
                resetHighlight();
            }}
        }});
    </script>
</body>
</html>
"""
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"✅ Generated interactive lineage HTML at: {OUTPUT_HTML}")


if __name__ == "__main__":
    graph = build_lineage_graph()
    generate_html(graph)
