import json, sys
from common import BASE, members

group = sys.argv[1]
target = float(sys.argv[2])
limit = int(sys.argv[3]) if len(sys.argv) > 3 else 0

include = []
for line in open("files.txt"):
    rel = line.strip()
    if not rel.startswith(group + "/"):
        continue
    m = members(f"{BASE}/{rel}")
    start = acc = 0
    for k, (_, size) in enumerate(m):
        acc += size
        if acc >= target or k == len(m) - 1:
            include.append({"id": f"{rel.split('/')[-1][:-4]}_{start}",
                            "zip": rel, "start": start, "end": k + 1})
            start, acc = k + 1, 0

if limit:
    include = include[:limit]
assert len(include) <= 256, f"{len(include)} chunks exceeds the matrix limit; raise target"
print(json.dumps({"include": include}))