"""The research worker's slow facts, refreshed off the request path so ``GET /status`` stays fast.

Two facts the worker reports are slow to establish on this Pi. Whether the installed CLI copy is the
verified one needs a hash of the whole copy after any change (``research_cli_policy``), and a DNS
lookup through the public resolvers the unit is confined to can take the resolver's whole retry
budget (``systemd/research-resolv.conf``: two servers, ``timeout:2 attempts:2``) on a phone hotspot.
The first real install (2026-11-17) stalled ``/status`` past its callers' deadlines while it hashed
inline, and its two-second DNS probe reported a failure with no reason.

``Facts`` recomputes the identity facts on its own thread, when a caller asks and at least every
``REFRESH_SECONDS``; a caller waits at most a stated bound for a refresh that started after it asked
and otherwise gets the last one with the time it was taken. ``DnsProbe`` runs at most one lookup at
a time on its own thread, keeps the reason for a failure and when it was measured, and counts public
DNS as usable for ``DNS_FRESH_SECONDS`` after its last success, so one slow lookup on a flaky uplink
neither hides research nor refuses a launch.
"""

import json
import socket
import sys
import threading
import time

from projects.vehicle_data.assistant_common import now

REFRESH_SECONDS = 60
DNS_HOST = "api.anthropic.com"
# One lookup may use the resolver's whole budget (2 servers x 2 attempts x 2 s); past this it counts
# as unanswered for the status display, though a late answer is still recorded.
DNS_PROBE_BOUND_SECONDS = 10
DNS_PROBE_GAP_SECONDS = 30  # at most one lookup every 30 s, however often status is read
DNS_FRESH_SECONDS = 300  # the freshness rule: DNS is usable while a lookup succeeded this recently


def log(event, **fields):
    print(json.dumps({"event": event, **fields}), file=sys.stderr, flush=True)


class Facts:
    """A value recomputed by ``compute`` on one background thread.

    ``current(wait)`` asks for a refresh and waits up to ``wait`` seconds for one that began after the
    call, so a caller sees changes made just before it asked when the Pi has time to compute them.
    It never waits longer: then it returns the previous value (``None`` before the first refresh)
    with ``current`` false. A failed refresh keeps the previous value and is logged."""

    def __init__(self, compute, every=REFRESH_SECONDS, before=None, wall=now):
        self.compute, self.every, self.before, self.wall = compute, every, before, wall
        self.cond = threading.Condition()
        self.asked = self.served = self.good = 0  # tickets: asked for, finished, finished with a value
        self.value = self.taken_at = None
        self.stopping = False
        self.thread = None

    def start(self):
        with self.cond:
            if self.thread is None:
                thread = threading.Thread(target=self._loop, daemon=True, name="research-facts")
                try:
                    thread.start()
                except RuntimeError as exc:  # e.g. TasksMax reached; stay unstarted so the next call retries
                    log("dtc_research_facts_thread_failed", error=repr(exc)[:300])
                    return False
                self.thread = thread
            return True

    def stop(self):
        with self.cond:
            self.stopping = True
            self.cond.notify_all()

    def request(self):
        """Ask for a refresh without waiting; its ticket."""
        with self.cond:
            self.asked += 1
            self.cond.notify_all()
            return self.asked

    def current(self, wait):
        """``(value, taken_at, current)``; ``current`` is false when the refresh did not finish in
        time or failed."""
        ticket = self.request()
        running = self.start()
        with self.cond:
            if running:  # with no thread, waiting would only burn the bound
                self.cond.wait_for(lambda: self.served >= ticket, wait)
            return self.value, self.taken_at, self.good >= ticket

    def _loop(self):
        while True:
            with self.cond:  # a new request, or the periodic refresh when nobody asks
                self.cond.wait_for(lambda: self.stopping or self.asked > self.served, self.every)
                if self.stopping:
                    return
                ticket = self.asked
            try:
                if self.before is not None:
                    self.before()
                value = self.compute()
            except Exception as exc:  # the thread must survive; the previous value stays, marked by its time
                log("dtc_research_facts_failed", error=repr(exc)[:300])
                value = None
            with self.cond:
                if value is not None:
                    self.value, self.taken_at, self.good = value, self.wall(), ticket
                self.served = ticket
                self.cond.notify_all()


