#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["pillow>=10", "numpy>=1.24"]
# ///
"""
Fantastic Photos — a small local web app for tidying photo libraries.

Run it:      uv run fantastic_photos.py
Then use it: a browser window opens at http://127.0.0.1:8756

Nothing is uploaded anywhere. The server runs on your own machine and only
ever reads the folders you point it at. Originals are never modified or
deleted — the merge only ever COPIES into a new folder.

Requires: Python 3.9+ and Pillow.  Nothing else.
"""

import base64
import csv
import hashlib
import http.server
import io
import json
import math
import os
import re
import shutil
import socketserver
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime

from PIL import Image, ImageOps, ImageDraw, ExifTags

try:
    import numpy as np
except ImportError:
    sys.stderr.write(
        "\nnumpy is required.\n"
        "  With uv:  uv run fantastic_photos.py   (installs it for you)\n"
        "  Or:       pip install numpy\n\n")
    raise SystemExit(1)

from multiprocessing import Pool, cpu_count

__version__ = "0.13.0"

PORT = 8756
IMG_EXT = {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".webp", ".bmp"}
VID_EXT = {".mov", ".mp4", ".m4v", ".avi"}

PROF, COARSE = 64, 16

# A real crop keeps a substantial part of the frame. Allowing tiny regions makes
# the matcher find coincidences: correlating six numbers against six numbers
# succeeds somewhere in almost any pair of photos.
MIN_SPAN = 0.35          # narrowest crop, as a fraction of the original's width
MIN_AREA = 0.25          # and it must cover at least this much of the area
SCALES = [i / 40 for i in range(14, 41)]

FINE_KEEP = 0.945        # profiles PROPOSE generously...
COARSE_KEEP = 0.88
VERIFY_KEEP = 0.86       # ...and the pixel check DECIDES
LINK_FRAC = 0.6          # to join a group, match this share of its members
MAX_GROUP = 12           # beyond this, say so rather than render a blob

TAGS = {v: k for k, v in ExifTags.TAGS.items()}

# ----------------------------------------------------------------- app state
STATE = {
    "sources": [],        # chosen source folders
    "dest": os.path.expanduser("~/Desktop/merged photos"),
    "options": {
        "naming": "date_time_orig",
        "undated": "prefix",       # prefix | skip
        "received": "subfolder",   # subfolder | mixed | skip
        "video": "subfolder",
        "find_crops": True,
        "find_bursts": True,
        "burst_seconds": 10,
        "burst_metres": 40,
    },
    "scan": {"running": False, "done": False, "step": "", "pct": 0, "error": None,
             "n": 0, "of": 0, "started": 0},
    "files": [],
    "pairs": [],
    "groups": [],         # clusters of related photos — one decision each
    "decisions": {},      # group id -> {"keep": [paths], "not_match": bool}
    "merge": {"running": False, "done": False, "log": [], "copied": 0, "skipped": 0},
}
LOCK = threading.Lock()


def build_groups(pairs):
    """Cluster related photos into one decision each.

    Naive transitive grouping (A~B, B~C, therefore {A,B,C}) is fragile: a single
    spurious link welds two unrelated clusters together permanently, and on a
    large library that cascades until most of the collection is one blob.

    So a photo only joins a group if it matches a decent share of the members
    already in it, not merely one of them.
    """
    adj = {}
    plist = {}
    for p in pairs:
        adj.setdefault(p["a"], set()).add(p["b"])
        adj.setdefault(p["b"], set()).add(p["a"])
        plist[(p["a"], p["b"])] = p
        plist[(p["b"], p["a"])] = p

    groups, claimed = [], set()
    for seed in sorted(pairs, key=lambda p: -p["score"]):
        a, b = seed["a"], seed["b"]
        if a in claimed or b in claimed:
            continue
        members = [a, b]
        # grow, only accepting photos linked to most of the group
        while len(members) < MAX_GROUP:
            best, best_hits = None, 0
            cands = set()
            for m in members:
                cands |= adj.get(m, set())
            for c in cands - set(members) - claimed:
                hits = sum(1 for m in members if c in adj.get(m, set()))
                if hits >= max(1, int(len(members) * LINK_FRAC + 0.999)) \
                        and hits > best_hits:
                    best, best_hits = c, hits
            if best is None:
                break
            members.append(best)
        claimed.update(members)

        ps, seen = [], set()
        for i, m in enumerate(members):
            for n in members[i + 1:]:
                q = plist.get((m, n))
                if q and id(q) not in seen:
                    seen.add(id(q))
                    ps.append(q)

        boxes, rels = {}, []
        for p in ps:
            if p["kind"] == "crop":
                boxes.setdefault(p["a"], p["box"])
                x0, y0, x1, y1 = p["box"]
                rels.append(
                    f"{os.path.basename(p['b'])} is a crop of "
                    f"{os.path.basename(p['a'])} "
                    f"(x {x0*100:.0f}\u2013{x1*100:.0f}%, y {y0*100:.0f}\u2013{y1*100:.0f}%)")
            elif p["kind"] == "burst":
                g = p.get("gap", 0)
                d = p.get("metres")
                where = (f", {d} m apart" if d is not None
                         else " (no location data \u2014 matched on time only)")
                rels.append(f"{os.path.basename(p['a'])} and "
                            f"{os.path.basename(p['b'])} were taken "
                            f"{g:.0f} second{'' if g == 1 else 's'} apart{where}")
            else:
                rels.append(f"{os.path.basename(p['a'])} and "
                            f"{os.path.basename(p['b'])} are framed almost identically")

        kinds = {p["kind"] for p in ps}
        groups.append({
            "id": "", "members": sorted(members), "boxes": boxes,
            "names": {m: os.path.basename(m) for m in members},
            "rels": rels[:12],
            "kind": kinds.pop() if len(kinds) == 1 else "mixed",
            "score": round(max(p["score"] for p in ps), 3) if ps else 0,
            "crowded": len(members) >= MAX_GROUP,
        })

    groups.sort(key=lambda g: (-len(g["members"]), -g["score"]))
    for i, g in enumerate(groups):
        g["id"] = f"g{i}"
    return groups


# ------------------------------------------------------------------- helpers
def is_image(n):
    return os.path.splitext(n)[1].lower() in IMG_EXT


def is_video(n):
    return os.path.splitext(n)[1].lower() in VID_EXT


def exif_date(path):
    """(datetime|None, source) — where the date came from matters, so we say."""
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            ifd = ex.get_ifd(0x8769)
            raw = ifd.get(TAGS.get("DateTimeOriginal")) or ex.get(TAGS.get("DateTime"))
            if raw:
                try:
                    return datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S"), "exif"
                except ValueError:
                    pass
    except Exception:
        pass
    m = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", os.path.basename(path))
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3])), "filename"
        except ValueError:
            pass
    return None, "none"


def camera_of(path):
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            mk = str(ex.get(TAGS.get("Make"), "") or "").strip("\x00 ").strip()
            md = str(ex.get(TAGS.get("Model"), "") or "").strip("\x00 ").strip()
            return f"{mk} {md}".strip()
    except Exception:
        return ""


GPSTAGS = {v: k for k, v in ExifTags.GPSTAGS.items()}


def gps_of(path):
    """(lat, lon) in decimal degrees, or (None, None)."""
    try:
        with Image.open(path) as im:
            g = im.getexif().get_ifd(0x8825)
        if not g:
            return None, None

        def deg(dms, ref):
            d, m, s = (float(x) for x in dms)
            v = d + m / 60 + s / 3600
            return -v if ref in ("S", "W") else v

        lat = g.get(GPSTAGS["GPSLatitude"])
        lar = g.get(GPSTAGS["GPSLatitudeRef"])
        lon = g.get(GPSTAGS["GPSLongitude"])
        lor = g.get(GPSTAGS["GPSLongitudeRef"])
        if lat and lar and lon and lor:
            return round(deg(lat, lar), 6), round(deg(lon, lor), 6)
    except Exception:
        pass
    return None, None


