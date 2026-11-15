"""Process-wide guard for the heavy hotspot scans.

Streamlit runs every visitor in its own thread inside ONE process. Each raw-hotspot scan (pyarrow read + point-in-polygon on
millions of detections) briefly needs a lot of memory and CPU, so many visitors scanning at once can exceed the host's limits
and kill the whole app. `heavy()` lets only a few scans run at a time (the others wait, behind their normal spinner) and
hands freed memory back to the OS afterwards. Modules are imported once per process, so this semaphore is shared by all
visitors even though app.py itself is re-run on every click.
"""
import contextlib
import gc
import threading

MAX_CONCURRENT_SCANS = 2
_slots = threading.BoundedSemaphore(MAX_CONCURRENT_SCANS)


def release_memory():
    gc.collect()
    try:                                    # pyarrow keeps freed buffers in its pool for a while; give them back now
        import pyarrow as pa
        pa.default_memory_pool().release_unused()
    except Exception:
        pass


@contextlib.contextmanager
def heavy():
    with _slots:
        try:
            yield
        finally:
            release_memory()
