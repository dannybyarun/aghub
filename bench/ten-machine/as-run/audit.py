import os
import json
import os
import urllib.request
from collections import Counter

url = os.environ["AGHUB_HUB_URL"]
token = os.environ["AGHUB_TOKEN"]
project = os.environ.get("AGHUB_PROJECT", "swarm-real")

req = urllib.request.Request(
    f"{url}/api/audit/export?format=jsonl&project={project}&limit=5000",
    headers={"Authorization": f"Bearer {token}"},
)
with urllib.request.urlopen(req, timeout=60) as response:
    lines = response.read().decode("utf-8").splitlines()

events = [json.loads(line) for line in lines if line.strip()]
print("audit events:", len(events))
kinds = Counter(event.get("kind", "?") for event in events)
for kind, count in sorted(kinds.items()):
    print(f"  {kind:<24} {count}")

actors = Counter(event.get("actor") or event.get("agent") or "?" for event in events)
print("actors:", dict(sorted(actors.items())))

rejections = [
    event
    for event in events
    if "reject" in (event.get("kind") or "") or event.get("result") == "rejected"
]
print("rejections:", len(rejections))
for event in rejections[:20]:
    print("  ", json.dumps(event)[:240])

os.makedirs("audit", exist_ok=True)
with open("audit/audit.jsonl", "w", encoding="utf-8") as handle:
    for event in events:
        handle.write(json.dumps(event) + "\n")
