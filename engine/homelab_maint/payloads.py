"""JSON payloads behind the Homarr v2 custom widgets (ops-overview, ops-disk, ops-jobs, ops-guard, ops-reclaim).

Everything here is a pure function of the status dict (STATE_DIR/status.json as written by cli.py) and `now`.
Design rules, all driven by the Homarr v2 custom-widget runtime (widgets/CONVENTIONS.md):
  * small: every payload is < 4 KB (fit() enforces it), because the whole upstream JSON is shipped to the
    browser on every widget poll;
  * precomputed: human sizes, ages ("3m", "5h"), percentages and Mantine colour names ("teal" ok, "yellow" warn,
    "red" crit, "orange" the check itself failed, "gray" neutral/stale), because templates should not do date or
    number formatting (the runtime only offers a few static Date helpers and throws on an invalid input);
  * flat: templates read `data.state.<key>` (property reads are null-safe, METHOD calls such as .map are not, so the
    templates guard every list with `(x||[])`), hence payloads expose top-level scalars and lists of complete rows.
    Every key is present even with no data (empty lists, "" strings); trouble adds `error` + `stale: true`.

Metric/item keys read from the task modules (all optional; a missing key falls back to the task's status/summary):
  disk_forecast.metrics   mounts[{mount,free_h|free,used_pct,days,level,info}], root_free_h, root_days
  docker_df.metrics       images_h, build_cache_h, volumes_h, safe_reclaim_h (also *_gib / *_bytes)
  backup_freshness        metrics.backups [{name,age_h,result,level}]
  smart_trend             metrics.devices [{dev,temp_c,realloc,pending,level,note}]
  growth_watch            metrics.paths|items [{path,rate,level}]
  memory_health.metrics   mem_available_h, swap_used_h, psi_mem_full60, psi_io_some60, swap_in_pps, oom_kills_delta
  spike_sampler.metrics   containers, anon_total_h, largest [{name,anon_h}]
  stuck_detector          metrics.actionable|candidates; items [{name,anon_h,reason,protected,busy}]
  C1 cleaners             metrics.mode ("report"|"apply") + selected, selected_h (what a report-mode run would free)
  C2 plans                entry.plan {items:[{name,bytes,needs_manual_check}], total_bytes}, entry.plan_hash
Anything else (alert_path_health, orphan_report, image_ledger, plex_media_mount_check, ...) is shown through
status + summary only. Summaries start with "ok: " / "warn: " in the tasks; the prefix is dropped for display.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .core import GIB, STATE_DIR, human, read_json

STALE_S = 45 * 60                  # status older than this is "stale" (3 missed 15-min check runs)
MAX_BYTES = 3900                   # hard budget per payload (spec: < 4 KB)
TIER_STALE_S = {"check": 45 * 60, "daily": 30 * 3600, "weekly": 9 * 86400, "monthly": 40 * 86400}   # monthly: the 1st-Saturday window
TIER_ORDER = {"check": 0, "daily": 1, "weekly": 2, "monthly": 3}

SEV = {"ok": 0, "skipped": 0, "info": 0, "warn": 1, "crit": 2, "error": 2}
COLOR = {"ok": "teal", "skipped": "gray", "info": "gray", "warn": "yellow", "crit": "red", "error": "orange"}
RANK = {"crit": 4, "error": 3, "warn": 2, "info": 1, "ok": 0, "skipped": 0}   # issue ordering
GOOD = {"ok", "success", "succeeded", "0", "true", "done"}                    # backup result words


# --------------------------------------------------------------------------- small helpers
def _num(v: Any, default: float | None = None) -> float | None:
    if isinstance(v, bool):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _clean(s: str) -> str:
    """Replace lone surrogates with "?". They are valid JSON (\\udcff) and come from surrogateescape file names that
    tasks copy into labels; left in, they make every UTF-8 encode of the payload (fit(), the HTTP body) raise."""
    return s.encode("utf-8", "replace").decode("utf-8")


def _t(s: Any, n: int) -> str:
    """Single-line, control-free, surrogate-free, truncated text."""
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", _clean("" if s is None else str(s))).strip()
    return s if len(s) <= n else s[: max(n - 2, 1)] + ".."


def _sum(e: dict | None, n: int) -> str:
    """Task summary for display: the "warn: " style level prefix the tasks add is redundant next to a colour."""
    return _t(re.sub(r"^(ok|info|warn|crit|error|skipped):\s*", "", str((e or {}).get("summary") or "")), n)


_UNITS = {"B": 1, "KIB": 1024, "MIB": 1024 ** 2, "GIB": GIB, "TIB": 1024 ** 4}


def _parse_h(s: Any) -> float:
    """"14.9 GiB" -> bytes (0 when it is not a human size)."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?i?B)\s*", str(s or ""), re.I)
    return float(m.group(1)) * _UNITS.get(m.group(2).upper(), 0) if m else 0.0


