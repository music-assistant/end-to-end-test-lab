"""Tests for the one-page pipe copier."""

from __future__ import annotations

import fcntl
import os
import sys
import termios
import threading
import time

import pytest

from labctl.pipecopy import DEFAULT_PIPE_BYTES, PIPE_BYTES, copy

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="F_SETPIPE_SZ is Linux only")


def test_copy_shrinks_both_pipes_and_copies_everything() -> None:
    """A source that is full before the copy starts still drops to one page, losing nothing."""
    src_r, src_w = os.pipe()
    dst_r, dst_w = os.pipe()
    data = os.urandom(DEFAULT_PIPE_BYTES * 2 + 17)

    def feed() -> None:
        os.write(src_w, data)
        os.close(src_w)

    def run_copy() -> None:
        try:
            copy(src_r, dst_w)
        finally:
            os.close(dst_w)

    feeder = threading.Thread(target=feed, daemon=True)
    copier = threading.Thread(target=run_copy, daemon=True)
    feeder.start()
    # the copy starts on a source the feeder has already filled, like MPD's pipe
    deadline = time.monotonic() + 5
    while _buffered(src_r) <= PIPE_BYTES and time.monotonic() < deadline:
        time.sleep(0.01)
    copier.start()
    received = b""
    while chunk := os.read(dst_r, PIPE_BYTES):
        received += chunk
    copier.join(5)
    feeder.join(5)

    assert not copier.is_alive()
    assert received == data
    page = max(PIPE_BYTES, os.sysconf("SC_PAGESIZE"))
    assert fcntl.fcntl(src_r, fcntl.F_GETPIPE_SZ) == page
    assert fcntl.fcntl(dst_r, fcntl.F_GETPIPE_SZ) == page


def _buffered(fd: int) -> int:
    """Bytes waiting in the pipe."""
    return int.from_bytes(fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"), sys.byteorder)
