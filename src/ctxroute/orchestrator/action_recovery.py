"""Optional recovery of an exact echoed prompt suffix, never arbitrary extra actions."""

import json


def without_prompt_echo(
    text: str, progress: dict[str, str | int | float | bool]
) -> tuple[str, bool]:
    stripped = text.lstrip()
    try:
        action, offset = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError:
        return text, False
    prefix = "NEXT ACTION (orchestrator guidance, not tool output)\n"
    suffix = stripped[offset:].strip()
    if not isinstance(action, dict) or not suffix.startswith(prefix):
        return text, False
    try:
        echoed = json.loads(suffix.removeprefix(prefix))
    except json.JSONDecodeError:
        return text, False
    expected = {
        "priority": progress.get("next_priority", "investigate"),
        "guidance": progress.get("next_action", "Choose the next useful action."),
        "request": "Return exactly one new JSON action. Do not replay the last record.",
    }
    if echoed != expected:
        return text, False
    return stripped[:offset], True