class DnsProbe:
    """Whether public DNS answers from inside the research unit, with the evidence for it.

    ``start()`` begins a lookup on a daemon thread unless one is in flight or one began less than
    ``gap`` seconds ago; it never waits. ``view()`` reads the evidence without waiting: ``ok`` is the
    freshness rule (a success within ``fresh`` seconds), ``result`` the last finished lookup
    (``pending`` before any), ``reason`` why the last lookup failed or that the one in flight has
    had no answer for longer than ``bound`` seconds."""

    def __init__(self, host=DNS_HOST, resolve=socket.getaddrinfo, clock=time.monotonic, wall=now,
                 bound=DNS_PROBE_BOUND_SECONDS, gap=DNS_PROBE_GAP_SECONDS, fresh=DNS_FRESH_SECONDS):
        self.host, self.resolve, self.clock, self.wall = host, resolve, clock, wall
        self.bound, self.gap, self.fresh = bound, gap, fresh
        self.lock = threading.Lock()
        self.in_flight_since = None
        self.last_started = None
        self.last = None  # {"result", "reason", "at", "seconds"} of the last finished lookup
        self.last_ok = None  # (monotonic, wall) of the last success

    def start(self, force=False):
        """The lookup's thread, or ``None`` when none was started."""
        with self.lock:
            moment = self.clock()
            if self.in_flight_since is not None:
                return None
            if not force and self.last_started is not None and moment - self.last_started < self.gap:
                return None
            previous_start = self.last_started
            self.in_flight_since = self.last_started = moment
        thread = threading.Thread(target=self._lookup, args=(moment,), daemon=True, name="research-dns")
        try:
            thread.start()
        except RuntimeError as exc:  # e.g. TasksMax reached: release the reservation or no lookup ever runs again
            with self.lock:
                self.in_flight_since = None
                self.last_started = previous_start  # the gap measures lookups, not failed starts
                self.last = {"result": "failed", "reason": f"could not start a lookup thread: {exc}"[:200],
                             "at": self.wall(), "seconds": None}
            return None
        return thread

    def _lookup(self, began):
        try:
            # IPv4 only: this Pi boots with ipv6.disable=1, so only A records can be used, and one
            # query avoids a parallel A/AAAA pair on one socket losing a reply behind a NAT.
            self.resolve(self.host, 443, socket.AF_INET, socket.SOCK_STREAM)
            result, reason = "ok", None
        except Exception as exc:  # any failure is evidence to keep, never a crash
            result, reason = "failed", f"{type(exc).__name__}: {exc}"[:200]
        with self.lock:
            finished = self.clock()
            seconds = round(finished - began, 3)
            self.last = {"result": result, "reason": reason, "at": self.wall(), "seconds": seconds}
            if result == "ok":
                self.last_ok = (finished, self.last["at"])
            self.in_flight_since = None

    def usable(self):
        return self.view()["ok"]

    def view(self):
        with self.lock:
            moment = self.clock()
            ok = self.last_ok is not None and moment - self.last_ok[0] <= self.fresh
            last = self.last or {"result": "pending", "reason": None, "at": None, "seconds": None}
            reason = last["reason"]
            waited = None if self.in_flight_since is None else moment - self.in_flight_since
            if waited is not None and waited > self.bound:
                reason = f"no answer within {self.bound} s (a lookup has waited {int(waited)} s)"
            return {"ok": ok, "result": last["result"], "reason": reason, "checked_at": last["at"],
                    "seconds": last["seconds"], "last_ok_at": self.last_ok[1] if self.last_ok else None,
                    "fresh_seconds": self.fresh}


def refusal_detail(view):
    """The launch refusal's detail when ``view`` is not ``ok``: it names DNS and says to retry."""
    if view["result"] == "pending" and not view["reason"]:
        return "the research service has not finished its first public DNS check; try again in a minute"
    why = f" ({view['reason']})" if view["reason"] else ""
    return (f"public DNS has not answered from the research service in the last {view['fresh_seconds'] // 60} "
            f"minutes{why}; try again in a minute")
