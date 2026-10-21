"""Set one top-level key of a JSON object held as text, leaving every other byte as it was.

The new value is indented like the object's existing members; a new key goes in sorted position
when the existing keys are sorted (as consolidate writes its baseline), else at the end. The result
is parsed back and compared, so a scanning mistake raises instead of writing a damaged file."""
import json
import re
from typing import List, Optional, Tuple

WHITESPACE = re.compile(r"[ \t\n\r]*")
DECODER = json.JSONDecoder()


def skip(text: str, index: int) -> int:
    return WHITESPACE.match(text, index).end()


def members(text: str) -> Tuple[int, int, List[Tuple[str, int, int, int]]]:
    """(index of '{', index of the closing '}', [(key, key start, value start, value end)])."""
    i = skip(text, 0)
    if text[i:i + 1] != "{":
        raise ValueError("not a JSON object")
    opening, i = i, skip(text, i + 1)
    found = []
    if text[i:i + 1] == "}":
        return opening, i, found
    while True:
        key_start = i
        key, i = DECODER.raw_decode(text, i)
        i = skip(text, i)
        if not isinstance(key, str) or text[i:i + 1] != ":":
            raise ValueError(f"malformed member at offset {key_start}")
        value_start = skip(text, i + 1)
        _, i = DECODER.raw_decode(text, value_start)
        found.append((key, key_start, value_start, i))
        i = skip(text, i)
        if text[i:i + 1] == ",":
            i = skip(text, i + 1)
        elif text[i:i + 1] == "}":
            return opening, i, found
        else:
            raise ValueError(f"malformed object at offset {i}")


def member_indent(text: str, opening: int, found) -> Optional[str]:
    if not found:
        return None
    line_start = text.rfind("\n", opening, found[0][1])
    return text[line_start + 1:found[0][1]] if line_start >= 0 else None


def render(value, indent: str, sort_keys: bool) -> str:
    return json.dumps(value, indent=indent, sort_keys=sort_keys).replace("\n", "\n" + indent)


def set_key(text: str, key: str, value, sort_keys: bool = True) -> str:
    original = json.loads(text)
    opening, closing, found = members(text)
    indent = member_indent(text, opening, found)
    one_line = indent is None
    indent = indent if indent is not None else "  "
    rendered = render(value, indent, sort_keys)
    existing = next((m for m in found if m[0] == key), None)
    if existing:
        result = text[:existing[2]] + rendered + text[existing[3]:]
    else:
        member = json.dumps(key) + ": " + rendered
        keys = [m[0] for m in found]
        later = next((m for m in found if m[0] > key), None) if keys == sorted(keys) and not one_line else None
        if later:
            result = text[:later[1]] + member + ",\n" + indent + text[later[1]:]
        elif found:
            end = found[-1][3]
            tail = "" if "\n" in text[end:closing] else "\n"
            result = text[:end] + ",\n" + indent + member + tail + text[end:]
        else:
            result = text[:opening + 1] + "\n" + indent + member + "\n" + text[closing:]
    expected = dict(original)
    expected[key] = value
    if json.loads(result) != expected:
        raise ValueError("splice check failed; the file was not changed")
    return result