def _short_path(p: Any, n: int = 22) -> str:
    p = _clean(str(p or ""))
    return p if len(p) <= n else ".." + p[-(n - 2):]


def _age_txt(sec: float | None) -> str:
    if sec is None:
        return "never"
    m = int(max(sec, 0) // 60)
    if m < 90:
        return f"{m}m"
    h = m // 60
    return f"{h}h" if h < 48 else f"{h // 24}d"


def _days_txt(d: Any) -> str:
    d = _num(d)
    if d is None or d < 0:
        return ""
    if d >= 365:
        return ">1y"
    return "<1d" if d < 1 else f"{d:.0f}d"


def _hb(n: Any) -> str:
    n = _num(n)
    return "" if n is None else human(n)


def _hs(m: dict, *bases: str) -> str:
    """Human size for `<base>_h`, `<base>_gib` or `<base>_bytes` in a metrics dict, first base that exists ("" if none)."""
    for base in bases:
        v = m.get(f"{base}_h")
        if isinstance(v, str) and v:
            return _t(v, 14)
        g = _num(m.get(f"{base}_gib"))
        if g is not None:
            return human(g * GIB)
        if _num(m.get(f"{base}_bytes")) is not None:
            return _hb(m.get(f"{base}_bytes"))
    return ""


def _tasks(status: dict) -> dict:
    t = status.get("tasks") if isinstance(status, dict) else None
    return {k: v for k, v in t.items() if isinstance(v, dict)} if isinstance(t, dict) else {}


def _m(e: dict | None) -> dict:
    m = (e or {}).get("metrics")
    return m if isinstance(m, dict) else {}


def _rows(e: dict | None, *keys: str) -> list[dict]:
    """Row list from metrics[key] (first key that holds a list of dicts), else the entry's items."""
    for k in keys:
        v = _m(e).get(k)
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            return v
    items = (e or {}).get("items")
    return [x for x in items if isinstance(x, dict)] if isinstance(items, list) else []


def _acked_now(e: dict | None) -> bool:
    """SPEC5: status.json marked this entry `acked` and the acknowledgement is still in force (acks.flag_live: a finite `until` in the future)."""
    try:
        from . import acks
        return acks.flag_live((e or {}).get("acked"))
    except Exception:  # noqa: BLE001
        return False


def _lvl(e: dict | None) -> str:
    """Display level of one task entry; findings flagged alert=False, or acknowledged by the owner, never look like pages."""
    s = (e or {}).get("status", "ok")
    s = s if s in SEV else "info"
    return "info" if s in ("warn", "crit") and ((e or {}).get("alert") is False or _acked_now(e)) else s


def _lv(v: Any, default: str) -> str:
    """A level word from a metrics row, tolerating junk (unhashable or unknown values fall back to `default`)."""
    return v if isinstance(v, str) and v in COLOR else default


def _col(e: dict | None) -> str:
    return COLOR[_lvl(e)] if e else "gray"


def _task_stale(e: dict, now: float) -> bool:
    last = _num(e.get("last_run"))
    return last is None or now - last > TIER_STALE_S.get(e.get("tier", ""), 24 * 3600)


def _mode(e: dict) -> str:
    return {"apply": "apply", "dry-run": "report", "check": "check"}.get(e.get("mode", ""), "check")


def _overall(status: dict) -> str:
    o = status.get("overall")
    if o in ("ok", "warn", "crit"):
        return o
    worst = max((SEV[_lvl(e)] for e in _tasks(status).values() if e.get("alert", True)), default=0)
    return ("ok", "warn", "crit")[worst]


# --------------------------------------------------------------------------- common envelope
def _base(status: dict, now: float | None) -> tuple[dict, float]:
    now = time.time() if now is None else float(now)
    gen = _num(status.get("generated_at")) if isinstance(status, dict) else None
    if not gen:
        return {"error": "no status yet", "stale": True, "age_min": None, "ago": "never", "level": "none", "c": "gray",
                "paused": False, "host": ""}, now
    age = max(0.0, now - gen)
    stale = age > STALE_S
    level = _overall(status)
    return {"stale": stale, "age_min": int(age // 60), "ago": _age_txt(age), "level": level,
            "c": "gray" if stale else COLOR[level], "paused": bool(status.get("paused")),
            "host": _t(status.get("host"), 24)}, now


def fit(payload: dict, limit: int = MAX_BYTES) -> dict:
    """Trim the longest list (then the longest string) until the compact UTF-8 JSON fits `limit` bytes."""
    def size() -> int:
        # errors="replace": a stray surrogate in a pass-through field must not make the size probe itself raise
        return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "replace"))
    guard = 0
    while size() > limit and guard < 500:
        guard += 1
        lists = [(len(v), k) for k, v in payload.items() if isinstance(v, list) and len(v) > 1]
        if lists:
            payload[max(lists)[1]].pop()
            payload["cut"] = True
            continue
        strs = [(len(v), k) for k, v in payload.items() if isinstance(v, str) and len(v) > 8]
        if not strs:
            break
        k = max(strs)[1]
        payload[k] = _t(payload[k], len(payload[k]) // 2)
        payload["cut"] = True
    return payload


# --------------------------------------------------------------------------- shared blocks
def _tiers(status: dict, now: float) -> list[dict]:
    runs = status.get("tier_runs") if isinstance(status.get("tier_runs"), dict) else {}
    out = []
    for tier in ("check", "daily", "weekly"):
        r = runs.get(tier) if isinstance(runs.get(tier), dict) else {}
        last = _num(r.get("last_run"))
        age = None if last is None else now - last
        late = age is None or age > TIER_STALE_S[tier]
        out.append({"n": tier, "a": _age_txt(age), "c": "orange" if late else "teal",
                    "d": bool(r.get("dry_run")) if last is not None else False})
    return out


def _apply_counts(status: dict) -> tuple[int, int]:
    cleaners = [e for e in _tasks(status).values() if e.get("klass") in ("C1", "C2")]
    return sum(1 for e in cleaners if e.get("mode") == "apply"), len(cleaners)


def _reclaimed(status: dict, now: float) -> dict:
    log = [r for r in (status.get("reclaimed_log") or []) if isinstance(r, dict)]
    log = [r for r in log if _num(r.get("t")) is not None and _num(r.get("bytes"), 0) > 0]
    tot = {"d1": 0.0, "d7": 0.0, "d30": 0.0, "d90": 0.0}
    by: dict[str, float] = {}
    for r in log:
        age, b = now - float(r["t"]), float(r["bytes"])
        for k, lim in (("d1", 1), ("d7", 7), ("d30", 30), ("d90", 90)):
            if age <= lim * 86400:
                tot[k] += b
        if age <= 30 * 86400:
            by[str(r.get("task"))] = by.get(str(r.get("task")), 0.0) + b
    return {"tot": tot, "by": by, "log": sorted(log, key=lambda r: -float(r["t"]))}


def _title(status: dict, name: str) -> str:
    return _t((_tasks(status).get(name) or {}).get("title") or name, 24)


# --------------------------------------------------------------------------- overview
def overview(status: dict, now: float | None = None) -> dict:
    p, now = _base(status, now)
    tasks = _tasks(status)
    n = {"ok": 0, "info": 0, "warn": 0, "crit": 0, "err": 0}   # err = the check itself failed to run
    bad = []
    for name, e in tasks.items():
        lv = _lvl(e)
        n["err" if lv == "error" else "ok" if lv in ("ok", "skipped") else lv] += 1
        if lv in ("warn", "crit", "error"):
            bad.append((-RANK[lv], name, e, lv))
    bad.sort(key=lambda x: x[:2])
    issues = [{"n": _t(e.get("title") or name, 22), "s": _sum(e, 72), "c": COLOR[lv]}
              for _r, name, e, lv in bad[:6]]
    head = ", ".join(f"{n[k]} {k}" for k in ("crit", "err", "warn") if n[k]) or ("all clear" if tasks else "no data")

    df = tasks.get("disk_forecast")
    dm = _m(df)
    mem = tasks.get("memory_health")
    mm = _m(mem)
    rec = _reclaimed(status, now)["tot"]
    psi = _num(mm.get("psi_mem_full60"))
    tiles = [
        {"l": "Root free", "v": _t(dm.get("root_free_h"), 12) or "--",
         "s": ("full in " + _days_txt(dm.get("root_days"))) if _days_txt(dm.get("root_days")) else "no forecast yet",
         "c": _col(df)},
        {"l": "Memory", "v": _t(mm.get("mem_available_h"), 12) or "--",
         "s": f"psi {psi:.1f}%" if psi is not None else "available", "c": _col(mem)},
        {"l": "Freed 24h", "v": human(rec["d1"]), "s": "7d " + human(rec["d7"]), "c": "teal" if rec["d1"] else "gray"},
    ]
    on, tot = _apply_counts(status)
    sub = f"{len(tasks)} checks: " + ", ".join(f"{n[k]} {k}" for k in ("ok", "info", "warn", "crit", "err") if n[k])
    p.update(head=head, sub=sub if tasks else "", issues=issues, tiles=tiles, tiers=_tiers(status, now),
             ap_on=on, ap_all=tot)
    return fit(p)


# --------------------------------------------------------------------------- disk
def _mount_row(r: dict) -> dict:
    free = _t(r.get("free_h"), 12) or _hb(r.get("free"))
    lv = _lv(r.get("level"), "ok")
    info = bool(r.get("info_only") or r.get("info")) or lv == "info"
    c = "gray" if info else COLOR[lv]
    pct = _num(r.get("used_pct"), 0.0)
    return {"m": _short_path(r.get("mount") or r.get("path")), "f": free, "p": int(round(min(max(pct, 0), 100))),
            "d": _days_txt(r.get("days")), "c": c, "i": info, "_s": SEV[lv]}


def _top(rows: list[dict], n: int) -> list[dict]:
    """First `n` rows of a list already sorted worst-first, but never fewer than all rows with severity (`_s`) > 0.
    Strips the private sort keys (`_s`, `_a`)."""
    keep = max(n, sum(1 for r in rows if r["_s"] > 0))
    return [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows[:keep]]


def _rate_val(r: dict) -> float:
    """Growth rate in GiB/day for ranking: the task's numeric fields when present, else the number in "+0.08 GiB/d"."""
    for k in ("peak", "gib_day"):
        v = _num(r.get(k))
        if v is not None:
            return v
    m = re.match(r"\s*([-+]?\d+(?:\.\d+)?)", str(r.get("rate") or ""))
    return float(m.group(1)) if m else float("-inf")


def disk(status: dict, now: float | None = None) -> dict:
    p, now = _base(status, now)
    t = _tasks(status)
    df = t.get("disk_forecast")
    mounts = sorted((_mount_row(r) for r in _rows(df, "mounts")), key=lambda r: (r["i"], -r["_s"], -r["p"]))
    shown = mounts[:7]
    for r in mounts:
        r.pop("_s")
    rf, rd = _t(_m(df).get("root_free_h"), 12), _days_txt(_m(df).get("root_days"))
    p.update(head=(f"/ {rf} free" + (f", full {rd}" if rd else "")) if rf else "no disk data",
             mounts=shown, more=max(len(mounts) - len(shown), 0), dc=_col(df))

    dm, dd = _m(t.get("docker_df")), t.get("docker_df")
    p["dk"] = [{"l": lbl, "v": _hs(dm, *bases) or "--"} for lbl, bases in
               (("images", ("images",)), ("cache", ("build_cache",)), ("volumes", ("volumes",)),
                ("reclaimable", ("safe_reclaim", "reclaimable")))]
    p["dkc"] = _col(dd)

    # The three lists below are RANKED BY SEVERITY FIRST and cut afterwards, and every problem row survives the cut:
    # a failing item (cool disk with pending sectors, 4th backup failed, journal runaway) must never be hidden
    # behind healthy ones that merely sort earlier. fit() trims from the tail, i.e. the least severe rows go first.
    bk = []
    for r in _rows(t.get("backup_freshness"), "backups"):
        age = _num(r.get("age_h"))
        bad = str(r.get("result", "ok")).lower() not in GOOD
        lv = _lv(r.get("level"), "ok")
        bk.append({"n": _t(str(r.get("name", "?")).replace("backup-", ""), 14),
                   "a": _age_txt(None if age is None else age * 3600),
                   "c": "red" if bad else COLOR[lv],   # row.level (set by the task) = age verdict
                   "_s": 2 if bad else SEV[lv], "_a": age or 0.0})
    bk.sort(key=lambda r: (-r["_s"], -r["_a"]))
    p["bk"] = _top(bk, 3)

    sm = []
    for r in _rows(t.get("smart_trend"), "devices"):
        tc = _num(r.get("temp_c"))
        pend, real = _num(r.get("pending"), 0), _num(r.get("realloc"), 0)
        flag = _t(r.get("note"), 18) or (f"pending {pend:.0f}" if pend else f"realloc {real:.0f}" if real else "")
        lv = _lv(r.get("level"), "warn" if pend else "ok")
        sm.append({"n": _short_path(r.get("dev"), 12), "t": "?" if tc is None else f"{tc:.0f}C", "x": flag,
                   "c": COLOR[lv], "_s": SEV[lv], "_a": tc or 0.0})
    sm.sort(key=lambda r: (-r["_s"], -r["_a"]))          # problems first, then hottest
    p["sm"] = _top(sm, 5)

    gr = []
    for r in _rows(t.get("growth_watch"), "paths"):
        rate, lv = _t(r.get("rate"), 14), _lv(r.get("level"), "ok")
        if not rate or (rate == "n/a" and SEV[lv] == 0):  # never measured and nothing wrong: nothing to show
            continue
        gr.append({"p": _short_path(r.get("path"), 26), "r": "unmeasured" if rate == "n/a" else rate,
                   "c": COLOR[lv] if SEV[lv] else "gray", "_s": SEV[lv], "_a": _rate_val(r)})
    gr.sort(key=lambda r: (-r["_s"], -r["_a"]))          # warnings first, then the fastest growers
    p["gr"] = _top(gr, 3)

    px = t.get("plex_media_mount_check")
    p["plex"], p["plexc"] = _sum(px, 70), _col(px)
    return fit(p)


# --------------------------------------------------------------------------- jobs
def jobs(status: dict, now: float | None = None) -> dict:
    p, now = _base(status, now)
    rows, fail = [], 0
    tasks = _tasks(status)
    for name, e in tasks.items():
        lv, stale = _lvl(e), _task_stale(e, now)
        problem = lv == "error" or stale
        fail += problem
        if e.get("klass") == "C0" and not problem:
            continue
        last = _num(e.get("last_run"))
        rows.append(((0 if problem else 1, TIER_ORDER.get(e.get("tier"), 9), e.get("mode") != "apply", e.get("klass", ""), name), {
            "n": _t(e.get("title") or name, 22), "k": e.get("klass", "?"), "t": e.get("tier", "?"), "m": _mode(e),
            "c": "gray" if stale and lv != "error" else COLOR[lv], "s": _sum(e, 56),
            "f": _hb(e.get("reclaimed_bytes")) if _num(e.get("reclaimed_bytes"), 0) else "",
            "a": _age_txt(None if last is None else now - last), "x": stale}))
    rows.sort(key=lambda x: x[0])
    on, tot = _apply_counts(status)
    # nt = number of task entries: the template says "no cleanup jobs have run yet" only when there is real data
    p.update(head=f"apply {on}/{tot}" if tot else "no cleaners" if tasks else "no data", rows=[r for _k, r in rows[:10]],
             fail=fail, tiers=_tiers(status, now), ap_on=on, ap_all=tot, nt=len(tasks))
    return fit(p)


# --------------------------------------------------------------------------- guard
def _named_size(r: Any) -> dict:
    """Accept {name,anon_h|anon_gib} or [name, gib] rows."""
    if isinstance(r, (list, tuple)) and len(r) >= 2:
        g = _num(r[1])
        return {"n": _short_path(r[0], 24), "h": "" if g is None else human(g * GIB)}
    if isinstance(r, dict):
        return {"n": _short_path(r.get("name") or r.get("container"), 24), "h": _hs(r, "anon") or _hs(r, "mem")}
    return {"n": "?", "h": ""}


def guard(status: dict, now: float | None = None) -> dict:
    p, now = _base(status, now)
    t = _tasks(status)
    mh = t.get("memory_health")
    mm = _m(mh)
    pf, pi, si = _num(mm.get("psi_mem_full60")), _num(mm.get("psi_io_some60")), _num(mm.get("swap_in_pps"))
    oom = _num(mm.get("oom_kills_delta"))
    p["mem"] = [
        {"l": "available", "v": _t(mm.get("mem_available_h"), 12) or "--"},
        {"l": "swap used", "v": _t(mm.get("swap_used_h"), 12) or "--"},
        {"l": "mem psi", "v": "--" if pf is None else f"{pf:.1f}%"},
        {"l": "io psi", "v": "--" if pi is None else f"{pi:.1f}%"},
        {"l": "swap in", "v": "--" if si is None else f"{si:.0f}/s"},
        {"l": "oom kills", "v": "--" if oom is None else f"{oom:.0f}"},
    ]
    p["mc"], p["mn"] = _col(mh), _sum(mh, 110)

    sd = t.get("stuck_detector")
    sdm = _m(sd)
    sk = []
    for r in _rows(sd, "candidates")[:4]:
        label = "protected" if r.get("protected") else "busy" if r.get("busy") else ""
        row = _named_size(r)
        row.update(r=_t(str(r.get("reason") or "").split(":")[0], 28), k=label, c="gray" if label else "yellow")
        sk.append(row)
    act = _num(sdm.get("actionable"))
    p["sk"], p["skc"], p["sks"] = sk, _col(sd), _sum(sd, 80)
    p["skn"] = int(act) if act is not None else sum(1 for r in sk if not r["k"])

    sp = t.get("spike_sampler")
    sm = _m(sp)
    top = sm.get("largest") if isinstance(sm.get("largest"), list) else []
    p["top"] = [_named_size(r) for r in top[:3]]
    cn = _num(sm.get("containers"))
    p["cn"], p["ta"] = "" if cn is None else f"{cn:.0f}", _hs(sm, "anon_total", "total_anon")

    p["ln"] = [{"l": lbl, "s": _sum(t.get(name), 80) or "no data", "c": _col(t.get(name))}
               for lbl, name in (("Orphans", "orphan_report"), ("Alert path", "alert_path_health"),
                                 ("Images", "image_ledger"))]
    p["head"] = ("no data" if not sd else f"{p['skn']} stuck" if p["skn"] else f"{len(sk)} watched" if sk else "no stuck")
    return fit(p)


# --------------------------------------------------------------------------- reclaim
def _pending_bytes(e: dict) -> float:
    """What a report-mode cleaner says it would free (apply-mode runs report what they did, not what is pending)."""
    m = _m(e)
    return _parse_h(m.get("selected_h")) if m.get("mode") == "report" and (_num(m.get("selected"), 0) or 0) > 0 else 0.0


def _plan_size(r: dict) -> float:
    for k in ("size", "bytes", "size_bytes"):
        v = _num(r.get(k))
        if v is not None:
            return v
    return 0.0


def reclaim(status: dict, now: float | None = None) -> dict:
    p, now = _base(status, now)
    rec = _reclaimed(status, now)
    tot = rec["tot"]
    p.update(head=human(tot["d1"]) + " / 24h", d1=human(tot["d1"]), d7=human(tot["d7"]), d30=human(tot["d30"]),
             d90=human(tot["d90"]))
    p["ev"] = [{"w": _age_txt(now - float(r["t"])) + " ago", "n": _title(status, str(r.get("task"))),
                "h": human(float(r["bytes"]))} for r in rec["log"][:5]]
    top = max(rec["by"].values(), default=0.0)
    p["by"] = [{"n": _title(status, k), "h": human(v), "p": int(round(v / top * 100)) if top else 0}
               for k, v in sorted(rec["by"].items(), key=lambda kv: -kv[1])[:4]]

    pend = [(b, name, e) for name, e in _tasks(status).items() if e.get("klass") == "C1"
            for b in [_pending_bytes(e)] if b > 0]
    pend.sort(key=lambda x: -x[0])
    p["pend"] = human(sum(b for b, _n, _e in pend))
    p["pr"] = [{"n": _t(e.get("title") or name, 22), "h": human(b), "m": _mode(e), "c": "gray"} for b, name, e in pend[:5]]

    plans, cand = [], []
    for name, e in sorted(_tasks(status).items()):
        plan = e.get("plan")
        if e.get("klass") != "C2" or not isinstance(plan, dict):
            continue
        items = [r for r in plan.get("items", []) if isinstance(r, dict)]
        total = _num(plan.get("total_bytes"))
        total = total if total is not None else sum(_plan_size(r) for r in items)
        h = _t(e.get("plan_hash"), 16)
        plans.append({"n": _t(e.get("title") or name, 22), "h": human(total), "k": len(items), "x": h,
                      "cmd": _clean(f"homelab-maint approve {name} {h}") if h else ""})
        for r in sorted(items, key=lambda r: -_plan_size(r))[:4]:
            cand.append({"n": _short_path(r.get("name"), 26), "h": human(_plan_size(r)),
                         "m": bool(r.get("needs_manual_check")), "c": "yellow" if r.get("needs_manual_check") else "gray"})
    p["plans"], p["cand"] = plans[:2], cand[:5]
    on, allc = _apply_counts(status)
    p.update(ap_on=on, ap_all=allc)
    return fit(p)


# --------------------------------------------------------------------------- dispatch / raw status
ROUTES = {"overview": overview, "disk": disk, "jobs": jobs, "guard": guard, "reclaim": reclaim}


def build(route: str, status: dict, now: float | None = None) -> dict:
    return ROUTES[route](status if isinstance(status, dict) else {}, now)


def error_payload(msg: str) -> dict:
    return {"error": _t(msg, 80), "stale": True}


def public_status(status: dict) -> dict:
    """status.json minus C2 plans (can be large and list paths) and task tracebacks."""
    out = {k: v for k, v in status.items() if k != "tasks"}
    out["tasks"] = {}
    for name, e in _tasks(status).items():
        e = {k: v for k, v in e.items() if k != "plan"}
        if isinstance(e.get("metrics"), dict):
            e["metrics"] = {k: v for k, v in e["metrics"].items() if k != "traceback"}
        out["tasks"][name] = e
    return out


def main(argv: list[str] | None = None) -> int:
    """Debug helper: python3 -m homelab_maint.payloads ROUTE [STATUS_JSON] [NOW] prints the payload."""
    import sys
    a = argv if argv is not None else sys.argv[1:]
    if not a or a[0] not in ROUTES:
        print("usage: payloads ROUTE [STATUS_JSON] [NOW]  ROUTE in " + "|".join(ROUTES), file=sys.stderr)
        return 2
    st = read_json(Path(a[1]) if len(a) > 1 else STATE_DIR / "status.json", {}) or {}
    print(json.dumps(build(a[0], st, float(a[2]) if len(a) > 2 else None), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
