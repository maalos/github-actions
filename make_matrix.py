import json, sys
import urllib.request, urllib.error
from common import BASE, members, BadArchive

group = sys.argv[1]
target = float(sys.argv[2])
limit = int(sys.argv[3]) if len(sys.argv) > 3 else 0


def url_exists(url: str, timeout: float = 15) -> bool:
    """Return False on 404/410; True otherwise (including transient errors,
    so a flaky network doesn't silently drop files)."""
    for method, headers in (("HEAD", {}), ("GET", {"Range": "bytes=0-0"})):
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout):
                return True
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return False
            if method == "HEAD" and e.code in (403, 405, 501):
                continue  # server may not allow HEAD; retry with ranged GET
            print(f"warn: {url} -> HTTP {e.code}, treating as available", file=sys.stderr)
            return True
        except Exception as e:
            print(f"warn: {url} -> {e!r}, treating as available", file=sys.stderr)
            return True
    return True


include = []
corrupt = []
for line in open("files.txt"):
    rel = line.strip()
    if not rel.startswith(group + "/"):
        continue
    url = f"{BASE}/{rel}"
    if not url_exists(url):
        print(f"skip (404): {url}", file=sys.stderr)
        continue
    try:
        m = members(url)
    except BadArchive as e:
        print(f"::warning::skip (corrupt zip): {e}", file=sys.stderr)
        corrupt.append(rel)
        continue
    start = acc = 0
    for k, (_, size) in enumerate(m):
        acc += size
        if acc >= target or k == len(m) - 1:
            include.append({"id": f"{rel.split('/')[-1][:-4]}_{start}",
                            "zip": rel, "start": start, "end": k + 1})
            start, acc = k + 1, 0

if corrupt:
    print(f"{len(corrupt)} corrupt archive(s) skipped: {corrupt}", file=sys.stderr)

if limit:
    include = include[:limit]
assert len(include) <= 256, f"{len(include)} chunks exceeds the matrix limit; raise target"
print(json.dumps({"include": include}))