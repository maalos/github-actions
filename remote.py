#!/usr/bin/env python3
"""remote.py <zip_rel> <start> <end> <out.ctl>: members [start:end) of a remote zip -> one .ctl"""
import multiprocessing as mp, os, sys, time
import numpy as np
from common import BASE, members, open_zip
import process as P

WORKERS = int(os.environ.get("DL_WORKERS", "4"))
_url = _z = None

def _init(url):
    global _url, _z
    _url, _z = url, None

def _work(name):
    global _z
    t0, err = time.time(), None
    for attempt in range(8):
        try:
            if _z is None:
                _z = open_zip(_url)          # one session per process
            data = _z.read(name)             # downloads only this member's bytes
            break
        except Exception as e:
            err, _z = e, None
            print(f"retry {attempt + 1} {name}: {e!r}", flush=True)
            time.sleep(min(10 * (attempt + 1), 60))
    else:
        return name, None, 0, f"download failed: {err!r}", 0, 0
    t1 = time.time()
    try:
        hs, ps = P.find_trees(data)
        del data
        cx, cy, h = P.centroids(hs, ps)
        return name, P.quantize(cx, cy, h), len(hs) - len(cx), None, t1 - t0, time.time() - t1
    except Exception as e:
        return name, None, 0, repr(e), t1 - t0, 0

def main(rel, start, end, out):
    url = f"{BASE}/{rel}"
    names = [n for n, _ in members(url)][start:end]
    parts, dropped, failed = [], 0, []
    with mp.Pool(WORKERS, initializer=_init, initargs=(url,)) as pool:
        for i, (name, q, d, err, td, tp) in enumerate(pool.imap_unordered(_work, names), 1):
            if err:
                failed.append(name)
                print(f"FAIL {name}: {err}", flush=True)
                continue
            parts.append(q)
            dropped += d
            print(f"[{i}/{len(names)}] {name} {len(q[0])} trees  dl {td:.0f}s  parse {tp:.0f}s", flush=True)
    if failed:
        sys.exit(f"{len(failed)} members failed, no output written")
    if parts:
        qx, qy, qh = (np.concatenate([p[i] for p in parts]) for i in range(3))
    else:
        qx = qy = np.zeros(0, np.int32); qh = np.zeros(0, np.uint16)
    blob = P.encode(qx, qy, qh)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out + ".part", "wb") as f:
        f.write(blob)
    os.replace(out + ".part", out)
    print(f"{out}: {len(qx)} trees, {len(blob)} bytes, {dropped} unusable trees", flush=True)

if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4])