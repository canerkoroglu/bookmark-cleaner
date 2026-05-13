from __future__ import annotations

import json
from pathlib import Path

p = Path("output/exported.json")
print(f"exists={p.exists()}")
print(f"size={p.stat().st_size if p.exists() else 0}")
if not p.exists() or p.stat().st_size == 0:
    raise SystemExit(0)

data = json.loads(p.read_text(encoding="utf-8"))
nodes: list[dict] = []


def walk(n):
    if isinstance(n, dict):
        if n.get("type") == "text/x-moz-place" and isinstance(n.get("uri"), str):
            nodes.append(n)
        for v in n.values():
            if isinstance(v, (dict, list)):
                walk(v)
    elif isinstance(n, list):
        for x in n:
            walk(x)


walk(data)

with_keyword = [n for n in nodes if isinstance(n.get("keyword"), str) and n.get("keyword").strip()]
with_description = [n for n in nodes if isinstance(n.get("description"), str) and n.get("description").strip()]
with_404 = [n for n in nodes if isinstance(n.get("keyword"), str) and "404_not_found" in n.get("keyword")]

print(f"total_bookmark_nodes={len(nodes)}")
print(f"nodes_with_keyword={len(with_keyword)}")
print(f"nodes_with_description={len(with_description)}")
print(f"nodes_with_404_keyword={len(with_404)}")

print("\nSAMPLE_KEYWORD_NODES:")
for n in with_keyword[:3]:
    print(f"- {n.get('uri')}")
    print(f"  keyword={n.get('keyword')}")

print("\nSAMPLE_DESCRIPTION_NODES:")
for n in with_description[:3]:
    desc = str(n.get("description") or "")
    print(f"- {n.get('uri')}")
    print(f"  description={desc[:120]}")

print("\nSAMPLE_404_NODES:")
for n in with_404[:3]:
    print(f"- {n.get('uri')}")
    print(f"  keyword={n.get('keyword')}")

