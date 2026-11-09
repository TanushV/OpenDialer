"""R9E.6: a blocker that promised to clear by itself and did not is repaired, and if that fails, called stuck.

Pure bookkeeping: the caller passes the blocker codes it sees and the time, and gets back the codes to repair now.
A code is due for repair once it outlives its promise (`EXITS[code].by_itself_within`). After a repair it gets one
more promise window; still there after that, it is stuck, and the screen shows its real control instead of
"clears by itself". A code that goes away is forgotten.
"""

from app.calling.blockers import EXITS


class Watchdog:
    def __init__(self):
        self.since = {}  # code -> when it was first seen, or last repaired
        self.repairs = {}  # code -> repairs tried while it stayed
        self.stuck = set()

    def observe(self, codes, now):
        """Record what blocks calling now; return the codes to repair now."""
        codes = set(codes)
        for code in list(self.since):
            if code not in codes:
                self.since.pop(code)
                self.repairs.pop(code, None)
                self.stuck.discard(code)
        due = []
        for code in sorted(codes):
            promise = EXITS[code].by_itself_within if code in EXITS else None
            if not promise:
                continue
            start = self.since.setdefault(code, now)
            if now - start < promise:
                continue
            tried = self.repairs.get(code, 0)
            if tried >= 1:
                self.stuck.add(code)
            self.repairs[code] = tried + 1
            self.since[code] = now
            due.append(code)
        return due
