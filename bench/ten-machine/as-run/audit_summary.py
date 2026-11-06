import json
from collections import Counter

events = [json.loads(line) for line in open("audit/audit.jsonl", encoding="utf-8") if line.strip()]
print("audit events:", len(events))
counts = Counter(e["type"] for e in events)
for k, v in sorted(counts.items()):
    print(f"  {k:<24} {v}")
print("time span:", events[0]["at"], "->", events[-1]["at"])

claims = [e for e in events if e["type"] in ("claim", "lease.claim", "task.claim")]
print("claim-like events:", len(claims))
conflicts = [e for e in events if "conflict" in e["type"] or e["payload"].get("conflict")]
print("conflicts:", len(conflicts))
for e in conflicts[:10]:
    print("  ", json.dumps(e)[:300])

rejects = [e for e in events if "reject" in e["type"] or "denied" in e["type"]]
print("rejects:", len(rejects))
for e in rejects[:10]:
    print("  ", json.dumps(e)[:300])

holds = [e for e in events if e["type"] == "board.lock"]
print("board locks:", len(holds), "max fencing:", max((e["payload"].get("fencing") or 0) for e in holds))
