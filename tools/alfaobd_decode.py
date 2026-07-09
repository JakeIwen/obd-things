#!/usr/bin/env python3
"""Decode an AlfaOBD `*_Debug.bin` (Preferences -> "Debug Data recording") into plain text.

Format of the .bin: an ASCII hex string whose decoded bytes are the ones-complement
(XOR 0xFF) of the log text. So:  file -> bytes.fromhex() -> XOR 0xFF -> UTF-8/latin-1 text.
The text is a timestamped ELM/STN adapter trace ("HH:MM:SS.mmm S:/R: <hex>", payloads are
hex-encoded ASCII; multi-frame responses come as a length line + indexed 0:/1:/2: segments).

Generic (any AlfaOBD debug bin, any vehicle). Streams in chunks so multi-hundred-MB files
stay cheap on a Pi. Parsing the decoded text into UDS/DIDs lives in
projects/ecu_mapping/ (alfalog.py).

Usage: alfaobd_decode.py <in.bin> [out.txt]      # out defaults to <in>.decoded.txt
"""
import sys

_HEX = set(b"0123456789abcdefABCDEF")
_CHUNK = 1 << 22  # 4 MiB of hex per read


def decode(src_path, out_path):
    carry = b""
    n_in = n_out = 0
    with open(src_path, "rb") as f, open(out_path, "wb") as g:
        while True:
            raw = f.read(_CHUNK)
            if not raw:
                break
            n_in += len(raw)
            buf = carry + bytes(c for c in raw if c in _HEX)
            if len(buf) & 1:              # keep hex pairs aligned across chunks
                carry, buf = buf[-1:], buf[:-1]
            else:
                carry = b""
            g.write(bytes(b ^ 0xFF for b in bytes.fromhex(buf.decode("ascii"))))
            n_out += (len(buf) // 2)
    return n_in, n_out


def main(argv):
    if not (2 <= len(argv) <= 3):
        sys.exit(__doc__)
    src = argv[1]
    out = argv[2] if len(argv) == 3 else src.rsplit(".", 1)[0] + ".decoded.txt"
    n_in, n_out = decode(src, out)
    print(f"read {n_in:,} hex bytes -> wrote {n_out:,} decoded bytes to {out}")


if __name__ == "__main__":
    main(sys.argv)
