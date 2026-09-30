"""Length-preserving VIN redaction for saved UDS identity evidence."""

VIN_DID = 0xF190
VIN_ALLOWED = frozenset(b"ABCDEFGHJKLMNPRSTUVWXYZ0123456789")
VIN_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)
VIN_TRANSLITERATION = {
    **{ord(str(number)): number for number in range(10)},
    **{
        ord(letter): value
        for letter, value in {
            "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
            "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
            "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
        }.items()
    },
}


def valid_vin_checksum(candidate):
    candidate = bytes(candidate).upper()
    if len(candidate) != 17 or any(byte not in VIN_ALLOWED for byte in candidate):
        return False
    total = sum(VIN_TRANSLITERATION[byte] * weight for byte, weight in zip(candidate, VIN_WEIGHTS))
    expected = ord("X") if total % 11 == 10 else ord(str(total % 11))
    return candidate[8] == expected


def mask_embedded_vins(data):
    """Redact checksum-valid VIN windows without changing record length or byte offsets."""
    original = bytes(data)
    masked = bytearray(original)
    for start in range(max(0, len(original) - 16)):
        candidate = original[start:start + 17]
        if valid_vin_checksum(candidate):
            masked[start + 11:start + 17] = b"######"
    return bytes(masked)


def redact_response_vins(did, response):
    """Return ``(safe_response, redacted)`` for a UDS identity response."""
    if response is None:
        return None, False
    original = bytes(response)
    safe = mask_embedded_vins(original)
    if (
        did == VIN_DID
        and len(original) >= 20
        and original[:3] == bytes((0x62, VIN_DID >> 8, VIN_DID & 0xFF))
    ):
        # Start from the already-scrubbed buffer so a second VIN elsewhere in a composite F190
        # response is not reintroduced while masking the direct 17-byte field.
        direct = bytearray(safe)
        direct[3 + 11:3 + 17] = b"######"
        safe = bytes(direct)
    return safe, safe != original
