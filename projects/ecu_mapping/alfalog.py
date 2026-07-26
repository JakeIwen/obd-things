"""Parse a *decoded* AlfaOBD debug log (output of tools/alfaobd_decode.py) into UDS
request->response exchanges. AlfaOBD-log-specific, so it lives with this project rather
than in lib/.

Decoded-log line grammar:
  HH:MM:SS.mmm S: <hex>    bytes sent to the ELM/STN adapter (hex-encoded ASCII command)
  HH:MM:SS.mmm R: <hex>    bytes received; multi-frame = a length line + indexed 0:/1:/2:
  <free text>              annotations: "Recording data for X", "Recording closed <date>", ...

A UDS request's payload is the ASCII of that hex (e.g. "22F190" = ReadDataByIdentifier F190).
Response callbacks are buffered through the adapter prompt because AlfaOBD may split an
indexed row at an arbitrary callback boundary. Indexed responses are validated and trimmed
to the ELM length header; malformed/partial indexed data fails closed as an empty response.
Everything streams so multi-hundred-MB logs stay cheap.
"""
import re

_LINE = re.compile(r'^(\d{2}:\d{2}:\d{2}\.\d{3}) ([SR]): ([0-9A-Fa-f]*)$')
_SEG  = re.compile(r'^([0-9A-Fa-f]):([0-9A-Fa-f]+)$')
_DATE = re.compile(r'(\d{4}/\d{2}/\d{2})')
_REC  = re.compile(r'Recording data for (.+)')
MAX_RESPONSE_BUFFER_CHARS = 1 << 20


def ascii_of(hexstr):
    try:
        return bytes.fromhex(hexstr).decode("latin-1")
    except ValueError:
        return ""


def _logical_lines(path, chunk=1 << 20):
    """Yield logical lines (split on \\r or \\n) without loading the whole file."""
    buf = ""
    with open(path, "r", encoding="latin-1") as f:
        while True:
            c = f.read(chunk)
            if not c:
                break
            buf = (buf + c).replace("\r\n", "\n").replace("\r", "\n")
            *lines, buf = buf.split("\n")
            yield from lines
        if buf:
            yield buf


def iter_lines(path):
    """Public: yield logical (\\r/\\n-split) lines of a decoded log, streaming."""
    yield from _logical_lines(path)


def recording_date_hints(path):
    """Map recording-header line numbers to dates written by their closing marker.

    AlfaOBD may not write a full date until ``Recording closed ...``. Pre-index
    header/close pairs so streaming parsers can use that date from the start without
    buffering a potentially very large recording in memory. Unclosed recordings have
    no hint and retain the parser's prior best-known date.
    """
    hints = {}
    active_start = None
    first_timestamp = None
    for line_number, line in enumerate(_logical_lines(path), 1):
        if _REC.search(line):
            active_start = line_number
            first_timestamp = None
            continue
        transport = _LINE.match(line)
        if active_start is not None and first_timestamp is None and transport:
            first_timestamp = transport.group(1)
        if "Recording closed" not in line:
            continue
        match = _DATE.search(line)
        close_time = re.search(r'\d{4}/\d{2}/\d{2} (\d{2}:\d{2}:\d{2}\.\d{3})', line)
        # AlfaOBD can leave recording enabled across days. In that case a later close
        # marker's clock may precede this block's first transport timestamp, so assigning
        # its date to the whole block would be demonstrably wrong. Retain the prior date.
        clocks_are_ordered = (
            first_timestamp is None
            or close_time is None
            or close_time.group(1) >= first_timestamp
        )
        if active_start is not None and match and clocks_are_ordered:
            hints[active_start] = match.group(1)
        active_start = None
        first_timestamp = None
    return hints


def _only_response_pending(parts, request):
    """Whether every plain response is an exact pending NRC for this request."""
    if not parts or not re.fullmatch(r"[0-9A-F]{2,}", request or ""):
        return False
    expected = f"7F{request[:2]}78"
    return all(part == expected for part in parts)


