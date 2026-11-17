"""Bounded local output evidence; never used to authorize an action or settle usage."""

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, cast


@dataclass
class TextAudit:
    observed: bool = False
    chars: int = 0
    whitespace: int = 0
    prefix: str = ""
    _digest: Any = field(default_factory=hashlib.sha256, repr=False)

    def add(self, text: str) -> None:
        self.observed = True
        self.chars += len(text)
        self.whitespace += sum(c.isspace() for c in text)
        self._digest.update(text.encode())
        self.prefix += text[: max(0, 65536 - len(self.prefix))]

    def record(self, key: str) -> dict[str, Any]:
        text = self.prefix.replace(key, "[REDACTED]") if key else self.prefix
        blocks = Counter(text[n : n + 64] for n in range(0, len(text) - 63, 64))
        return {
            "chars": self.chars,
            "whitespace_chars": self.whitespace,
            "sha256": self._digest.hexdigest(),
            "captured_chars": len(self.prefix),
            "capture_complete": self.observed and self.chars == len(self.prefix),
            "redacted": text != self.prefix,
            "text": text,
            "most_repeated_64char_block_count": max(blocks.values(), default=0),
        }


@dataclass
class OutputAudit:
    content: TextAudit = field(default_factory=TextAudit)
    reasoning: TextAudit = field(default_factory=TextAudit)

    def capture_response(self, response: Any) -> None:
        """Capture one non-streaming receipt before SDK output validation, once."""
        if not isinstance(response, dict):
            return
        choices = cast(dict[str, Any], response).get("choices")
        if not isinstance(choices, list) or not choices:
            return
        choice = cast(list[Any], choices)[0]
        if not isinstance(choice, dict):
            return
        message = cast(dict[str, Any], choice).get("message")
        if not isinstance(message, dict):
            return
        for name, audit in (("content", self.content), ("reasoning_content", self.reasoning)):
            value = cast(dict[str, Any], message).get(name)
            if isinstance(value, str) and not audit.observed:
                audit.add(value)

    def record(self, key: str) -> dict[str, Any]:
        return {"content": self.content.record(key), "reasoning": self.reasoning.record(key)}
