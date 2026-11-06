import json
import re

LOG_FILES = ["runA-full.log", "runB-full.log"]
pat = re.compile(r"^(?P<job>.+?)\t(?P<step>.*?)\t(?P<ts>\S+Z) EVIDENCE\|(?P<agent>[^|]+)\|(?P<key>[^|]+)\|(?P<value>.*)$")

evidence = {}
for path in LOG_FILES:
    with open(path, encoding="utf-8-sig") as handle:
        for line in handle:
            match = pat.match(line.rstrip("\n"))
            if not match:
                continue
            agent = match.group("agent")
            evidence.setdefault(agent, {})[match.group("key")] = match.group("value")

order = [f"a{i}" for i in range(1, 6)] + [f"b{i}" for i in range(1, 6)]
print("| Machine | Account repo | Runner id | Boot id | Lane | Race |")
print("|---|---|---|---|---|---|")
for agent in order:
    row = evidence.get(agent, {})
    account = row.get("account_repo", "?")
    runner = row.get("runner", "?")
    boot = row.get("boot_id", "?")[:8]
    lane = {
        "a1": "T-001", "a2": "T-002", "a3": "T-003", "a4": "T-004", "a5": "T-005",
        "b1": "T-006", "b2": "T-007", "b3": "T-008", "b4": "T-009", "b5": "T-010",
    }[agent]
    race = row.get("race", "?")
    print(f"| {agent} | {account} | {runner} | {boot} | {lane} | {race} |")

print()
print("runner ids:", len({evidence[a].get("runner") for a in order}))
print("boot ids:", len({evidence[a].get("boot_id") for a in order}))
print("machine ids:", len({evidence[a].get("machine_id") for a in order}))
print("hostnames:", {evidence[a].get("machine") for a in order})

# job timing from the run JSON
for label, path in (("A", "runA.json"), ("B", "runB.json")):
    with open(path, encoding="utf-16") as handle:
        data = json.load(handle)
    print(f"run {label} {data['status']} {data['conclusion']}")
    for job in data["jobs"]:
        print(f"  {job['name']:<48} {job['startedAt']} -> {job['completedAt']}")
