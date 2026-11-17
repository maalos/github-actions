#!/usr/bin/env python3
"""LOD1 trees zip (CityGML) -> one .ctl (Compressed Tree List) file.

    process.py <in.zip> <out.ctl>      build
    process.py --decode <file.ctl>     print x,y,height as CSV (for checking)

Keeps only x, y (EPSG:2180, tree centre) and height, quantised. Needs numpy.

.ctl layout
    4 bytes   magic "CTL1"
    rest      one xz stream (LZMA2, CRC32) whose decompressed content is a
              sequence of LEB128 varints:
                n, xy_step_mm, h_step_mm, zigzag(x0), zigzag(y0)   header
                n x  morton_delta     \\ trees sorted by Morton code of the
                n x  zigzag(h_delta)  /  (qx-x0, qy-y0); two separate streams
    tree i:  code_i = code_{i-1} + morton_delta_i  -> de-interleave -> (qx-x0, qy-y0)
             qh_i   = qh_{i-1} + unzigzag(h_delta_i)           (qh_-1 = 0)
             x = (x0+qx) * xy_step_mm/1000,  y likewise,  height = qh * h_step_mm/1000
"""
import lzma, math, multiprocessing as mp, os, re, sys, zipfile
import numpy as np

# ---- precision: the main size knob. Each doubling of XY_STEP_MM saves ~1 bit/tree.
XY_STEP_MM = 250      # position grid, 0.25 m
H_STEP_MM = 500       # height grid, 0.5 m
MAGIC = b"CTL1"

OBJ = b"<veg:SolitaryVegetationObject"
# height, then the first posList after it (= the footprint ring of lod1Geometry)
TREE_RE = re.compile(rb"<veg:height[^>]*>([^<]*)</veg:height>.*?"
                     rb"<gml:posList>([^<]*)</gml:posList>", re.S)

U = np.uint64


# ----------------------------------------------------------------- bit tricks
def spread(v):                       # 32 bits -> every other bit of 64
    v = v & U(0xFFFFFFFF)
    v = (v | (v << U(16))) & U(0x0000FFFF0000FFFF)
    v = (v | (v << U(8))) & U(0x00FF00FF00FF00FF)
    v = (v | (v << U(4))) & U(0x0F0F0F0F0F0F0F0F)
    v = (v | (v << U(2))) & U(0x3333333333333333)
    v = (v | (v << U(1))) & U(0x5555555555555555)
    return v


def compact(v):                      # inverse of spread
    v = v & U(0x5555555555555555)
    v = (v | (v >> U(1))) & U(0x3333333333333333)
    v = (v | (v >> U(2))) & U(0x0F0F0F0F0F0F0F0F)
    v = (v | (v >> U(4))) & U(0x00FF00FF00FF00FF)
    v = (v | (v >> U(8))) & U(0x0000FFFF0000FFFF)
    v = (v | (v >> U(16))) & U(0x00000000FFFFFFFF)
    return v


def varint_enc(v):
    """uint64 array -> LEB128 bytes (uint8 array), vectorised."""
    v = np.asarray(v, dtype=np.uint64)
    nb = np.ones(len(v), dtype=np.int64)
    for k in range(1, 10):
        nb += v >= (U(1) << U(7 * k))
    off = np.cumsum(nb) - nb
    out = np.empty(int(nb.sum()), dtype=np.uint8)
    for k in range(int(nb.max()) if len(v) else 0):
        m = nb > k
        b = ((v[m] >> U(7 * k)) & U(0x7F)).astype(np.uint8)
        b |= (nb[m] > k + 1).astype(np.uint8) << 7
        out[off[m] + k] = b
    return out


def varint_dec(buf):
    """LEB128 bytes (uint8 array) -> uint64 array, vectorised."""
    if len(buf) == 0:
        return np.zeros(0, dtype=np.uint64)
    if buf[-1] & 0x80:
        raise ValueError("truncated varint stream")
    last = (buf & 0x80) == 0
    ends = np.flatnonzero(last)
    starts = np.concatenate(([0], ends[:-1] + 1))
    grp = np.cumsum(last) - last
    pos = (np.arange(len(buf)) - starts[grp]).astype(np.uint64)
    return np.add.reduceat((buf & 0x7F).astype(np.uint64) << (pos * U(7)), starts)


