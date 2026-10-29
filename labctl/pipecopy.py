"""Copy stdin to a FIFO through one-page pipe buffers, so a player's stop leaves almost no tail."""

from __future__ import annotations

import argparse
import errno
import fcntl
import os
import sys
import time
from pathlib import Path

PIPE_BYTES = 4096
# the default pipe size; one read this big empties a full pipe so it can shrink
DEFAULT_PIPE_BYTES = 65536


def copy(src: int, dst: int) -> None:
    """Shrink both pipes to one page, then copy src to dst with blocking writes until EOF."""
    # dst only holds what an earlier writer left, which the reader drains
    while not _shrink(dst):
        time.sleep(0.01)
    # the player fills src before we start and refills it on every read, so empty it in one
    # read and shrink right after, until the shrink wins the race
    src_shrunk = False
    while chunk := os.read(src, PIPE_BYTES if src_shrunk else DEFAULT_PIPE_BYTES):
        src_shrunk = src_shrunk or _shrink(src)
        view = memoryview(chunk)
        while view:
            view = view[os.write(dst, view) :]


def main(argv: list[str] | None = None) -> None:
    """Run the `lab-pipecopy` CLI."""
    parser = argparse.ArgumentParser(prog="lab-pipecopy", description=__doc__)
    parser.add_argument("output", type=Path, help="FIFO to write")
    args = parser.parse_args(argv)
    dst = os.open(args.output, os.O_WRONLY)
    try:
        copy(sys.stdin.fileno(), dst)
    finally:
        os.close(dst)


def _shrink(fd: int) -> bool:
    """Set the pipe to one page; False while it holds more than that."""
    try:
        fcntl.fcntl(fd, fcntl.F_SETPIPE_SZ, PIPE_BYTES)
    except OSError as err:
        if err.errno != errno.EBUSY:
            raise
        return False
    return True
