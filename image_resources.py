"""Bound image memory across jobs, map rendering and edits in one worker.

The production service has one Gunicorn worker. Per-job asyncio semaphores
alone do not bound the total when multiple jobs/edits run together.
"""
from contextlib import contextmanager
import functools
import inspect
import gc
import os
import sys
import threading


def _configured_limit():
    try:
        return max(1, min(int(os.environ.get("ZUKAI_IMAGE_TASK_LIMIT", "2")), 8))
    except ValueError:
        return 2


IMAGE_TASK_LIMIT = _configured_limit()
_slots = threading.BoundedSemaphore(IMAGE_TASK_LIMIT)
# Keep the two remote requests, but read/decode only one large edit response at
# a time. An unread streaming response does not retain a full base64 body.
_edit_pixels = threading.BoundedSemaphore(1)


def release_unused_memory():
    """Collect SDK/HTTP cycles and return freed native buffers on Render Linux."""
    try:
        gc.collect()
        if sys.platform.startswith("linux"):
            import ctypes
            ctypes.CDLL(None).malloc_trim(0)
    except (AttributeError, OSError):
        pass  # Non-glibc/local platforms still get Python garbage collection.


def edit_pixels(function):
    """Serialize edit preparation/response decoding, without serializing HTTP."""
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        with _edit_pixels:
            try:
                return function(*args, **kwargs)
            finally:
                # function's locals have gone before collecting/returning pages.
                release_unused_memory()
    return wrapped


@contextmanager
def image_task(cancel_check=None):
    while True:
        if cancel_check:
            cancel_check()
        if _slots.acquire(timeout=0.2):
            break
    try:
        if cancel_check:
            cancel_check()
        yield
    finally:
        try:
            release_unused_memory()
        finally:
            _slots.release()


def limited_image_task(function):
    """Acquire before requesting/decoding bytes; release even on failure/cancel."""
    parameters = list(inspect.signature(function).parameters)
    cancel_index = parameters.index("cancel_check") if "cancel_check" in parameters else None

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        check = kwargs.get("cancel_check")
        if check is None and cancel_index is not None and len(args) > cancel_index:
            check = args[cancel_index]
        with image_task(check):
            return function(*args, **kwargs)
    return wrapped
