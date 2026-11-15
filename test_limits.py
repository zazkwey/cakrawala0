"""Unit test for limits.py (standard library only).   python test_limits.py"""
import threading
import time

import limits

running, peak, lock = 0, 0, threading.Lock()


def scan():
    global running, peak
    with limits.heavy():
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1


threads = [threading.Thread(target=scan) for _ in range(12)]
[t.start() for t in threads]
[t.join() for t in threads]
assert peak <= limits.MAX_CONCURRENT_SCANS, peak
assert peak >= 1

# a scan that fails must still free its slot, otherwise the app would slowly lock up
for _ in range(limits.MAX_CONCURRENT_SCANS * 3):
    try:
        with limits.heavy():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
done = threading.Event()
threading.Thread(target=lambda: (limits.heavy().__enter__(), done.set())).start()
assert done.wait(2), "slot was not released after an exception"
print("test_limits: ALL PASSED (peak concurrent scans =", peak, ")")
