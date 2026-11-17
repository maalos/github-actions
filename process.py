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

