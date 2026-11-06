import os
import json
import sys
import urllib.request

url = os.environ["AGHUB_HUB_URL"]
token = os.environ["AGHUB_TOKEN"]
project = os.environ.get("AGHUB_PROJECT", "swarm-real")

req = urllib.request.Request(
    f"{url}/api/board?project={project}",
    headers={"Authorization": f"Bearer {token}"},
)
try:
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.load(response)
except Exception as exc:
    print("board error:", exc)
    raise SystemExit(1)

snapshot = data.get("snapshot", data)
print("leases:", [(l.get("task"), l.get("agent")) for l in data.get("leases", [])])
print("agents:", sorted(a.get("id") for a in snapshot.get("agents", [])))
print("queue:")
for entry in snapshot.get("queue", []):
    print("  ", entry.get("task"), entry.get("agent"), entry.get("status"), entry.get("reason"))
print("tasks:")
for task in snapshot.get("tasks", []):
    print(f"  {task.get('id'):<6} {task.get('status'):<10} {str(task.get('owner')):<10} {task.get('title')}")
