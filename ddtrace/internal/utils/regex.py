import re
import threading
from typing import Optional
from typing import Union

from ddtrace.internal.logger import get_logger


log = get_logger(__name__)

# Strings that trigger catastrophic backtracking in common ReDoS patterns such
# as (a+)+, (a*)+, or deeply nested alternation groups.  The trailing "!" is a
# non-matching character that forces full backtracking in vulnerable patterns.
_REDOS_PROBE_STRINGS: list[bytes] = [
    b"a" * 100 + b"!",
    b"=" * 100 + b"!",
    (b"aA") * 50 + b"!",
    b"a" * 200,
]

# How long (seconds) a probe run is allowed to take before we conclude the
# pattern is catastrophically slow.  500 ms is generous for legitimate patterns
# but easily exceeded by ReDoS triggers.
_VALIDATION_TIMEOUT_S = 0.5


def safe_compile_re(
    pattern: Union[str, bytes],
    flags: int = 0,
    default: Optional["re.Pattern"] = None,
    env_var: str = "",
) -> Optional["re.Pattern"]:
    """Compile *pattern* from operator-supplied configuration, rejecting it if
    it exhibits catastrophic backtracking on adversarial probe strings.

    Returns the compiled pattern on success, or *default* (typically ``None``)
    when the pattern is invalid or times out during probing.  A warning is
    logged in both failure cases.

    The probe runs in a daemon thread; if the thread is still alive after
    *_VALIDATION_TIMEOUT_S* seconds we abandon it and fall back to *default*.
    The leaked thread will eventually finish on its own (or exit with the
    process because it is a daemon).
    """
    label = f" ({env_var})" if env_var else ""

    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        log.warning("Regex pattern%s is invalid, ignoring it: %s", label, exc)
        return default

    # Build probe strings in the same type as the pattern.
    is_bytes = isinstance(pattern, (bytes, bytearray))
    probes: list[Union[str, bytes]] = (
        _REDOS_PROBE_STRINGS if is_bytes else [s.decode("ascii") for s in _REDOS_PROBE_STRINGS]
    )

    outcome: list[Optional[Exception]] = [None]  # None = success, Exception = error
    timed_out = False

    def _probe() -> None:
        try:
            for s in probes:
                compiled.search(s)
        except Exception as exc:
            outcome[0] = exc

    t = threading.Thread(target=_probe, daemon=True, name="ddtrace.regex_probe")
    t.start()
    t.join(timeout=_VALIDATION_TIMEOUT_S)

    if t.is_alive():
        timed_out = True

    if timed_out:
        log.warning(
            "Regex pattern%s appears to have catastrophic backtracking and will be ignored. Review the value of %s.",
            label,
            env_var or "the pattern environment variable",
        )
        return default

    if outcome[0] is not None:
        log.warning("Regex pattern%s raised an error during validation: %s", label, outcome[0])
        return default

    return compiled
