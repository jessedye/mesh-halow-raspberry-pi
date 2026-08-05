#!/usr/bin/env python3
"""halow_rungcost — pure logic for the measured TRANSPORT_HALOW rung cost
(roadmap 20). No I/O here: halow-mon feeds it one window per minute and
persists the result; the UI serves the state file read-only.

The rule this implements (mesh-v4 docs/transport-ladder-halow.md:43-49):
"do not hand-pick an ordinal at all. A cost derived from measured rate
and delivery orders the rungs without anyone deciding whether HaLow is
7 or 20."

Anchors are the ladder's two MEASURED rates, never datasheet claims:
LoRa best 9.0 kbps [M] -> cost 100 (the lora rung), ESP-NOW healthy
604 kbps [M] -> cost 10 (the espnow rung). Cost is linear in log-rate
between them; floor 6 keeps healthy HaLow under wifi=5's rank never,
ceiling 99 keeps an associated-but-slow HaLow above lora=100.
"""

import math

DEMOTE_AFTER = 2   # mesh-v4 tools/vip.py:47 — undamped failover was unusable
PROMOTE_AFTER = 3  # vip.py:48 — health is earned, never granted on sight
EWMA_ALPHA = 0.3
BAD_DELIVERY_PCT = 70  # complement of mesh-v4 ESPNOW_FAIL_PCT_BAD 30 (patch 0004:5466)
ANCHOR_LORA_KBPS = 9.0     # [M] LoRa best (sf5-burst), transport-ladder doc
ANCHOR_ESPNOW_KBPS = 604.0  # [M] ESP-NOW healthy
COST_MIN, COST_MAX = 6, 99
_SLOPE = 90.0 / math.log10(ANCHOR_ESPNOW_KBPS / ANCHOR_LORA_KBPS)  # 49.3/decade

COUNTERS = ("tx_packets", "tx_retries", "tx_failed")


def cost_from_goodput(kbps):
    """Integer cost from EWMA goodput; None for no evidence."""
    if kbps is None or kbps <= 0:
        return None
    c = round(100 - _SLOPE * math.log10(kbps / ANCHOR_LORA_KBPS))
    return max(COST_MIN, min(COST_MAX, c))


def evaluate_window(prev, cur):
    """(verdict, window_dict) from two consecutive samples' cumulative
    counters. prev/cur are dicts with t, tx_packets/retries/failed,
    tx_mbps. cur=None means the MAC vanished from the dump: absent —
    not associated is not quiet; the rung is down for that node."""
    if cur is None:
        return "absent", None
    if prev is None:
        return "baseline", None
    if any(k not in cur or k not in prev for k in ("tx_packets", "tx_failed")):
        return "no-counters", None   # never fake 100% delivery
    d_pkts = cur["tx_packets"] - prev["tx_packets"]
    d_fail = cur["tx_failed"] - prev["tx_failed"]
    d_retr = cur.get("tx_retries", 0) - prev.get("tx_retries", 0)
    if d_pkts < 0 or d_fail < 0 or d_retr < 0:
        return "reset", None   # reassociation zeroed the counters; not evidence
    if d_pkts == 0:
        return "quiet", None   # an idle station is not a broken one
    delivery = 100.0 * (d_pkts - d_fail) / d_pkts
    retry = 100.0 * d_retr / d_pkts
    tx_mbps = cur.get("tx_mbps")
    goodput = (tx_mbps * 1000.0 * delivery / 100.0
               if tx_mbps is not None else None)
    verdict = "good" if delivery >= BAD_DELIVERY_PCT else "bad"
    return verdict, {"verdict": verdict,
                     "delivery_pct": round(delivery, 2),
                     "retry_pct": round(retry, 2),
                     "tx_mbps": tx_mbps,
                     "goodput_kbps": round(goodput, 1) if goodput else None,
                     "n_pkts": d_pkts}


def fresh_entry(now):
    return {"last": None, "healthy": False, "cost": None,
            "goodput_kbps_ewma": None, "oks": 0, "fails": 0,
            "window": None, "since": now, "last_seen": now,
            "windows": {"good": 0, "bad": 0, "quiet": 0, "reset": 0,
                        "absent": 0, "no_counters": 0}}


