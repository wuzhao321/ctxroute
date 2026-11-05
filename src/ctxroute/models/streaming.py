"""Bounded SSE completion tracking without storing model text in diagnostics."""

from dataclasses import dataclass, field


class IncompleteStreamError(RuntimeError):
    """The stream ended without the protocol completion marker."""


@dataclass
class StreamTracker:
    events: int = 0
    keepalives: int = 0
    done: bool = False
    max_event_bytes: int = 1_048_576
    _buffer: bytes = b""
    _data: list[bytes] = field(default_factory=list[bytes])
    _event_bytes: int = 0

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            line = line.removesuffix(b"\r")
            self._event_bytes += len(line)
            if self._event_bytes > self.max_event_bytes:
                raise ValueError("SSE event exceeded byte limit")
            if not line:
                if self._data:
                    self.events += 1
                    if b"\n".join(self._data).strip() == b"[DONE]":
                        self.done = True
                self._data.clear()
                self._event_bytes = 0
            elif line.startswith(b":"):
                self.keepalives += 1
            elif line == b"data" or line.startswith(b"data:"):
                self._data.append(line.partition(b":")[2].removeprefix(b" "))
        if len(self._buffer) + self._event_bytes > self.max_event_bytes:
            raise ValueError("SSE event exceeded byte limit")
