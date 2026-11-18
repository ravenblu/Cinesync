"""Background JPEG decoding and bounded frame storage for the Maya player."""
from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path

from .qt import QtGui


class FrameCache:
    """One decoder, replaceable pending requests, bounded decoded bytes."""

    def __init__(self, megabytes: int = 256) -> None:
        self.limit = megabytes * 1024 * 1024
        self.bytes = 0
        self.items = OrderedDict()
        self.pending = []
        self.condition = threading.Condition()
        self.stopped = False
        self.worker = threading.Thread(target=self._decode, name="CineSync images", daemon=True)
        self.worker.start()

    def request(self, paths: list) -> None:
        with self.condition:
            self.pending = list(dict.fromkeys(p for p in paths if p not in self.items))
            self.condition.notify()

    def get(self, path: Path):
        with self.condition:
            entry = self.items.get(path)
            if entry is not None:
                self.items.move_to_end(path)
                return entry[0]
        return None

    def _decode(self) -> None:
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopped or self.pending)
                if self.stopped:
                    return
                path = self.pending.pop(0)
            image = QtGui.QImage(str(path))
            size = image.sizeInBytes() if hasattr(image, "sizeInBytes") else image.byteCount()
            size = max(1, size)
            with self.condition:
                if self.stopped:
                    return
                if size > self.limit:
                    image, size = QtGui.QImage(), 1
                old = self.items.pop(path, None)
                if old:
                    self.bytes -= old[1]
                self.items[path] = (image, size)
                self.bytes += size
                while self.bytes > self.limit or len(self.items) > 512:
                    _, (_, removed) = self.items.popitem(last=False)
                    self.bytes -= removed

    def close(self) -> None:
        with self.condition:
            self.stopped = True
            self.pending.clear()
            self.items.clear()
            self.bytes = 0
            self.condition.notify()
        # No GUI wait on potentially slow network I/O; worker owns no widgets.