def step(entry, verdict, window, now):
    """vip.py evaluate() semantics: a good window zeroes fails, a bad one
    zeroes oks, transitions only at the streak thresholds. Indeterminate
    verdicts (quiet/reset/baseline/no-counters) leave streaks untouched —
    evidence merely ages. Returns the mutated entry."""
    entry["windows"][verdict.replace("-", "_")] = \
        entry["windows"].get(verdict.replace("-", "_"), 0) + 1
    if verdict in ("good", "bad", "absent"):
        bad = verdict != "good"
        if bad:
            entry["fails"] += 1
            entry["oks"] = 0
            if entry["fails"] >= DEMOTE_AFTER:
                entry["healthy"] = False
        else:
            entry["oks"] += 1
            entry["fails"] = 0
            if entry["oks"] >= PROMOTE_AFTER:
                entry["healthy"] = True
    if window is not None:
        entry["window"] = window
        g = window.get("goodput_kbps")
        if verdict in ("good", "bad") and g is not None:
            prev = entry["goodput_kbps_ewma"]
            entry["goodput_kbps_ewma"] = round(
                g if prev is None else (1 - EWMA_ALPHA) * prev
                + EWMA_ALPHA * g, 1)
    entry["cost"] = (cost_from_goodput(entry["goodput_kbps_ewma"])
                     if entry["healthy"] else None)
    return entry


def selftest():
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        print(f"{'PASS' if cond else 'FAIL'} {name}"
              f"{' ' + str(detail) if detail and not cond else ''}")
        ok = ok and cond

    # anchors, exact
    check("anchor-espnow", cost_from_goodput(604.0) == 10,
          cost_from_goodput(604.0))
    check("anchor-lora-clamp", cost_from_goodput(9.0) == 99,
          cost_from_goodput(9.0))
    check("anchor-fast-floor", cost_from_goodput(20000) == 6,
          cost_from_goodput(20000))
    seq = [cost_from_goodput(k) for k in
           (9, 20, 100, 333, 604, 2000, 20000)]
    check("monotonic-non-increasing",
          all(a >= b for a, b in zip(seq, seq[1:])), seq)
    check("no-evidence-no-cost", cost_from_goodput(None) is None
          and cost_from_goodput(0) is None)

    def samp(t, pkts, fail, retr=0, mbps=13.0):
        return {"t": t, "tx_packets": pkts, "tx_failed": fail,
                "tx_retries": retr, "tx_mbps": mbps}

    # baseline -> 3 good -> healthy with cost
    e = fresh_entry(0)
    v, w = evaluate_window(None, samp(0, 100, 0))
    step(e, v, w, 0)
    check("baseline-not-healthy", v == "baseline" and not e["healthy"])
    prev = samp(0, 100, 0)
    for i in range(1, 4):
        cur = samp(i * 60, 100 + i * 200, 0)
        v, w = evaluate_window(prev, cur)
        step(e, v, w, i * 60)
        prev = cur
        if i < 3:
            check(f"good-{i}-not-yet", v == "good" and not e["healthy"])
    check("healthy-after-3-good", e["healthy"] and
          isinstance(e["cost"], int) and COST_MIN <= e["cost"] <= COST_MAX,
          (e["healthy"], e["cost"]))
    # one bad does not demote; two consecutive do
    cur = samp(240, prev["tx_packets"] + 100, prev["tx_failed"] + 50)
    v, w = evaluate_window(prev, cur)
    step(e, v, w, 240)
    prev = cur
    check("one-bad-holds", v == "bad" and e["healthy"])
    cur = samp(300, prev["tx_packets"] + 100, prev["tx_failed"] + 50)
    v, w = evaluate_window(prev, cur)
    step(e, v, w, 300)
    prev = cur
    check("two-bad-demotes", not e["healthy"] and e["cost"] is None)
    # quiet freezes streaks
    oks_before = e["oks"]
    cur2 = dict(prev)
    cur2["t"] = 360
    v, w = evaluate_window(prev, cur2)
    step(e, v, w, 360)
    check("quiet-freezes", v == "quiet" and e["oks"] == oks_before
          and not e["healthy"])
    # counter reset discarded
    v, w = evaluate_window(prev, samp(420, 5, 0))
    check("reset-discarded", v == "reset" and w is None)
    # absent counts bad
    e2 = fresh_entry(0)
    e2["healthy"], e2["oks"] = True, 3
    for i in range(DEMOTE_AFTER):
        v, w = evaluate_window(samp(0, 100, 0), None)
        step(e2, v, w, i)
    check("absent-demotes", v == "absent" and not e2["healthy"])
    # missing tx_failed never yields a cost
    e3 = fresh_entry(0)
    p = {"t": 0, "tx_packets": 100, "tx_mbps": 13.0}
    c = {"t": 60, "tx_packets": 300, "tx_mbps": 13.0}
    for i in range(5):
        v, w = evaluate_window(p, c)
        step(e3, v, w, i)
    check("no-counters-no-health", v == "no-counters"
          and not e3["healthy"] and e3["cost"] is None)
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    print(__doc__)
