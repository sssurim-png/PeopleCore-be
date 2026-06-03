"""
nano-graphrag 가 만든 NetworkX 그래프를 PyVis 로 인터랙티브 HTML 시각화.

실행:
  docker exec analysis-service python /app/scripts/visualize_graph.py
출력:
  /app/graph_viz.html
"""
import os
from pyvis.network import Network
import networkx as nx

GRAPH_FILE = "/app/graph_data/graph_chunk_entity_relation.graphml"
OUTPUT_HTML = "/app/graph_viz.html"

if not os.path.exists(GRAPH_FILE):
    raise SystemExit(f"❌ 그래프 파일 없음: {GRAPH_FILE}")

G = nx.read_graphml(GRAPH_FILE)
print(f"로드: 노드 {G.number_of_nodes()}, 엣지 {G.number_of_edges()}")

net = Network(
    height="900px",
    width="100%",
    bgcolor="#0f172a",
    font_color="white",
    notebook=False,
    cdn_resources="remote",
)
net.barnes_hut(
    gravity=-3000,
    central_gravity=0.3,
    spring_length=120,
    spring_strength=0.005,
    damping=0.4,
)

# 엔티티 타입별 색상
type_colors = {
    "organization": "#f59e0b",
    "person":       "#ec4899",
    "geo":          "#10b981",
    "event":        "#8b5cf6",
    "concept":      "#3b82f6",
}
DEFAULT_COLOR = "#6b7280"


def _clean(s):
    if s is None:
        return ""
    return str(s).strip().strip('"').strip()


for node_id in G.nodes():
    attrs = G.nodes[node_id]
    entity_type = _clean(attrs.get("entity_type")).lower()
    color = type_colors.get(entity_type, DEFAULT_COLOR)
    display = _clean(node_id)[:30]
    desc = _clean(attrs.get("description"))[:200]
    title = f"<b>{display}</b><br/>type: {entity_type or 'unknown'}<br/>{desc}"
    net.add_node(node_id, label=display, title=title, color=color, size=15)

for src, dst, attrs in G.edges(data=True):
    desc = _clean(attrs.get("description"))[:120]
    try:
        weight = float(attrs.get("weight", 1.0))
    except (TypeError, ValueError):
        weight = 1.0
    net.add_edge(src, dst, title=desc, value=weight)

net.show_buttons(filter_=["physics"])

net.save_graph(OUTPUT_HTML)
size_kb = os.path.getsize(OUTPUT_HTML) / 1024
print(f"✅ 저장: {OUTPUT_HTML}  ({size_kb:.0f} KB)")
