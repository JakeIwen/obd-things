import time

from lib import uds


def classify_session_response(session, response):
    """Require the positive SID and requested session echo; timing bytes may follow."""
    if not response:
        return "timeout"
    response = bytes(response)
    if len(response) >= 3 and response[:2] == bytes.fromhex("7F 10"):
        return "negative"
    if len(response) >= 2 and response[:2] == bytes((0x50, session)):
        return "positive_echo"
    return "unexpected"


def classify_tester_present_response(response):
    """Validate the non-suppressed TesterPresent response ``7E 00``."""
    if not response:
        return "timeout"
    response = bytes(response)
    if len(response) >= 3 and response[:2] == bytes.fromhex("7F 3E"):
        return "negative"
    if response == bytes.fromhex("7E 00"):
        return "positive_echo"
    return "unexpected"


def request_once(sock, payload, timeout, request_attempts=None, responses_received=None,
                 counter_key=None, retries=0):
    """Drain and make one UDS call while recording attempt/response semantics honestly.

    An attempt is counted immediately before ``uds.request``. A non-empty response is counted
    only after that call returns, so receive-side exceptions remain visible as attempts without
    being mislabeled as confirmed responses.
    """
    uds.drain(sock)
    if request_attempts is not None:
        request_attempts[counter_key] = request_attempts.get(counter_key, 0) + 1
    response, status = uds.request(sock, payload, timeout=timeout, retries=retries)
    if response and responses_received is not None:
        responses_received[counter_key] = responses_received.get(counter_key, 0) + 1
    return response, status


def tester_present(sock, timeout, request_attempts, responses_received, events):
    """Send, validate, and preserve one explicit-session keepalive result."""
    payload = bytes.fromhex("3E 00")
    started = time.monotonic()
    try:
        response, status = request_once(
            sock,
            payload,
            timeout,
            request_attempts=request_attempts,
            responses_received=responses_received,
            counter_key="tester_present",
        )
    except Exception as exc:
        events.append(
            {
                "request_hex": uds.hx(payload),
                "response_hex": None,
                "category": "transport_error",
                "validated_echo": False,
                "status": f"{type(exc).__name__}: {exc}",
                "negative_response": None,
                "elapsed_s": round(time.monotonic() - started, 3),
            }
        )
        raise RuntimeError("TesterPresent transport failure; explicit session is uncertain") from exc
    category = classify_tester_present_response(response)
    event = {
        "request_hex": uds.hx(payload),
        "response_hex": uds.hx(response) if response else None,
        "category": category,
        "validated_echo": category == "positive_echo",
        "status": status,
        "negative_response": uds.negative_response_details(response),
        "elapsed_s": round(time.monotonic() - started, 3),
    }
    events.append(event)
    if not event["validated_echo"]:
        raise RuntimeError(
            f"TesterPresent was not acknowledged with exact 7E 00 echo ({category}); "
            "explicit session is uncertain"
        )
    return event
