"""Parse a *decoded* AlfaOBD debug log (output of tools/alfaobd_decode.py) into UDS
request->response exchanges. AlfaOBD-log-specific, so it lives with this project rather
than in lib/.

Decoded-log line grammar:
  HH:MM:SS.mmm S: <hex>    bytes sent to the ELM/STN adapter (hex-encoded ASCII command)
  HH:MM:SS.mmm R: <hex>    bytes received; multi-frame = a length line + indexed 0:/1:/2:
  <free text>              annotations: "Recording data for X", "Recording closed <date>", ...

A UDS request's payload is the ASCII of that hex (e.g. "22F190" = ReadDataByIdentifier F190).
Responses reassemble by concatenating indexed segments (or, single-frame, the bare hex).
Everything streams so multi-hundred-MB logs stay cheap.
"""
import re

_LINE = re.compile(r'^(\d{2}:\d{2}:\d{2}\.\d{3}) ([SR]): ([0-9A-Fa-f]*)$')
_SEG  = re.compile(r'^([0-9A-Fa-f]):([0-9A-Fa-f]+)$')
_DATE = re.compile(r'(\d{4}/\d{2}/\d{2})')
_REC  = re.compile(r'Recording data for (.+)')


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


def _finish(seg, plain):
    return ("".join(seg[k] for k in sorted(seg)) if seg else plain).upper()


def iter_exchanges(path):
    """Yield dicts for each completed UDS request:
       {ts, date, addr, module, req, resp}  (req/resp are uppercase hex; resp may be '')."""
    addr, module, date = "?", None, "????/??/??"
    pend = None  # {'ts','req','seg','plain'}
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
        pay = ascii_of(payhex).strip().upper().replace(" ", "")
        if sr == "S":
            if pend:
                yield {"ts": pend["ts"], "date": date, "addr": addr,
                       "module": module, "req": pend["req"],
                       "resp": _finish(pend["seg"], pend["plain"])}
                pend = None
            if pay.startswith("ATSH"):
                addr = pay[4:]
                continue
            if pay.startswith(("AT", "ST")) or not re.fullmatch(r"[0-9A-F]+", pay) or len(pay) < 2:
                continue
            pend = {"ts": ts, "req": pay, "seg": {}, "plain": ""}
        elif sr == "R" and pend is not None:
            for part in pay.split("\r") if "\r" in pay else [pay]:
                part = part.strip()
                if not part or part == ">":
                    continue
                sm = _SEG.match(part)
                if sm:
                    pend["seg"][int(sm.group(1), 16)] = sm.group(2)
                elif re.fullmatch(r"[0-9A-F]{2,}", part):
                    pend["plain"] += part
            if ">" in payhex or ">" in pay:
                yield {"ts": pend["ts"], "date": date, "addr": addr,
                       "module": module, "req": pend["req"],
                       "resp": _finish(pend["seg"], pend["plain"])}
                pend = None
    if pend:
        yield {"ts": pend["ts"], "date": date, "addr": addr,
               "module": module, "req": pend["req"],
               "resp": _finish(pend["seg"], pend["plain"])}


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