def zz_enc(a):                       # int64 -> uint64, small |a| -> small code
    a = np.asarray(a, dtype=np.int64)
    return ((a << 1) ^ (a >> 63)).astype(np.uint64)


def zz_dec(z):
    z = np.asarray(z, dtype=np.uint64)
    return (z >> U(1)).astype(np.int64) ^ -((z & U(1)).astype(np.int64))


# ----------------------------------------------------------- GML -> centroids
def find_trees(data):
    """bytes of one .gml -> (list of height strings, list of footprint posLists)."""
    n = data.count(OBJ)
    m = TREE_RE.findall(data)                    # fast: one pass over the file
    if len(m) != n:                              # some tree lacks a part: go object by object
        m = []
        for chunk in data.split(OBJ)[1:]:
            r = TREE_RE.search(chunk)
            if r:
                m.append(r.groups())
    return [a for a, _ in m], [b for _, b in m]


def centroids_fast(hs, ps):
    h = np.array(hs).astype(np.float64)
    k = np.fromiter((p.count(b" ") + 1 for p in ps), np.int64, len(ps))
    ok = (k % 3 == 0) & (k >= 12) & np.isfinite(h)
    if not ok.all():
        keep = np.flatnonzero(ok)
        ps = [ps[i] for i in keep]
        h, k = h[keep], k[keep]
    vals = np.fromstring(b" ".join(ps), dtype=np.float64, sep=" ")
    if vals.size != k.sum():
        raise ValueError("unexpected posList formatting")
    xs, ys = vals[0::3], vals[1::3]
    m = k // 3
    ends = np.cumsum(m)
    starts = ends - m
    last = ends - 1
    closed = (xs[last] == xs[starts]) & (ys[last] == ys[starts])   # ring repeats 1st point
    cnt = m - closed
    cx = (np.add.reduceat(xs, starts) - np.where(closed, xs[last], 0.0)) / cnt
    cy = (np.add.reduceat(ys, starts) - np.where(closed, ys[last], 0.0)) / cnt
    return cx, cy, h


def centroids_slow(hs, ps):
    X, Y, H = [], [], []
    for h, p in zip(hs, ps):
        try:
            hv = float(h)
            v = [float(t) for t in p.split()]
        except ValueError:
            continue
        if len(v) % 3 or len(v) < 12 or not math.isfinite(hv):
            continue
        xs, ys = v[0::3], v[1::3]
        if xs[0] == xs[-1] and ys[0] == ys[-1]:
            xs.pop(); ys.pop()
        X.append(sum(xs) / len(xs)); Y.append(sum(ys) / len(ys)); H.append(hv)
    return np.array(X), np.array(Y), np.array(H)


def centroids(hs, ps):
    if not hs:
        e = np.zeros(0)
        return e, e, e
    try:
        return centroids_fast(hs, ps)
    except ValueError:
        return centroids_slow(hs, ps)


def quantize(cx, cy, h):
    qx = np.rint(cx * 1000.0 / XY_STEP_MM).astype(np.int32)
    qy = np.rint(cy * 1000.0 / XY_STEP_MM).astype(np.int32)
    qh = np.clip(np.rint(h * 1000.0 / H_STEP_MM), 0, 65535).astype(np.uint16)
    return qx, qy, qh


# ------------------------------------------------------------ worker processes
_zf = None


def _init(path):
    global _zf
    _zf = zipfile.ZipFile(path)


def _work(name):
    try:
        hs, ps = find_trees(_zf.read(name))
        cx, cy, h = centroids(hs, ps)
        return name, quantize(cx, cy, h), len(hs) - len(cx), None
    except Exception as e:                       # a broken tile must not kill hours of work
        return name, None, 0, repr(e)


