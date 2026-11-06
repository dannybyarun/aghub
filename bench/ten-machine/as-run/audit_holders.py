import json, re
from collections import Counter
events=[json.loads(l) for l in open("audit/audit.jsonl",encoding="utf-8") if l.strip()]
locks=[e for e in events if e["type"]=="board.lock"]
holders=Counter(re.sub(r":\d+$","",e["payload"]["holder"]) for e in locks)
print("lock holders:")
for h,c in sorted(holders.items()): print(f"  {h}: {c}")
print("fencing min/max:", min(e["payload"]["fencing"] for e in locks), max(e["payload"]["fencing"] for e in locks))
leases=[e for e in events if e["type"]=="lease.claim"]
print("lease claims by task/agent:")
for e in leases: print("  ", e["payload"]["task"], e["payload"]["agent"], "fencing", e["payload"]["fencing"])
