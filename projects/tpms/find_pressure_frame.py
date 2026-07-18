#!/usr/bin/env python3
"""Correlate every C-CAN broadcast slice against the 4 known per-wheel pressure curves (numpy)."""
import csv, re, sys, datetime, collections
import numpy as np

SNIFF, A, B = sys.argv[1], sys.argv[2], sys.argv[3]
fmt = "%Y-%m-%d %H:%M:%S"
t_a = datetime.datetime.strptime(A, fmt).timestamp()
t_b = datetime.datetime.strptime(B, fmt).timestamp()
W = ["FL", "FR", "RR", "RL"]

truth = {}
tt = None
rows = [r for r in csv.DictReader(open("tmp/tpms/tpms_drive_log.csv"))]
tg = []
cols = {w: [] for w in W}
for r in rows:
    t = datetime.datetime.strptime(r["time"], fmt).timestamp()
    if t_a <= t <= t_b and all(r[f"psi_{w}"] for w in W):
        tg.append(t)
        for w in W:
            cols[w].append(float(r[f"psi_{w}"]))
tg = np.array(tg)
for w in W:
    truth[w] = np.array(cols[w])

pat = re.compile(r"\(([\d.]+)\) \w+ ([0-9A-Fa-f]+)#([0-9A-Fa-f]*)")
frames = collections.defaultdict(list)
for ln in open(SNIFF, errors="ignore"):
    m = pat.match(ln)
    if not m:
        continue
    t = float(m.group(1))
    if t_a <= t <= t_b:
        frames[int(m.group(2), 16)].append((t, bytes.fromhex(m.group(3))))

def corr_to_wheels(st, sv):
    """interpolate slice (st,sv) onto logger grid, return {wheel:r}."""
    xi = np.interp(tg, st, sv)
    if xi.std() == 0:
        return {}
    out = {}
    for w in W:
        y = truth[w]
        r = np.corrcoef(xi, y)[0, 1]
        out[w] = r
    return out

results = []
for cid, fl in frames.items():
    fl.sort()
    st = np.array([t for t, _ in fl])
    L = max((len(d) for _, d in fl), default=0)
    if len(fl) < 10:
        continue
    for i in range(L):
        col = np.array([d[i] if len(d) > i else np.nan for _, d in fl], float)
        if np.unique(col[~np.isnan(col)]).size < 4:
            continue
        for nm, val in ((f"b{i}", col),):
            cw = corr_to_wheels(st, val)
            for w, r in cw.items():
                if r > 0.85:
                    results.append((r, cid, nm, w))
    for i in range(L-1):
        be = np.array([(d[i] << 8) | d[i+1] if len(d) > i+1 else np.nan for _, d in fl], float)
        le = np.array([d[i] | (d[i+1] << 8) if len(d) > i+1 else np.nan for _, d in fl], float)
        for nm, val in ((f"BE{i}", be), (f"LE{i}", le)):
            if np.unique(val[~np.isnan(val)]).size < 4:
                continue
            cw = corr_to_wheels(st, val)
            for w, r in cw.items():
                if r > 0.85:
                    results.append((r, cid, nm, w))

results.sort(reverse=True)
print(f"# slices with r>0.85 vs a wheel curve  (sniff={SNIFF.split('/')[-1]}, {len(tg)} grid pts)")
print(f"{'r':>6}  {'ID':>5} {'slice':>5}  wheel")
seen = set()
for r, cid, nm, w in results:
    if (cid, nm) in seen:
        continue
    seen.add((cid, nm))
    print(f"{r:6.3f}  0x{cid:03X} {nm:>5}  {w}")
    if len(seen) >= 45:
        break

print("\n# frames whose 4 best fields each track a DISTINCT wheel (slot-map candidate):")
bywheel = collections.defaultdict(dict)
for r, cid, nm, w in results:
    if w not in bywheel[cid] or r > bywheel[cid][w][0]:
        bywheel[cid][w] = (r, nm)
for cid, wm in sorted(bywheel.items()):
    if len(wm) == 4:
        print(f"  0x{cid:03X}  " + "  ".join(f"{w}:{wm[w][1]}({wm[w][0]:.2f})" for w in W))