# ------------------------------------------------------------------- encoding
def encode(qx, qy, qh):
    n = len(qx)
    if n == 0:
        payload = varint_enc([0, XY_STEP_MM, H_STEP_MM, 0, 0])
    else:
        qx, qy = qx.astype(np.int64), qy.astype(np.int64)
        x0, y0 = int(qx.min()), int(qy.min())
        rx, ry = qx - x0, qy - y0
        if max(int(rx.max()), int(ry.max())) >= 1 << 32:
            raise ValueError("extent too large for 32 bits/axis; raise XY_STEP_MM")
        code = spread(rx.astype(np.uint64)) | (spread(ry.astype(np.uint64)) << U(1))
        order = np.argsort(code, kind="stable")  # Morton order = spatial locality
        code, h = code[order], qh[order].astype(np.int64)
        del order
        dm = np.diff(code, prepend=U(0))         # >= 0, no sign bit needed
        zh = zz_enc(np.diff(h, prepend=0))       # neighbours have similar heights
        head = [n, XY_STEP_MM, H_STEP_MM, int(zz_enc([x0])[0]), int(zz_enc([y0])[0])]
        payload = np.concatenate([varint_enc(head), varint_enc(dm), varint_enc(zh)])
    return MAGIC + lzma.compress(
        payload.tobytes(), format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC32,
        filters=[{"id": lzma.FILTER_LZMA2, "preset": 9 | lzma.PRESET_EXTREME,
                  "lc": 3, "lp": 0, "pb": 0}])


def read_ctl(path):
    """-> x, y, height as float64 arrays (metres, EPSG:2180)."""
    raw = open(path, "rb").read()
    if raw[:4] != MAGIC:
        raise ValueError("not a CTL1 file")
    v = varint_dec(np.frombuffer(lzma.decompress(raw[4:]), dtype=np.uint8))
    n, xy, hh = int(v[0]), int(v[1]), int(v[2])
    x0, y0 = int(zz_dec(v[3:4])[0]), int(zz_dec(v[4:5])[0])
    if n == 0:
        e = np.zeros(0)
        return e, e, e
    code = np.cumsum(v[5:5 + n], dtype=np.uint64)
    qh = np.cumsum(zz_dec(v[5 + n:5 + 2 * n]))
    qx = compact(code).astype(np.int64) + x0
    qy = compact(code >> U(1)).astype(np.int64) + y0
    return qx * xy / 1000.0, qy * xy / 1000.0, qh * hh / 1000.0


# ------------------------------------------------------------------------ main
def build(zip_path, out_path):
    with zipfile.ZipFile(zip_path) as z:
        names = [i.filename for i in z.infolist()
                 if not i.is_dir() and i.filename.lower().endswith(".gml")]
    parts, dropped, failed = [], 0, 0
    with mp.Pool(os.cpu_count() or 1, initializer=_init, initargs=(zip_path,)) as pool:
        for name, q, d, err in pool.imap_unordered(_work, names):
            if err:
                failed += 1
                print(f"WARN {name}: {err}", file=sys.stderr)
                continue
            parts.append(q)
            dropped += d
    if parts:
        qx, qy, qh = (np.concatenate([p[i] for p in parts]) for i in range(3))
    else:
        qx = qy = np.zeros(0, np.int32); qh = np.zeros(0, np.uint16)
    del parts
    blob = encode(qx, qy, qh)
    tmp = out_path + ".part"                     # rename only when complete, so the
    with open(tmp, "wb") as f:                   # pipeline's "skip if exists" is safe
        f.write(blob)
    os.replace(tmp, out_path)
    print(f"{os.path.basename(zip_path)}: {len(qx)} trees, {len(blob)} bytes "
          f"({len(blob) * 8 / max(len(qx), 1):.1f} bits/tree), "
          f"{dropped} unusable trees, {failed} failed tiles", file=sys.stderr)


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) == 2 and a[0] == "--decode":
        x, y, h = read_ctl(a[1])
        np.savetxt(sys.stdout, np.column_stack([x, y, h]), delimiter=",",
                   fmt=("%.3f", "%.3f", "%.2f"), header="x,y,height", comments="")
    elif len(a) == 2:
        build(a[0], a[1])
    else:
        sys.exit(__doc__)