def verify_pixels(big_path, small_path, box):
    """Confirm a proposed match by comparing actual pixels.

    The profile stage only ever proposes; without this check, near-misses that
    happen to share a brightness curve sail straight through.
    """
    try:
        with Image.open(big_path) as im:
            im.load()
            b = ImageOps.exif_transpose(im).convert("L")
        W, H = b.size
        x0, y0, x1, y1 = box
        px = (max(0, int(x0 * W)), max(0, int(y0 * H)),
              min(W, int(x1 * W)), min(H, int(y1 * H)))
        if px[2] - px[0] < 32 or px[3] - px[1] < 32:
            return 0.0
        region = b.crop(px).resize((96, 96), Image.Resampling.LANCZOS)
        with Image.open(small_path) as im:
            im.load()
            sm = ImageOps.exif_transpose(im).convert("L").resize(
                (96, 96), Image.Resampling.LANCZOS)
        return pearson(list(region.getdata()), list(sm.getdata()))
    except Exception:
        return 0.0


def metres_between(a_lat, a_lon, b_lat, b_lon):
    """Great-circle distance in metres."""
    from math import radians, sin, cos, asin, sqrt
    p1, p2 = radians(a_lat), radians(b_lat)
    dp = p2 - p1
    dl = radians(b_lon - a_lon)
    h = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * 6371000 * asin(sqrt(h))


def new_name(rec):
    stem, ext = os.path.splitext(rec["name"])
    ext = ext.lower()
    if ext == ".jpeg":
        ext = ".jpg"
    if rec["date"]:
        d = datetime.fromisoformat(rec["date"])
        return f"{d:%Y-%m-%d %H.%M.%S} {stem}{ext}"
    return f"undated {stem}{ext}"