def _parse_response_block(block, request=None):
    """Return one validated prompt-delimited ELM response as uppercase hex.

    AlfaOBD's debug callback boundaries do not necessarily align with the ELM's
    CR-delimited response rows. ``block`` must therefore be the concatenation of
    every callback up to a prompt (or the best available partial block when a
    request/eof flushes it).

    Empty, textual, structurally inconsistent, and incomplete indexed responses
    return ``""``. It is safer for downstream correlation to retain an exchange
    with no usable response than to fabricate bytes from a partial ISO-TP row.
    """
    segments = []
    plain = []
    length_header = None
    saw_segment = False

    for raw_part in re.split(r"\r\n|\r|\n", block):
        part = raw_part.strip().upper().replace(" ", "")
        if not part or part == ">":
            continue
        segment = _SEG.fullmatch(part)
        if segment:
            if length_header is None or (
                plain and not _only_response_pending(plain, request)
            ):
                return ""
            index = int(segment.group(1), 16)
            encoded = segment.group(2)
            if len(encoded) % 2 or index != (len(segments) & 0x0F):
                return ""
            segments.append(encoded)
            saw_segment = True
            continue
        if re.fullmatch(r"[0-9A-F]{3}", part):
            if (
                length_header is not None
                or saw_segment
                or (
                    plain
                    and not _only_response_pending(plain, request)
                )
            ):
                return ""
            length_header = int(part, 16)
            if length_header <= 0:
                return ""
            continue
        if re.fullmatch(r"[0-9A-F]{2,}", part) and len(part) % 2 == 0:
            if length_header is not None or saw_segment:
                return ""
            plain.append(part)
            continue
        return ""

    if segments:
        if length_header is None or length_header < 8:
            return ""
        widths = [len(segment) // 2 for segment in segments]
        if (
            len(widths) < 2
            or widths[0] != 6
            or any(width != 7 for width in widths[1:-1])
            or not 1 <= widths[-1] <= 7
        ):
            return ""
        encoded = "".join(segments)
        bytes_before_final = sum(widths[:-1])
        total_bytes = len(encoded) // 2
        if not bytes_before_final < length_header <= total_bytes:
            return ""
        return "".join(plain) + encoded[:length_header * 2]

    if length_header is not None:
        return ""
    return "".join(plain)


def _completed_exchange(pend, completion_reason):
    """Render one pending exchange, including timing/provenance for exact joins."""
    response_overflow = pend["response_buffer_overflow"]
    return {
        "ts": pend["request_ts"],  # backwards-compatible request timestamp
        "request_ts": pend["request_ts"],
        "response_end_ts": pend["response_end_ts"],
        "request_line": pend["request_line"],
        "response_end_line": pend["response_end_line"],
        "completion_reason": completion_reason,
        "prompt_seen": completion_reason == "prompt",
        "response_buffer_overflow": response_overflow,
        "date": pend["date"],
        "addr": pend["addr"],
        "module": pend["module"],
        "req": pend["req"],
        "resp": (
            ""
            if response_overflow
            else _parse_response_block(
                pend["response_buffer"],
                pend["req"],
            )
        ),
    }


def iter_exchanges_detailed(path):
    """Yield completed UDS exchanges with request and response-end provenance.

    ``ts`` remains the request-send timestamp for compatibility.  ``completion_reason``
    distinguishes an actual adapter ``prompt`` from a pending exchange flushed by the
    ``next_request`` or ``eof``. Gauge/debug joins should anchor only prompt-completed
    ``response_end_ts`` values.
    """
    addr, module, date = "?", None, "????/??/??"
    pend = None
    date_hints = recording_date_hints(path)
    for line_number, ln in enumerate(_logical_lines(path), 1):
        m = _LINE.match(ln)
        if not m:
            r = _REC.search(ln)
            if r:
                module = r.group(1).strip()
                date = date_hints.get(line_number, date)
            elif "Recording closed" in ln:
                module = None
            d = _DATE.search(ln)
            if d:
                date = d.group(1)
            continue
        ts, sr, payhex = m.group(1), m.group(2), m.group(3)
        decoded_callback = ascii_of(payhex)
        pay = decoded_callback.strip().upper().replace(" ", "")
        if sr == "S":
            if pend:
                yield _completed_exchange(pend, "next_request")
                pend = None
            if pay.startswith("ATSH"):
                addr = pay[4:]
                continue
            if pay.startswith(("AT", "ST")) or not re.fullmatch(r"[0-9A-F]+", pay) or len(pay) < 2:
                continue
            pend = {
                "request_ts": ts,
                "response_end_ts": None,
                "request_line": line_number,
                "response_end_line": None,
                "date": date,
                "addr": addr,
                "module": module,
                "req": pay,
                "response_buffer": "",
                "response_buffer_overflow": False,
            }
        elif sr == "R" and pend is not None:
            pend["response_end_ts"] = ts
            pend["response_end_line"] = line_number
            # Keep the decoded callback verbatim: one indexed ELM row may end
            # halfway through one R callback and continue at the start of the
            # next. Normalizing each callback independently would corrupt it.
            prompt_seen = ">" in decoded_callback
            before_prompt = decoded_callback.split(">", 1)[0]
            if not pend["response_buffer_overflow"]:
                prospective_size = (
                    len(pend["response_buffer"]) + len(before_prompt)
                )
                if prospective_size > MAX_RESPONSE_BUFFER_CHARS:
                    pend["response_buffer"] = ""
                    pend["response_buffer_overflow"] = True
                else:
                    pend["response_buffer"] += before_prompt
            if prompt_seen:
                yield _completed_exchange(pend, "prompt")
                pend = None
    if pend:
        yield _completed_exchange(pend, "eof")


def iter_exchanges(path):
    """Yield legacy exchange dictionaries with request timestamp ``ts``."""
    keys = ("ts", "date", "addr", "module", "req", "resp")
    for exchange in iter_exchanges_detailed(path):
        yield {key: exchange[key] for key in keys}


def phys_addr(atsh):
    """ATSH header -> physical UDS address string (18DAxxF1 -> 'xx')."""
    return atsh[2:4] if atsh.startswith("DA") and len(atsh) >= 6 else atsh


def decode_vin(resp_hex):
    """If resp is a positive F190 read (62F190 + 17 bytes), return the VIN, else ''."""
    k = resp_hex.upper().find("62F190")
    if k < 0:
        return ""
    v = ascii_of(resp_hex[k + 6:k + 6 + 34])
    return v if len(v) >= 11 else ""


def redact_vin(v):
    """Mask the unique serial (positions 12-17) for publish-safe output; keep the
    WMI/VDS/year descriptor. Real VIN comparisons use the unmasked value internally."""
    v = (v or "").strip()
    return v[:11] + "######" if len(v) >= 17 else "«redacted»"
