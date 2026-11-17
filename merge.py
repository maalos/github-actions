#!/usr/bin/env python3
"""merge.py <chunks_dir> <out_dir>: chunk .ctl files -> one .ctl per zip (env MATRIX = matrix json)"""
import json, lzma, os, sys
import numpy as np
import process as P

def read_q(path):
    """chunk .ctl -> integer qx, qy, qh (no rescaling, so the merge is lossless)."""
    raw = open(path, "rb").read()
    if raw[:4] != P.MAGIC:
        raise ValueError(f"{path}: not a CTL1 file")
    v = P.varint_dec(np.frombuffer(lzma.decompress(raw[4:]), dtype=np.uint8))
    n = int(v[0])
    assert int(v[1]) == P.XY_STEP_MM and int(v[2]) == P.H_STEP_MM, "precision mismatch"
    if n == 0:
        e = np.zeros(0, np.int64)
        return e, e, e
    x0, y0 = int(P.zz_dec(v[3:4])[0]), int(P.zz_dec(v[4:5])[0])
    code = np.cumsum(v[5:5 + n], dtype=np.uint64)
    qh = np.cumsum(P.zz_dec(v[5 + n:5 + 2 * n]))
    return (P.compact(code).astype(np.int64) + x0,
            P.compact(code >> P.U(1)).astype(np.int64) + y0, qh)

src, dst = sys.argv[1], sys.argv[2]
os.makedirs(dst, exist_ok=True)
zips = {}
for c in json.loads(os.environ["MATRIX"])["include"]:
    zips.setdefault(c["zip"], []).append(c["id"])

bad = 0
for rel, ids in zips.items():
    paths = [os.path.join(src, i + ".ctl") for i in ids]
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        print(f"MISSING {rel}: {len(missing)}/{len(paths)} chunks", file=sys.stderr)
        bad += 1
        continue
    parts = [read_q(p) for p in paths]
    qx, qy, qh = (np.concatenate([p[i] for p in parts]) for i in range(3))
    del parts
    blob = P.encode(qx, qy, qh)
    out = os.path.join(dst, os.path.basename(rel)[:-4] + ".ctl")
    open(out, "wb").write(blob)
    print(f"{out}: {len(qx)} trees, {len(blob)} bytes")
sys.exit(1 if bad else 0)