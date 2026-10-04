"""Wire constants shared by the telemetry HTTP server and client."""

MAX_REQUEST_BYTES = 4096
MAX_RESPONSE_BYTES = 1024 * 1024
OBSERVATION_DEADLINE_HEADER = "X-Van-Telemetry-Deadline-Monotonic"
MAX_OBSERVATION_QUEUE_SECONDS = 1.0