def md5(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while blk := f.read(chunk):
            h.update(blk)
    return h.hexdigest()


# -------------------------------------------------------- crop / near-match
def profiles(path):
    with Image.open(path) as im:
        im.load()
        up = ImageOps.exif_transpose(im).convert("L")

    def strip(w, h):
        return np.asarray(up.resize((w, h), Image.Resampling.LANCZOS),
                          dtype=np.float64).ravel()

    return {"w": up.size[0], "h": up.size[1],
            "cf": strip(PROF, 1), "rf": strip(1, PROF),
            "cc": strip(COARSE, 1), "rc": strip(1, COARSE)}


def pearson(x, y):
    a = np.asarray(x, dtype=np.float64)
    b = np.asarray(y, dtype=np.float64)
    if a.size < 4:
        return 0.0
    a = a - a.mean()
    b = b - b.mean()
    den = float(np.sqrt((a * a).sum() * (b * b).sum()))
    return 0.0 if den <= 0 else float((a * b).sum() / den)


def resample(seq, n):
    a = np.asarray(seq, dtype=np.float64)
    if a.size == n:
        return a
    return np.interp(np.linspace(0, a.size - 1, n), np.arange(a.size), a)


def slide(big, small, span, step=1):
    """Correlation of `small` against EVERY window of `big`, computed at once.

    The loop over offsets is replaced by one matrix multiply: sliding_window_view
    gives a view of all windows with no copying, and the correlations fall out
    as a single dot product.
    """
    n = big.size
    if span > n or span < 4:
        return -1.0, 0.0
    probe = resample(small, span)
    probe = probe - probe.mean()
    pnorm = float(np.sqrt((probe * probe).sum()))
    if pnorm <= 0:
        return -1.0, 0.0

    win = np.lib.stride_tricks.sliding_window_view(big, span)   # (n-span+1, span)
    wc = win - win.mean(axis=1, keepdims=True)
    den = np.sqrt((wc * wc).sum(axis=1)) * pnorm
    with np.errstate(divide="ignore", invalid="ignore"):
        corr = np.where(den > 0, (wc @ probe) / den, -1.0)
    k = int(np.argmax(corr))
    return float(corr[k]), k / n


def examine(A, B, n, keep, step):
    """A crop keeps the SAME aspect ratio, so width determines height."""
    ck, rk = ("cf", "rf") if n == PROF else ("cc", "rc")
    ratio = (A["w"] * B["h"]) / (A["h"] * B["w"])
    best = None
    for px in SCALES:
        py = px * ratio
        if py > 1.001 or px < MIN_SPAN or py < MIN_SPAN or px * py < MIN_AREA:
            continue
        sx = max(4, round(n * px))
        sy = max(4, round(n * py))
        cx, ox = slide(A[ck], B[ck], sx)
        if cx < keep:
            continue
        cy, oy = slide(A[rk], B[rk], sy)
        if cy < keep:
            continue
        s = min(cx, cy)
        if best is None or s > best[0]:
            best = (s, ox, oy, sx / n, sy / n)
    return best


# ------------------------------------------------- parallel comparison workers
_W = {}


def _worker_init(feats, keys):
    # Workers are spawned on macOS and Windows, so they re-import this module
    # and see none of the parent's state. Everything must be handed over here.
    _W["feats"] = feats
    _W["keys"] = keys


def _compare_chunk(rng):
    lo, hi = rng
    feats, keys = _W["feats"], _W["keys"]
    n = len(keys)
    out = []
    # Walk the upper triangle by flat index, so chunks are equal-sized.
    for idx in range(lo, hi):
        i = int((1 + math.isqrt(1 + 8 * idx)) // 2)
        while i * (i - 1) // 2 > idx:
            i -= 1
        while (i + 1) * i // 2 <= idx:
            i += 1
        j = idx - i * (i - 1) // 2
        if i >= n or j >= n:
            continue
        pa, pb = keys[j], keys[i]
        a, b = feats[pa], feats[pb]
        A, B, PA, PB = (a, b, pa, pb) if a["w"] * a["h"] >= b["w"] * b["h"] \
            else (b, a, pb, pa)
        if examine(A, B, COARSE, COARSE_KEEP, 1) is None:
            continue
        r = examine(A, B, PROF, FINE_KEEP, 3)
        if not r:
            continue
        score, ox, oy, sw, sh = r
        box = [ox, oy, ox + sw, oy + sh]
        pc = verify_pixels(PA, PB, box)
        if pc < VERIFY_KEEP:
            continue
        out.append({"a": PA, "b": PB, "box": box,
                    "score": round(min(score, pc), 3),
                    "kind": "same framing" if sw > .93 and sh > .93 else "crop"})
    return out


# ------------------------------------------------------------------- the scan
def do_scan():
    s = STATE["scan"]
    try:
        # Clear the previous run up front. Otherwise stale groups and a stale
        # plan stay on screen while the new scan runs, and they may describe
        # different folders or different options entirely.
        with LOCK:
            STATE["files"] = []
            STATE["pairs"] = []
            STATE["groups"] = []
            STATE["decisions"] = {}
            STATE["merge"] = {"running": False, "done": False, "log": [],
                              "copied": 0, "skipped": 0}
        s.update(running=True, done=False, error=None, step="listing files",
                 pct=2, n=0, of=0, started=time.time(), finished=None)
        recs = []
        for root in STATE["sources"]:
            for dirpath, _dirs, names in os.walk(root):
                for n in sorted(names):
                    if n.startswith("."):
                        continue
                    p = os.path.join(dirpath, n)
                    if not os.path.isfile(p):
                        continue
                    if is_image(n):
                        kind = "image"
                    elif is_video(n):
                        kind = "video"
                    else:
                        continue
                    recs.append({"path": p, "name": n, "src": root,
                                 "kind": kind, "size": os.path.getsize(p)})
        total = max(len(recs), 1)
        # Publish immediately so the counts can fill in while we work, rather
        # than the user staring at a bar with no numbers behind it.
        with LOCK:
            STATE["files"] = recs
        s.update(step=f"found {len(recs)} files", pct=6, n=0, of=len(recs))

        for i, r in enumerate(recs):
            if r["kind"] == "image":
                d, how = exif_date(r["path"])
                r["date"] = d.isoformat() if d else None
                r["date_src"] = how
                r["camera"] = camera_of(r["path"])
                r["lat"], r["lon"] = gps_of(r["path"])
            else:
                r["date"], r["date_src"], r["camera"] = None, "none", ""
                r["lat"] = r["lon"] = None
            r["received"] = (r["kind"] == "image" and not r["camera"])
            r["scanned"] = True
            if i % 10 == 0:
                s.update(pct=8 + int(30 * i / total), n=i, of=total,
                         step=f"reading dates and locations \u2014 {i} of {total}")

        s.update(step="checking for identical files", pct=40, n=0, of=0)
        bysize = {}
        for r in recs:
            bysize.setdefault(r["size"], []).append(r)
        for group in bysize.values():
            if len(group) < 2:
                continue
            byhash = {}
            for r in group:
                byhash.setdefault(md5(r["path"]), []).append(r)
            for same in byhash.values():
                for dup in same[1:]:
                    dup["exact_dup_of"] = same[0]["path"]

        pairs = []
        imgs = [r for r in recs if r["kind"] == "image"]

        # ---- bursts: close in TIME and (where we know it) close in SPACE ----
        # Two people shooting different subjects at the same moment must not be
        # grouped, so when both photos carry GPS we also require them to be near
        # each other. When either lacks GPS we fall back to time alone and say so.
        if STATE["options"]["find_bursts"]:
            s.update(step="looking for bursts", pct=42)
            secs = float(STATE["options"]["burst_seconds"])
            metres = float(STATE["options"]["burst_metres"])
            dated = sorted(
                [r for r in imgs if r["date"]],
                key=lambda r: r["date"])
            for a, b in zip(dated, dated[1:]):
                gap = (datetime.fromisoformat(b["date"])
                       - datetime.fromisoformat(a["date"])).total_seconds()
                if gap > secs:
                    continue
                dist = None
                if a["lat"] is not None and b["lat"] is not None:
                    dist = metres_between(a["lat"], a["lon"], b["lat"], b["lon"])
                    if dist > metres:
                        continue          # same moment, different place
                pairs.append({
                    "id": f"b{len(pairs)}", "a": a["path"], "b": b["path"],
                    "box": None, "kind": "burst", "score": 1.0,
                    "gap": gap, "metres": None if dist is None else round(dist),
                })
        if STATE["options"]["find_crops"] and len(imgs) > 1:
            s.update(step=f"fingerprinting {len(imgs)} images", pct=45)
            feats = {}
            for i, r in enumerate(imgs):
                try:
                    feats[r["path"]] = profiles(r["path"])
                except Exception:
                    pass
                if i % 5 == 0:
                    s.update(pct=45 + int(25 * i / max(len(imgs), 1)), n=i,
                             of=len(imgs),
                             step=f"fingerprinting images \u2014 {i} of {len(imgs)}")

            keys = [r["path"] for r in imgs if r["path"] in feats]
            npair = len(keys) * (len(keys) - 1) // 2
            ncpu = max(1, min(cpu_count() - 1, 8))
            s.update(step=f"comparing {npair:,} pairs on {ncpu} cores",
                     pct=70, n=0, of=npair)

            chunk = max(2000, npair // (ncpu * 8) + 1)
            ranges = [(i, min(i + chunk, npair)) for i in range(0, npair, chunk)]
            found, done = [], 0
            if npair:
                with Pool(ncpu, initializer=_worker_init,
                          initargs=(feats, keys)) as pool:
                    for part in pool.imap_unordered(_compare_chunk, ranges):
                        found.extend(part)
                        done += 1
                        s.update(pct=70 + int(28 * done / len(ranges)),
                                 n=done * chunk, of=npair,
                                 step=f"comparing on {ncpu} cores \u2014 "
                                      f"{min(done * chunk, npair):,} of {npair:,} "
                                      f"pairs, {len(found)} match(es)")
            for k, pr in enumerate(found):
                pr["id"] = f"c{k}"
                pairs.append(pr)

        groups = build_groups(pairs)
        with LOCK:
            STATE["files"] = recs
            STATE["pairs"] = pairs
            STATE["groups"] = groups
            # default: keep everything. Doing nothing must never lose a photo.
            STATE["decisions"] = {
                g["id"]: {"keep": list(g["members"]), "not_match": False}
                for g in groups}
        s.update(step="done", pct=100, running=False, done=True,
                 finished=time.time())
    except Exception as e:
        s.update(running=False, done=False, error=f"{type(e).__name__}: {e}")


# ------------------------------------------------------------------ the merge
def plan():
    """Work out the final name for every file, honouring decisions."""
    drop = set()
    for g in STATE["groups"]:
        d = STATE["decisions"].get(g["id"])
        if not d or d.get("not_match"):
            continue                       # treat as unrelated: keep everything
        keep = set(d.get("keep", g["members"]))
        for m in g["members"]:
            if m not in keep:
                drop.add(m)
    rows, used = [], {}
    for r in STATE["files"]:
        why = None
        sub = ""
        if r.get("exact_dup_of"):
            why = "identical to another file"
        elif r["path"] in drop:
            why = "you chose the other one"
        elif r["kind"] == "video":
            if STATE["options"]["video"] == "skip":
                why = "video (skipped)"
            elif STATE["options"]["video"] == "subfolder":
                sub = "video"
        elif r["received"]:
            if STATE["options"]["received"] == "skip":
                why = "not taken on a camera (skipped)"
            elif STATE["options"]["received"] == "subfolder":
                sub = "received"
        if not why and not r["date"] and STATE["options"]["undated"] == "skip":
            why = "no date (skipped)"

        if why:
            rows.append({"path": r["path"], "name": r["name"], "new": None,
                         "sub": "", "skip": why})
            continue
        nm = new_name(r)
        key = (sub, nm.lower())
        if key in used:
            used[key] += 1
            stem, ext = os.path.splitext(nm)
            nm = f"{stem} ({used[key]}){ext}"
        else:
            used[key] = 1
        rows.append({"path": r["path"], "name": r["name"], "new": nm,
                     "sub": sub, "skip": None})
    return rows


def do_merge():
    m = STATE["merge"]
    m.update(running=True, done=False, log=[], copied=0, skipped=0)
    try:
        dest = STATE["dest"]
        os.makedirs(dest, exist_ok=True)
        rows = plan()
        manifest = os.path.join(dest, "_manifest.csv")
        with open(manifest, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["original", "new_name", "subfolder", "skipped_because"])
            for r in rows:
                w.writerow([r["path"], r["new"] or "", r["sub"], r["skip"] or ""])
                if r["skip"]:
                    m["skipped"] += 1
                    continue
                out_dir = os.path.join(dest, r["sub"]) if r["sub"] else dest
                os.makedirs(out_dir, exist_ok=True)
                shutil.copy2(r["path"], os.path.join(out_dir, r["new"]))
                m["copied"] += 1
        m["log"].append(f"manifest written to {manifest}")
        m.update(running=False, done=True)
    except Exception as e:
        m["log"].append(f"ERROR {type(e).__name__}: {e}")
        m.update(running=False, done=False)


# ------------------------------------------------------------------- thumbs
def thumbnail(path, box=None, size=340):
    with Image.open(path) as im:
        im.load()
        d = ImageOps.exif_transpose(im).convert("RGB")
    d.thumbnail((size, size), Image.Resampling.LANCZOS)
    if box:
        dr = ImageDraw.Draw(d)
        W, H = d.size
        dr.rectangle([box[0]*W, box[1]*H, box[2]*W, box[3]*H],
                     outline=(255, 55, 55), width=3)
    buf = io.BytesIO()
    d.save(buf, "JPEG", quality=76)
    return buf.getvalue()


# --------------------------------------------------------------------- HTTP
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if u.path == "/api/state":
            with LOCK:
                sc = dict(STATE["scan"])
                if sc.get("started"):
                    end = sc.get("finished") or time.time()
                    sc["elapsed"] = round(end - sc["started"], 1)
                    if sc["running"] and sc["pct"] > 4:
                        sc["eta"] = round(sc["elapsed"] * (100 - sc["pct"]) / sc["pct"])
                return self._send(200, {
                    "sources": STATE["sources"], "dest": STATE["dest"],
                    "options": STATE["options"], "scan": sc,
                    "counts": summary(), "groups": STATE["groups"],
                    "decisions": STATE["decisions"], "merge": STATE["merge"],
                })
        if u.path == "/api/roots":
            # Windows has drive letters, macOS/Linux have /Volumes and /mnt.
            # The browser cannot know which, so the server says.
            home = os.path.expanduser("~")
            out = [{"name": "Home", "path": home}]
            for label, sub in (("Desktop", "Desktop"), ("Pictures", "Pictures"),
                               ("Downloads", "Downloads"), ("Documents", "Documents")):
                q = os.path.join(home, sub)
                if os.path.isdir(q):
                    out.append({"name": label, "path": q})
            if os.name == "nt":
                for d in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                    q = f"{d}:\\"
                    if os.path.isdir(q):
                        out.append({"name": f"{d}: drive", "path": q})
            else:
                for q in ("/Volumes", "/media", "/mnt"):
                    if os.path.isdir(q):
                        out.append({"name": os.path.basename(q) or q, "path": q})
            return self._send(200, {"roots": out, "sep": os.sep,
                                    "version": __version__})
        if u.path == "/api/browse":
            base = q.get("path", [os.path.expanduser("~")])[0]
            base = os.path.abspath(os.path.expanduser(base))
            try:
                entries = []
                for n in sorted(os.listdir(base)):
                    if n.startswith("."):
                        continue
                    p = os.path.join(base, n)
                    if os.path.isdir(p):
                        try:
                            imgs = sum(1 for x in os.listdir(p) if is_image(x))
                        except Exception:
                            imgs = 0
                        entries.append({"name": n, "path": p, "images": imgs})
                parent = os.path.dirname(base.rstrip(os.sep)) or base
                return self._send(200, {"path": base, "parent": parent,
                                        "sep": os.sep, "dirs": entries})
            except Exception as e:
                return self._send(200, {"error": str(e), "path": base,
                                        "parent": os.path.dirname(base), "dirs": []})
        if u.path == "/api/thumb":
            p = q.get("p", [""])[0]
            box = q.get("box", [""])[0]
            b = [float(x) for x in box.split(",")] if box else None
            try:
                return self._send(200, thumbnail(p, b), "image/jpeg")
            except Exception:
                return self._send(404, b"", "image/jpeg")
        if u.path == "/api/destinfo":
            d = STATE["dest"].strip()
            parent = os.path.dirname(d.rstrip("/"))
            info = {"path": d, "exists": False, "is_dir": False, "count": 0,
                    "parent_exists": os.path.isdir(parent) if parent else False,
                    "writable": False, "parent": parent}
            if d:
                info["exists"] = os.path.exists(d)
                info["is_dir"] = os.path.isdir(d)
                if info["is_dir"]:
                    try:
                        info["count"] = len([x for x in os.listdir(d)
                                             if not x.startswith(".")])
                        info["writable"] = os.access(d, os.W_OK)
                    except Exception:
                        pass
                elif info["parent_exists"]:
                    info["writable"] = os.access(parent, os.W_OK)
            return self._send(200, info)
        if u.path == "/api/plan":
            rows = plan()
            return self._send(200, {
                "rows": rows[:400], "total": len(rows),
                "copy": sum(1 for r in rows if not r["skip"]),
                "skip": sum(1 for r in rows if r["skip"])})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(n) or b"{}")
        if u.path == "/api/sources":
            with LOCK:
                STATE["sources"] = data.get("sources", [])
            return self._send(200, {"ok": True})
        if u.path == "/api/dropped":
            added, bad = [], []
            for raw in data.get("paths", []):
                q = raw.strip()
                if q.startswith("file://"):          # Finder / Explorer drag
                    q = urllib.parse.unquote(urllib.parse.urlparse(q).path)
                q = os.path.abspath(os.path.expanduser(q))
                if os.path.isfile(q):
                    q = os.path.dirname(q)      # dropped a photo -> use its folder
                if os.path.isdir(q):
                    if q not in STATE["sources"]:
                        added.append(q)
                else:
                    bad.append(raw)
            with LOCK:
                STATE["sources"].extend(added)
            return self._send(200, {"added": added, "bad": bad,
                                    "sources": STATE["sources"]})
        if u.path == "/api/locate":
            # Browsers refuse to reveal a dragged folder's path, on every OS.
            # We do get its NAME and some of the filenames inside it, so search
            # the places photos actually live for a directory matching both.
            name = (data.get("name") or "").strip()
            kids = set(data.get("children") or [])
            if not name:
                return self._send(200, {"matches": [], "searched": [],
                                        "reason": "the browser gave no folder name"})

            home = os.path.expanduser("~")
            roots = [os.path.dirname(x) for x in STATE["sources"]]
            for sub in ("Pictures", "Desktop", "Downloads", "Documents",
                        "OneDrive", "Photos", "projects"):
                roots.append(os.path.join(home, sub))
            roots.append(home)
            if os.name == "nt":
                for d in "DEFGHIJKLMNOPQRSTUVWXYZC":     # removable drives first
                    q = f"{d}:\\"
                    if os.path.isdir(q):
                        roots.append(q)
            else:
                roots += ["/Volumes", "/media", "/mnt"]

            seen, ordered = set(), []
            for r in roots:
                if r and os.path.isdir(r) and r not in seen:
                    seen.add(r)
                    ordered.append(r)

            SKIP = {"Library", "node_modules", ".git", "Applications", "System",
                    ".Trash", "venv", ".venv", "__pycache__", "Windows",
                    "Program Files", "Program Files (x86)", "ProgramData",
                    "AppData", "$Recycle.Bin", "System Volume Information",
                    "WindowsApps", "Recovery"}
            matches, scanned, searched = [], 0, []
            for root in ordered:
                searched.append(root)
                try:
                    for dirpath, dirs, _files in os.walk(root, topdown=True):
                        depth = dirpath[len(root):].count(os.sep)
                        if depth >= 5:
                            dirs[:] = []
                            continue
                        dirs[:] = [d for d in dirs
                                   if not d.startswith(".") and d not in SKIP]
                        scanned += 1
                        if scanned > 60000:
                            break
                        if name in dirs:
                            cand = os.path.join(dirpath, name)
                            if cand in matches:
                                continue
                            try:
                                inside = set(os.listdir(cand))
                            except Exception:
                                continue
                            if not kids or (kids & inside):
                                matches.append(cand)
                                if len(matches) >= 6:
                                    break
                except Exception:
                    continue
                if len(matches) >= 6 or scanned > 60000:
                    break
            return self._send(200, {"matches": matches, "scanned": scanned,
                                    "searched": searched[:8], "name": name})
        if u.path == "/api/resolve":
            q = (data.get("path") or "").strip()
            if q.startswith("file://"):
                q = urllib.parse.unquote(urllib.parse.urlparse(q).path)
            q = os.path.abspath(os.path.expanduser(q))
            if os.path.isfile(q):
                q = os.path.dirname(q)
            ok = os.path.isdir(q)
            return self._send(200, {"dir": q if ok else None,
                                    "name": os.path.basename(q.rstrip(os.sep)) if ok else None})
        if u.path == "/api/joindest":
            d = (data.get("dir") or "").strip()
            n = (data.get("name") or "merged photos").strip()
            joined = os.path.join(d, n)
            with LOCK:
                STATE["dest"] = joined
            return self._send(200, {"dest": joined})
        if u.path == "/api/mkdir":
            d = (data.get("dest") or STATE["dest"]).strip()
            try:
                os.makedirs(d, exist_ok=True)
                return self._send(200, {"ok": True, "path": d})
            except Exception as e:
                return self._send(200, {"ok": False,
                                        "error": f"{type(e).__name__}: {e}"})
        if u.path == "/api/dest":
            with LOCK:
                STATE["dest"] = data.get("dest", STATE["dest"])
            return self._send(200, {"ok": True})
        if u.path == "/api/options":
            with LOCK:
                STATE["options"].update(data)
            return self._send(200, {"ok": True})
        if u.path == "/api/scan":
            if not STATE["scan"]["running"]:
                threading.Thread(target=do_scan, daemon=True).start()
            return self._send(200, {"ok": True})
        if u.path == "/api/decide":
            with LOCK:
                STATE["decisions"][data["id"]] = {
                    "keep": data.get("keep", []),
                    "not_match": bool(data.get("not_match")),
                }
            return self._send(200, {"ok": True})
        if u.path == "/api/merge":
            if not STATE["merge"]["running"]:
                threading.Thread(target=do_merge, daemon=True).start()
            return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})


def summary():
    f = STATE["files"]
    # Only records that have been through the metadata pass are counted, so a
    # partial scan reports what it knows rather than what it hasn't looked at.
    seen = [r for r in f if r.get("scanned")]
    return {
        "total": len(f),
        "examined": len(seen),
        "images": sum(1 for r in f if r["kind"] == "image"),
        "videos": sum(1 for r in f if r["kind"] == "video"),
        "dated": sum(1 for r in seen if r.get("date")),
        "undated": sum(1 for r in seen if r["kind"] == "image" and not r.get("date")),
        "received": sum(1 for r in seen if r.get("received")),
        "gps": sum(1 for r in seen if r.get("lat") is not None),
        "exact_dups": sum(1 for r in f if r.get("exact_dup_of")),
        "groups": len(STATE["groups"]),
        "in_groups": sum(len(g["members"]) for g in STATE["groups"]),
        "crops": sum(1 for p in STATE["pairs"] if p["kind"] == "crop"),
    }


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>Fantastic Photos</title>
<style>
:root{--bd:#e3e3df;--mut:#6b6b66;--acc:#2f6f4f;--warn:#9d1111}
*{box-sizing:border-box}
body{font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
 margin:0;background:#f6f6f4;color:#1b1b1b}
header{background:#fff;border-bottom:1px solid var(--bd);padding:14px 26px}
h1{font-size:18px;margin:0} .sub{color:var(--mut);font-size:13px;margin-top:2px}
main{max-width:1060px;margin:0 auto;padding:22px 26px 80px}
.step{background:#fff;border:1px solid var(--bd);border-radius:10px;
 padding:18px 20px;margin-bottom:16px}
.step h2{font-size:15px;margin:0 0 12px;display:flex;align-items:center;gap:9px}
.num{background:#1b1b1b;color:#fff;width:21px;height:21px;border-radius:50%;
 display:inline-flex;align-items:center;justify-content:center;font-size:12px;flex:none}
button{font:inherit;padding:7px 14px;border-radius:7px;border:1px solid var(--bd);
 background:#fff;cursor:pointer}
button:hover{background:#f2f2ee}
button.primary{background:var(--acc);color:#fff;border-color:var(--acc);font-weight:600}
button.primary:disabled{background:#bbb;border-color:#bbb;cursor:not-allowed}
button.small{padding:4px 9px;font-size:13px}
.row{display:flex;gap:9px;align-items:center;flex-wrap:wrap}
.list{border:1px solid var(--bd);border-radius:7px;max-height:230px;overflow:auto;
 margin:9px 0;background:#fcfcfa}
.list div{padding:7px 11px;border-bottom:1px solid #f0f0ec;display:flex;
 justify-content:space-between;align-items:center;gap:10px;cursor:pointer}
.list div:hover{background:#f2f2ee} .list div:last-child{border-bottom:0}
.drop{border:2px dashed #c3d0de;border-radius:8px;padding:14px 12px;text-align:center;
 color:#4a6d90;background:#f4f8fc;margin-bottom:10px;transition:.12s;font-size:13px}
.drop{color:#4a6d90}
.drop.over{border-color:#2d5c8a;background:#e4eef8;color:#2d5c8a}
.drop.out{border-color:#c3ddc9;background:#f3faf5;color:#3f7a56}
.drop.out.over{border-color:var(--acc);background:#e6f2ea;color:var(--acc)}
.drop.bad{border-color:var(--warn);color:var(--warn)}
.io{display:grid;grid-template-columns:1fr 1fr;gap:20px;align-items:start}
@media(max-width:820px){.io{grid-template-columns:1fr}}
.col{min-width:0;border:1px solid var(--bd);border-radius:9px;padding:13px 14px;
 background:#fcfcfa}
.col.in{border-left:3px solid #4a7fb5}
.col.out{border-left:3px solid var(--acc)}
.iohead{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;
 margin-bottom:9px}
.col.in .iohead{color:#2d5c8a} .col.out .iohead{color:var(--acc)}
.iohead span{display:block;font-weight:400;font-size:12px;text-transform:none;
 letter-spacing:0;color:var(--mut);margin-top:1px}
.col .row{gap:6px} .col .row button{padding:3px 7px;font-size:12px}
.col .list{max-height:190px}
.picker{border:1px solid var(--bd);border-radius:8px;padding:11px;
 margin-top:9px;background:#fbfbf9}
.picker .list{margin-top:0}
.chosen.dest{background:#eef2f9;border-color:#ccd8ea}
#chosen,#chosendest{margin-top:10px}
#destpath{margin-top:8px;font-size:13px}
.st-new{color:#8a5300} .st-ok{color:var(--acc)} .st-warn{color:var(--warn)}
.flabel{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);
 font-weight:650;margin:14px 0 6px}
.crumb{font-size:13px;color:var(--mut);margin-bottom:5px;word-break:break-all}
.chosen{display:flex;justify-content:space-between;padding:7px 11px;background:#eef4ef;
 border:1px solid #cfe0d4;border-radius:6px;margin-bottom:6px;font-size:14px}
.stats{display:flex;gap:22px;flex-wrap:wrap;margin:10px 0 4px}
.stat b{display:block;font-size:21px;font-weight:650}
.stat span{font-size:12px;color:var(--mut)}
.bar{height:7px;background:#e8e8e4;border-radius:4px;overflow:hidden;margin:9px 0 5px}
.bar i{display:block;height:100%;background:var(--acc);transition:width .3s}
label.opt{display:block;padding:5px 0;font-size:14px}
label.opt input{margin-right:7px}
.card{border:1px solid var(--bd);border-radius:9px;padding:13px;margin-bottom:13px;
 background:#fcfcfa}
.card .hd{display:flex;justify-content:space-between;align-items:center;margin-bottom:9px}
.badge{font-size:11px;font-weight:650;text-transform:uppercase;letter-spacing:.05em;
 padding:2px 9px;border-radius:10px}
.badge.crop{background:#ffe7e7;color:var(--warn)}
.badge.same{background:#e8f0ff;color:#0f47a1}
.badge.burst{background:#fff2dd;color:#8a5300}
.badge.mixed{background:#efe7ff;color:#4b1fa8}
.imgs{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.imgs figure{margin:0;max-width:300px}
.imgs img{max-width:300px;border-radius:5px;display:block;background:#eee}
.imgs figcaption{font-size:12px;color:var(--mut);margin-top:5px;word-break:break-all}
.imgs figure.member{max-width:240px}
.imgs figure.member.clickable{cursor:pointer;user-select:none}
.imgs figure.member.clickable:hover{filter:brightness(.97)}
.imgs figure.member img{max-width:224px;transition:opacity .15s,filter .15s}
.skipmark{position:absolute;top:50%;left:0;right:0;transform:translateY(-50%);
 text-align:center;font-weight:800;font-size:15px;letter-spacing:.16em;
 color:#fff;background:rgba(157,17,17,.82);padding:5px 0;pointer-events:none}
button.toggle{margin-top:6px;width:100%;font-size:12px}
button.toggle.on{background:var(--acc);color:#fff;border-color:var(--acc)}
button.toggle.off{background:#fff;color:var(--warn);border-color:#e0b4b4}
.rels{font-size:12.5px;color:var(--mut);margin:2px 0 10px;line-height:1.6}
.choices{display:flex;gap:7px;flex-wrap:wrap;align-items:center}
.choices button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{text-align:left;padding:5px 8px;border-bottom:1px solid #f0f0ec}
th{color:var(--mut);font-weight:600}
.old{color:var(--mut)} .skip{color:var(--warn)}
code{background:#f0f0ec;padding:1px 5px;border-radius:4px;font-size:13px}
.hint{color:var(--mut);font-size:13px;margin-top:7px}
input[type=text]{font:inherit;padding:6px 9px;border:1px solid var(--bd);
 border-radius:6px;width:100%;max-width:480px}
</style></head><body>
<header><h1>Fantastic Photos</h1>
<div class="sub">Runs on your machine. Nothing is uploaded. Originals are only ever copied, never moved or deleted.</div>
</header><main>

<div class="step"><h2><span class="num">1</span> Folders</h2>
 <div class="io">
 <div class="col in">
 <div class="iohead">In <span>folders to merge</span></div>
 <div id="drop" class="drop">Drag folders here from Finder or Explorer</div>
 <button class="small" id="srctog" onclick="tog('src')">+ choose folders&hellip;</button>
 <div id="src_pick" class="picker" style="display:none">
  <div class="crumb" id="src_crumb"></div>
  <div class="list" id="src_list"></div>
  <div class="row">
   <button class="small" onclick="B.src.up()">&uarr; up</button>
   <span id="src_roots"></span>
   <span style="flex:1"></span>
   <button class="small" onclick="B.src.pickCurrent()">+ add the folder I'm in</button></div>
 </div>
 <div id="chosen"></div>

 </div>
 <div class="col out">
 <div class="iohead">Out <span>where the merged copies go</span></div>
 <div id="dropout" class="drop out">Drag a folder here to put the merged copies inside it</div>
 <button class="small" id="dsttog" onclick="tog('dst')">choose destination&hellip;</button>
 <div id="dst_pick" class="picker" style="display:none">
  <div class="crumb" id="dst_crumb"></div>
  <div class="list" id="dst_list"></div>
  <div class="row">
   <button class="small" onclick="B.dst.up()">&uarr; up</button>
   <span id="dst_roots"></span>
   <span style="flex:1"></span>
   <span class="sub">new folder:</span>
   <input type="text" id="newname" value="merged photos" style="width:170px">
   <button class="small" onclick="B.dst.pickCurrent()">create here</button></div>
  <div class="hint">Pick where it should live, then name the new folder.
   It is created when you press Copy &mdash; nothing existing is touched.</div>
 </div>
 <div id="chosendest"></div>
 <input type="text" id="destpath" spellcheck="false"
   placeholder="/path/to/merged photos"
   oninput="destTyped(this.value)">
 <div class="row" style="margin-top:7px">
  <button class="small" id="mkbtn" onclick="mkdirNow()">create folder</button>
  <span class="sub" id="deststatus"></span></div>
 </div>
 </div>
</div>

<div class="step"><h2><span class="num">2</span> Options</h2>
 <label class="opt"><input type="checkbox" id="o_crops" checked
   onchange="setopt('find_crops',this.checked)"> Look for cropped and near-duplicate photos</label>
 <label class="opt"><input type="checkbox" id="o_recv" checked
   onchange="setopt('received',this.checked?'subfolder':'mixed')">
   Put received images (WhatsApp, downloads) in a <code>received</code> subfolder</label>
 <label class="opt"><input type="checkbox" id="o_vid" checked
   onchange="setopt('video',this.checked?'subfolder':'mixed')">
   Put videos in a <code>video</code> subfolder</label>
 <label class="opt"><input type="checkbox" id="o_burst" checked
   onchange="setopt('find_bursts',this.checked)"> Group bursts &mdash; photos taken
   within <input type="number" id="o_bsec" value="10" min="1" max="300" style="width:62px"
   onchange="setopt('burst_seconds',+this.value)"> seconds and
   <input type="number" id="o_bm" value="40" min="5" max="1000" style="width:68px"
   onchange="setopt('burst_metres',+this.value)"> metres of each other</label>
 <div class="hint">Undated photos are named <code>undated IMG_1234.jpg</code> and land in the same folder.
  Photos without GPS are matched on time alone, and the group says so.</div>
</div>

<div class="step"><h2><span class="num">3</span> Scan</h2>
 <button class="primary" id="scanbtn" onclick="scan()">Scan folders</button>
 <div class="bar" id="barwrap" style="display:none"><i id="bar" style="width:0"></i></div>
 <div class="sub" id="scanmsg"></div>
 <div class="stats" id="stats"></div>
</div>

<div class="step" id="reviewstep" style="display:none">
 <h2><span class="num">4</span> Review matches</h2>
 <div class="sub" id="revsub"></div>
 <div id="pairs"></div>
</div>

<div class="step" id="planstep" style="display:none">
 <h2><span class="num">5</span> Preview the result</h2>
 <div class="sub" id="plansub"></div>
 <div class="list" style="max-height:340px"><table id="plan"></table></div>
</div>

<div class="step" id="gostep" style="display:none">
 <h2><span class="num">6</span> Create the merged folder</h2>
 <div class="sub" id="destecho" style="margin-bottom:9px"></div>
 <button class="primary" id="gobtn" onclick="merge()">Copy files</button>
 <div class="sub" id="mergemsg"></div>
</div>

</main><script>
let HOME='', SEP='/', S={};
async function j(u,o){const r=await fetch(u,o);return r.json()}
async function post(u,b){return j(u,{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify(b||{})})}

let sources=[];

// One folder browser, instantiated twice: sources and destination.
// Same widget, same gestures, different consequence.
function makeBrowser(id, onPick){
 let cur='', parent='';
 async function go(p){
  const d=await j('/api/browse?path='+encodeURIComponent(p));
  HOME=HOME||d.path; cur=d.path; parent=d.parent; SEP=d.sep||SEP;
  document.getElementById(id+'_crumb').textContent=d.path;
  const el=document.getElementById(id+'_list'); el.innerHTML='';
  if(!d.dirs.length){el.innerHTML='<div class="sub" style="cursor:default">'+
    'no sub-folders here</div>'}
  d.dirs.forEach(x=>{
   const div=document.createElement('div');
   const label=document.createElement('span'); label.textContent=x.name;
   const right=document.createElement('span'); right.className='sub';
   right.innerHTML=(x.images?x.images+' images &nbsp;':'');
   const b=document.createElement('button'); b.className='small';
   b.textContent=(id==='src'?'add':'use');
   b.onclick=e=>{e.stopPropagation();onPick(x.path)};
   right.appendChild(b);
   div.appendChild(label); div.appendChild(right);
   div.onclick=()=>go(x.path); el.appendChild(div)})
 }
 return {go, up:()=>go(parent||cur),
         pickCurrent:()=>onPick(cur), current:()=>cur};
}

const B={};
async function loadRoots(){
 const r=await j('/api/roots'); SEP=r.sep||SEP;
 ['src','dst'].forEach(w=>{
  const el=document.getElementById(w+'_roots'); if(!el)return;
  el.innerHTML=r.roots.map(x=>'<button class="small" onclick="B.'+w+
   '.go(\''+x.path.replace(/\\/g,'\\\\').replace(/'/g,"\\'")+'\')">'+
   x.name+'</button>').join(' ')})
}
function tog(which){
 const el=document.getElementById(which+'_pick');
 const btn=document.getElementById(which==='src'?'srctog':'dsttog');
 const open=el.style.display==='none';
 el.style.display=open?'block':'none';
 btn.textContent=open?'done':(which==='src'?'+ choose folders\u2026':'choose destination\u2026');
 if(open&&!B[which].current())B[which].go('');
}

function add(p){if(!sources.includes(p)){sources.push(p);post('/api/sources',{sources});draw()}}
function rm(p){sources=sources.filter(x=>x!==p);post('/api/sources',{sources});draw()}
function draw(){document.getElementById('chosen').innerHTML=sources.length?
 sources.map(p=>'<div class="chosen"><span>'+p+'</span><button class="small" onclick="rm(\''+
 p.replace(/'/g,"\\'")+'\')">remove</button></div>').join('')
 :'<div class="sub" style="padding:3px 0">none chosen yet</div>'}
function drawdest(v){
 const box=document.getElementById('chosendest'); if(box)box.innerHTML='';
 const f=document.getElementById('destpath'); if(f)f.value=v||'';
 const e=document.getElementById('destecho'); if(e)e.textContent=v;
 destStatus()}
function setopt(k,v){const o={};o[k]=v;post('/api/options',o)}
let destTimer=null;
function setdest(v){post('/api/dest',{dest:v}); drawdest(v)}
function destTyped(v){                       // typing: debounce, don't spam the server
 clearTimeout(destTimer);
 destTimer=setTimeout(async()=>{await post('/api/dest',{dest:v});
  const e=document.getElementById('destecho'); if(e)e.textContent=v;
  destStatus()},250)}
async function destStatus(){
 const i=await j('/api/destinfo');
 const el=document.getElementById('deststatus');
 const btn=document.getElementById('mkbtn');
 if(!i.path){el.className='sub st-warn';el.textContent='no destination set';
  btn.disabled=true;return}
 if(i.exists&&!i.is_dir){el.className='sub st-warn';
  el.textContent='that path is a file, not a folder';btn.disabled=true;return}
 if(i.is_dir){
  btn.disabled=true;
  if(i.count){el.className='sub st-warn';
   el.textContent='folder exists and already holds '+i.count+
    ' item(s) \u2014 copies will be added alongside them'}
  else{el.className='sub st-ok';el.textContent='folder exists and is empty'}
  if(!i.writable){el.className='sub st-warn';el.textContent+=' \u2014 not writable'}
  return}
 btn.disabled=false;
 if(i.parent_exists){el.className='sub st-new';
  el.textContent='does not exist yet \u2014 will be created when you press Copy'}
 else{el.className='sub st-warn';
  el.textContent='the folder above it ('+i.parent+') does not exist either'}
}
async function mkdirNow(){
 const v=document.getElementById('destpath').value;
 const r=await post('/api/mkdir',{dest:v});
 const el=document.getElementById('deststatus');
 if(!r.ok){el.className='sub st-warn';el.textContent=r.error;return}
 destStatus()}
B.src=makeBrowser('src', add);
B.dst=makeBrowser('dst', async p=>{
 const n=(document.getElementById('newname').value||'merged photos').trim();
 const r=await post('/api/joindest',{dir:p,name:n}); drawdest(r.dest)});

// Finder/Explorer drags carry a file:// URL, which we can turn back into a real
// path. Browsers never expose the path any other way.
function dropPaths(dt){
 const raw=(dt.getData('text/uri-list')||dt.getData('text/plain')||'').trim();
 if(!raw)return [];
 return raw.split(/[\r\n]+/).filter(x=>x&&!x.startsWith('#')).map(u=>{
  try{return u.startsWith('file:')?decodeURIComponent(new URL(u).pathname):u}
  catch(_){return u}});
}
// What the browser actually handed over — shown when a drop can't be resolved.
function dropDebug(dt){
 const bits=[];
 bits.push('types: '+(dt.types?[...dt.types].join(', '):'none'));
 if(dt.items)for(const it of dt.items){
  let e=null; try{e=it.webkitGetAsEntry&&it.webkitGetAsEntry()}catch(_){}
  bits.push('item '+it.kind+'/'+(it.type||'-')+
   (e?(' entry:'+e.name+(e.isDirectory?' [dir]':' [file]')):' entry:none'))}
 if(dt.files&&dt.files.length)
  bits.push('files: '+[...dt.files].map(f=>f.name).join(', '));
 return bits.join(' | ');
}
// Read a handful of names from inside a dragged folder, to identify it.
function entryChildren(entry, limit){
 return new Promise(res=>{
  if(!entry||!entry.isDirectory)return res([]);
  try{
   const rd=entry.createReader(); const out=[];
   const step=()=>rd.readEntries(es=>{
    if(!es.length||out.length>=limit)return res(out.slice(0,limit));
    es.forEach(e=>out.push(e.name)); step()},()=>res(out));
   step();
  }catch(_){res([])}})
}
// Ask the server to find a folder with this name containing these files.
async function locateByName(dt){
 if(!dt.items)return null;
 for(const it of dt.items){
  let e=null; try{e=it.webkitGetAsEntry&&it.webkitGetAsEntry()}catch(_){}
  if(e&&e.isDirectory){
   const kids=await entryChildren(e,12);
   const r=await post('/api/locate',{name:e.name,children:kids});
   return {name:e.name, matches:r.matches||[], searched:r.searched||[],
           scanned:r.scanned};
  }
 }
 return null;
}
function wireDrop(id, idle, onPaths){
 const dz=document.getElementById(id); if(!dz)return;
 const stop=e=>{e.preventDefault();e.stopPropagation()};
 ['dragenter','dragover'].forEach(ev=>dz.addEventListener(ev,e=>{
   stop(e);dz.classList.add('over');dz.classList.remove('bad')}));
 ['dragleave','dragend'].forEach(ev=>dz.addEventListener(ev,e=>{
   stop(e);dz.classList.remove('over')}));
 dz.addEventListener('drop',async e=>{
  stop(e); dz.classList.remove('over');
  const dt=e.dataTransfer;
  let paths=dropPaths(dt);
  if(!paths.length){
   dz.innerHTML='Working out where that folder is\u2026';
   const found=await locateByName(dt);
   if(found&&found.matches.length===1){paths=[found.matches[0]]}
   else if(found&&found.matches.length>1){
    dz.classList.add('bad');
    dz.innerHTML='Found several folders called <b>'+found.name+'</b>. '+
     'Pick the right one below:<br>'+found.matches.map(m=>
      '<button class="small" style="margin:4px 3px 0 0" onclick="pickFound(\''+
      dz.id+'\',\''+m.replace(/'/g,"\\'")+'\')">'+m+'</button>').join('');
    return}
   else{
    dz.classList.add('bad');
    const nm=found&&found.name?('<b>'+esc(found.name)+'</b>'):'that folder';
    const where=(found&&found.searched&&found.searched.length)
      ?'<br>Looked in: '+found.searched.map(esc).join(', '):'';
    dz.innerHTML='Could not find '+nm+' on this computer. '+
     'Use <b>+ choose folders</b> below instead \u2014 that always works.'+where+
     '<div class="sub" style="margin-top:8px;font-size:11px;word-break:break-all;'+
     'user-select:all;cursor:text">'+esc(dropDebug(dt))+'</div>'+
     '<div class="sub" style="font-size:11px;margin-top:4px">'+
     '(the grey line is diagnostic detail \u2014 select and copy it if reporting this)</div>';
    return}
  }
  dz.innerHTML=await onPaths(paths)||idle;
 });
 DROPFN[id]=onPaths;
}
const DROPFN={};
async function pickFound(zone, path){
 const dz=document.getElementById(zone);
 dz.classList.remove('bad');
 dz.innerHTML=await DROPFN[zone]([path])||'done';
}
function initDrop(){
 wireDrop('drop','Drag folders here from Finder or Explorer', async paths=>{
  const r=await post('/api/dropped',{paths});
  sources=r.sources; draw();
  return (r.added.length?'Added '+r.added.length+' folder(s). ':'')+
   (r.bad.length?'<span style="color:var(--warn)">Could not read: '+
     r.bad.join(', ')+'</span>':'Drag more here, or use the picker below.')});

 wireDrop('dropout','Drag a folder here to put the merged copies inside it',
  async paths=>{
   const r=await post('/api/resolve',{path:paths[0]});
   if(!r.dir)return '<span style="color:var(--warn)">Could not read that location</span>';
   const n=(document.getElementById('newname').value||'merged photos').trim();
   const jd=await post('/api/joindest',{dir:r.dir,name:n}); drawdest(jd.dest);
   return 'Destination set inside <b>'+esc(r.name||r.dir)+'</b>. '+
    'Rename the new folder below if you like.'});
}

async function scan(){
 // clear what the last scan produced before the new one starts
 S={groups:[],decisions:{},counts:{}};
 document.getElementById('pairs').innerHTML='';
 document.getElementById('stats').innerHTML='';
 document.getElementById('scanmsg').textContent='starting\u2026';
 document.getElementById('reviewstep').style.display='none';
 document.getElementById('planstep').style.display='none';
 document.getElementById('gostep').style.display='none';
 document.getElementById('mergemsg').textContent='';
 document.getElementById('barwrap').style.display='block';
 document.getElementById('bar').style.width='0';
 document.getElementById('scanbtn').disabled=true;
 await post('/api/scan'); poll()}

function secs(n){if(n==null)return '';
 return n<60?Math.round(n)+'s':Math.floor(n/60)+'m '+Math.round(n%60)+'s'}
function drawStats(c,live){
 // during a scan, only show what has actually been measured
 const rows=live
  ?[['examined',(c.examined||0)+' / '+(c.total||0)],['dated',c.dated],
    ['no date',c.undated],['received',c.received],['with gps',c.gps]]
  :[['files',c.total],['images',c.images],['videos',c.videos],['dated',c.dated],
    ['no date',c.undated],['received',c.received],['with gps',c.gps],
    ['identical',c.exact_dups],['groups',c.groups],['in groups',c.in_groups]];
 document.getElementById('stats').innerHTML=rows
  .map(([k,v])=>'<div class="stat"><b>'+(v==null?0:v)+'</b><span>'+k+'</span></div>').join('');
}
async function poll(){S=await j('/api/state');
 const s=S.scan;
 document.getElementById('bar').style.width=s.pct+'%';
 let msg=s.error?('Error: '+s.error):s.step;
 if(s.running&&s.elapsed!=null){
  msg+='  \u00b7 '+secs(s.elapsed)+' elapsed';
  if(s.eta!=null&&s.eta>1)msg+=', about '+secs(s.eta)+' left';
 } else if(s.done&&s.elapsed!=null){msg+='  \u00b7 took '+secs(s.elapsed)}
 document.getElementById('scanmsg').textContent=msg;
 if(S.counts)drawStats(S.counts,s.running);
 if(s.running){setTimeout(poll,400);return}
 document.getElementById('scanbtn').disabled=false;
 if(!s.done)return;
 renderGroups(); renderPlan();
 document.getElementById('gostep').style.display='block';
 const de2=document.getElementById('destecho'); if(de2)de2.textContent=S.dest;
}

function esc(s){return s.replace(/&/g,'&amp;').replace(/</g,'&lt;')}
function renderGroups(){
 const box=document.getElementById('pairs'); box.innerHTML='';
 const G=S.groups||[];
 document.getElementById('reviewstep').style.display=G.length?'block':'none';
 const n=G.reduce((a,g)=>a+g.members.length,0);
 document.getElementById('revsub').textContent=
  G.length+' group(s) covering '+n+' photos. Everything is kept unless you say otherwise.';
 G.forEach(g=>{
  const d=S.decisions[g.id]||{keep:g.members,not_match:false};
  // Only ever trust members of THIS group. A stale decision left over from a
  // previous scan could otherwise report more kept photos than exist.
  const raw=d.not_match?g.members:(d.keep||g.members);
  const keep=new Set(g.members.filter(m=>raw.indexOf(m)>=0));
  const div=document.createElement('div'); div.className='card';
  const cls={'crop':'crop','burst':'burst','mixed':'mixed'}[g.kind]||'same';
  const thumbs=g.members.map(m=>{
   const bx=g.boxes[m]?('&box='+g.boxes[m].join(',')):'';
   const on=keep.has(m);
   // Inline styles rather than a CSS class: this is the signal the whole
   // review depends on, so it must not be defeated by the cascade.
   const imgStyle=on
     ? 'opacity:1;filter:none'
     : 'opacity:.25;filter:grayscale(1)';
   const wrapStyle=on
     ? 'border:2px solid #2f6f4f;background:#f2f8f4'
     : 'border:2px dashed #c9c9c2;background:#f0f0ec';
   const click=d.not_match?''
     :' onclick="toggle(\''+g.id+'\',\''+m.replace(/'/g,"\\'")+'\')"';
   return '<figure class="member'+(d.not_match?'':' clickable')+'" style="'+wrapStyle+
     ';border-radius:7px;padding:7px;position:relative"'+click+' title="'+
     (on?'Click to skip this photo':'Click to keep this photo')+'">'+
    '<img style="'+imgStyle+'" src="/api/thumb?p='+encodeURIComponent(m)+bx+'">'+
    (on?'':'<div class="skipmark">SKIPPED</div>')+
    '<figcaption style="'+(on?'':'text-decoration:line-through;color:#999')+'">'+
     esc((g.names&&g.names[m])||m)+'</figcaption>'+
    '<button class="small toggle '+(on?'on':'off')+'" '+
     (d.not_match?'disabled ':'')+
     'onclick="event.stopPropagation();toggle(\''+g.id+'\',\''+
     m.replace(/'/g,"\\'")+'\')">'+
     (on?'\u2713 keeping \u2014 click to skip':'\u2717 skipped \u2014 click to keep')+
     '</button></figure>'}).join('');
  div.innerHTML=
   '<div class="hd"><span class="badge '+cls+'">'+g.kind+' &middot; '+
    g.members.length+' photos</span>'+
    (g.crowded?'<span class="badge crop" style="margin-left:6px">'+
     'unusually large \u2014 check these are really related</span>':'')+
    '<span class="sub">match '+g.score+'</span></div>'+
   '<div class="imgs">'+thumbs+'</div>'+
   '<div class="rels">'+g.rels.map(r=>'&bull; '+esc(r)).join('<br>')+'</div>'+
   '<div class="choices">'+
    '<button onclick="bulk(\''+g.id+'\',\'all\')">keep all</button>'+
    '<button onclick="bulk(\''+g.id+'\',\'none\')">keep none</button>'+
    '<button class="'+(d.not_match?'on':'')+
     '" onclick="bulk(\''+g.id+'\',\'notmatch\')">not a match</button>'+
    '<span class="sub" style="margin-left:6px">'+
     (d.not_match?'treated as unrelated \u2014 all kept':
      keep.size+' of '+g.members.length+' kept')+'</span></div>';
  box.appendChild(div)})
}
function grp(id){return S.groups.find(g=>g.id===id)}
async function send(id,keep,nm){await post('/api/decide',{id,keep,not_match:nm});
 S.decisions[id]={keep,not_match:nm}; renderGroups(); renderPlan()}
function toggle(id,m){const g=grp(id); if(!g)return;
 const d=S.decisions[id]||{keep:g.members,not_match:false};
 if(d.not_match)return;
 const raw=d.keep||g.members;
 const k=new Set(g.members.filter(x=>raw.indexOf(x)>=0));   // clamp to this group
 k.has(m)?k.delete(m):k.add(m);
 send(id,[...k],false)}
function bulk(id,what){const g=grp(id); if(!g)return;
 if(what==='all')send(id,g.members.slice(),false);
 else if(what==='none')send(id,[],false);
 else{const d=S.decisions[id]||{};send(id,g.members.slice(),!d.not_match)}}

async function renderPlan(){const d=await j('/api/plan');
 document.getElementById('planstep').style.display='block';
 document.getElementById('plansub').textContent=
  d.copy+' files will be copied, '+d.skip+' skipped.';
 document.getElementById('plan').innerHTML='<tr><th>from</th><th>to</th></tr>'+
  d.rows.map(r=>'<tr><td class="old">'+esc(r.name||r.path)+'</td><td>'+
   (r.skip?'<span class="skip">skipped &mdash; '+r.skip+'</span>':
    (r.sub?r.sub+'/':'')+r.new)+'</td></tr>').join('')}

async function merge(){document.getElementById('gobtn').disabled=true;
 await post('/api/merge'); mpoll()}
async function mpoll(){const s=await j('/api/state');
 const m=s.merge;
 document.getElementById('mergemsg').textContent=
  m.running?('copying... '+m.copied):
  (m.done?('Done. '+m.copied+' copied, '+m.skipped+' skipped. '+m.log.join(' ')):
   m.log.join(' '));
 if(m.running){setTimeout(mpoll,400);return}
 document.getElementById('gobtn').disabled=false}

// on load, adopt whatever the server already knows
(async()=>{
 B.src.go(''); B.dst.go(''); loadRoots();
 const s=await j('/api/state');
 sources=s.sources||[]; draw();
 document.getElementById('o_crops').checked=s.options.find_crops;
 document.getElementById('o_recv').checked=s.options.received==='subfolder';
 document.getElementById('o_vid').checked=s.options.video==='subfolder';
 document.getElementById('o_burst').checked=s.options.find_bursts;
 document.getElementById('o_bsec').value=s.options.burst_seconds;
 document.getElementById('o_bm').value=s.options.burst_metres;
 drawdest(s.dest);
 initDrop();
 if(s.scan.done){document.getElementById('barwrap').style.display='block';poll()}
})();
</script></body></html>"""


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def already_running(port):
    """Is OUR app on this port, as opposed to something unrelated?"""
    try:
        import urllib.request
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/roots", timeout=1.5) as r:
            return json.loads(r.read()).get("version") is not None
    except Exception:
        return False


def start_server():
    """Bind a port, but don't quietly start a duplicate.

    Double-clicking the launcher twice should bring you back to the copy that
    is already running, not spawn another one on a different port and leave
    you wondering which browser tab is real.
    """
    last = None
    for port in range(PORT, PORT + 10):
        try:
            return Server(("127.0.0.1", port), Handler), port
        except OSError as e:
            last = e
            if already_running(port):
                url = f"http://localhost:{port}/"
                print()
                print("  " + "=" * 58)
                print("   Fantastic Photos is already running.")
                print()
                print(f"       {url}")
                print()
                print("   Reopening that in your browser. Nothing new was started.")
                print("  " + "=" * 58)
                print()
                webbrowser.open(url)
                raise SystemExit(0)
    raise SystemExit(f"Could not find a free port between {PORT} and {PORT + 9}: {last}")


def banner(url, port):
    # Plain ASCII: box-drawing characters turn into mojibake in older Windows
    # consoles, and this is the one message that has to be readable.
    line = "=" * 58
    print()
    print("  " + line)
    print("   Fantastic Photos is running.")
    print()
    print("   Open this address in your browser:")
    print()
    print(f"       {url}")
    print()
    if port != PORT:
        print(f"   (port {PORT} was busy, so this copy is on {port})")
        print()
    print("   Keep this window open while you use it.")
    print("   Press Ctrl+C here, or just close the window, to stop.")
    print("  " + line)
    print()


if __name__ == "__main__":
    STATE["sources"] = []
    srv, port = start_server()
    url = f"http://localhost:{port}/"
    banner(url, port)
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped. Close this window, or run it again to restart.")
        print(f"  (it was at {url})\n")
