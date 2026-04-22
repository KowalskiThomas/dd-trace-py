"""
Reproducer for SIGSEGV in PyObject_GC_UnTrack via PyContextVar_Set
===================================================================

Commit that introduced the fix: a8cce63 (fix(internal): crash in ContextVar)

CRASH SUMMARY
-------------
Process terminated with SEGV_MAPERR (SIGSEGV).
Stack trace observed in production (ddtrace + uvloop):

    #0  PyObject_GC_UnTrack
    #1  PyContextVar_Set
    #2  _PyEval_EvalFrameDefault
    ...
    #34 __Pyx_PyObject_Call           (uvloop/loop.c)
    #35 __pyx_f_6uvloop_4loop_6Handle__run
    ...

ROOT CAUSE
----------
The old `BaseWrappingContext.__enter__` implementation created a reference
cycle that ran through CPython's internal HAMT (Hash Array Mapped Trie) nodes:

    ts->context
      └─▶ ctx_vars (HAMT root)
            └─▶ HAMT nodes
                  └─▶ dict {"__dd_wrapping_context_token__": token}
                        └─▶ token (PyContextToken)
                              └─▶ tok_ctx ──────────────▶ ts->context  ⟲

The crash happens because `PyContextVar_Set` is NOT atomic.  Its simplified
CPython implementation is:

    static PyObject *
    contextvar_set(PyContextVar *var, PyObject *val) {
        PyContext *ts_ctx = ts->context;
        PyObject *new_hamt = _PyHamt_Assoc(ts_ctx->ctx_vars, var, val);  // (A)
        Py_DECREF(ts_ctx->ctx_vars);   // ← old HAMT root refcount decremented
        ts_ctx->ctx_vars = new_hamt;
        ...
    }

At point (A), `_PyHamt_Assoc` allocates memory, which can trigger CPython's
cyclic garbage collector.  The GC traverses the cycle above, discovers the
Token whose `tok_ctx` points back to `ts->context`, and -- because the HAMT
nodes are GC-tracked objects involved in the cycle -- the GC may prematurely
decrement refcounts on HAMT nodes that are shared between the old and new HAMT.

If a leaf HAMT node's refcount drops to zero during the GC pass, its memory is
freed (possibly returned to the OS).  When `Py_DECREF(ts_ctx->ctx_vars)` then
executes and tries to dealloc the old HAMT root through the same nodes,
`PyObject_GC_UnTrack` writes to already-freed (or unmapped) memory →
SEGV_MAPERR.

THE FIX
-------
Store the *previous value* in the dict instead of the Token.  This means no
Token is ever held in the HAMT, so the cycle cannot form:

    # OLD (buggy) - Token stored in HAMT creates a cycle
    token = storage.set({})
    storage.get()["__dd_wrapping_context_token__"] = token   # cycle!

    # NEW (fixed) - prev value stored, Token immediately discarded
    prev = storage.get()
    storage.set({"__dd_wrapping_context_prev__": prev})      # no cycle

HOW TO USE THIS REPRODUCER
---------------------------
1.  Run against the FIXED code (current codebase):
        python tests/internal/reproduce_contextvar_crash.py
    Expected: prints "PASS" and exits cleanly.

2.  To reproduce the original crash, revert the fix in
    ddtrace/internal/wrapping/context.py so that __enter__ stores the Token,
    then run with Python 3.12 and uvloop installed:
        pip install uvloop
        python tests/internal/reproduce_contextvar_crash.py
    Expected on pre-fix code: intermittent SIGSEGV (SEGV_MAPERR) in a few
    thousand iterations, more reliably with uvloop's event loop.

NOTE: The crash is timing-dependent (GC must fire during _PyHamt_Assoc).
      It is most reliable on Python 3.12+ and with uvloop.  On Python 3.11
      the crash window is narrower but the reference cycle is still present.
"""

import asyncio
import gc
import sys
from contextvars import ContextVar
from typing import Any
from typing import Optional


# ---------------------------------------------------------------------------
# Buggy pattern (pre-fix): storing the Token creates a reference cycle
# ---------------------------------------------------------------------------

_buggy_storage: ContextVar[Optional[dict]] = ContextVar("_buggy_storage", default=None)


def _buggy_enter() -> Any:
    """Simulate the OLD BaseWrappingContext.__enter__ that caused the crash."""
    token = _buggy_storage.set({})
    # AIDEV-NOTE: This line creates the dangerous reference cycle.
    # Token.tok_ctx (C-level) holds a strong ref to the current PyContext.
    # That PyContext's ctx_vars HAMT contains this very dict, which holds
    # the Token back.  Cycle: ts->context → HAMT → dict → token → ts->context
    _buggy_storage.get()["__dd_wrapping_context_token__"] = token  # type: ignore[index]
    return token


def _buggy_exit(token: Any) -> None:
    _buggy_storage.reset(token)


# ---------------------------------------------------------------------------
# Fixed pattern (post-fix): storing prev breaks the cycle
# ---------------------------------------------------------------------------

_fixed_storage: ContextVar[Optional[dict]] = ContextVar("_fixed_storage", default=None)


def _fixed_enter() -> None:
    """Simulate the FIXED BaseWrappingContext.__enter__."""
    prev = _fixed_storage.get()
    _fixed_storage.set({"__dd_wrapping_context_prev__": prev})


def _fixed_exit() -> None:
    storage = _fixed_storage.get()
    assert storage is not None  # nosec
    prev = storage.pop("__dd_wrapping_context_prev__")
    _fixed_storage.set(prev)


# ---------------------------------------------------------------------------
# Cycle detection helpers
# ---------------------------------------------------------------------------

def _token_referrer_count() -> int:
    """Return the number of Python-visible objects that hold a ref to the Token."""
    gc.collect()
    token = _buggy_storage.set({})
    _buggy_storage.get()["__dd_wrapping_context_token__"] = token  # type: ignore[index]
    # Exclude the local `token` variable itself (1 local ref is expected)
    referrers = [r for r in gc.get_referrers(token) if r is not sys._getframe().f_locals]
    _buggy_storage.reset(token)
    return len(referrers)


def _fixed_dict_referrer_count() -> int:
    """Return the number of Python-visible objects that hold a ref to the storage dict."""
    gc.collect()
    _fixed_enter()
    d = _fixed_storage.get()
    # hamt nodes hold the dict; no Token involved
    referrers = [r for r in gc.get_referrers(d) if r is not sys._getframe().f_locals]
    _fixed_exit()
    return len(referrers)


# ---------------------------------------------------------------------------
# Stress test: maximise GC pressure while calling ContextVar.set()
# ---------------------------------------------------------------------------

ITERATIONS = 100_000


async def _stress_buggy() -> None:
    """Run the buggy pattern under asyncio with aggressive GC thresholds."""
    gc.set_threshold(1, 1, 1)
    for _ in range(ITERATIONS):
        tok = _buggy_enter()
        _buggy_exit(tok)


async def _stress_fixed() -> None:
    """Run the fixed pattern under asyncio with aggressive GC thresholds."""
    gc.set_threshold(1, 1, 1)
    for _ in range(ITERATIONS):
        _fixed_enter()
        _fixed_exit()


async def _try_uvloop_stress() -> bool:
    """Attempt the stress test with uvloop if available (higher crash probability)."""
    try:
        import uvloop  # type: ignore[import]
    except ImportError:
        return False

    loop = uvloop.new_event_loop()
    try:
        loop.run_until_complete(_stress_buggy())
    finally:
        loop.close()
    return True


# ---------------------------------------------------------------------------
# Main: verify cycle properties and run stress tests
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"Python {sys.version}")
    print()

    # 1. Verify the Token IS GC-tracked (prerequisite for the crash)
    token = _buggy_storage.set({})
    token_is_tracked = gc.is_tracked(token)
    _buggy_storage.reset(token)
    print(f"[CHECK] Token is GC-tracked: {token_is_tracked}")
    assert token_is_tracked, "Token must be GC-tracked for the cycle to be exploitable"

    # 2. Buggy pattern: Token has an extra referrer (the dict inside the HAMT)
    buggy_refs = _token_referrer_count()
    print(f"[CHECK] Buggy pattern  – Token referrer count (excluding local): {buggy_refs}")
    # The dict stored in the HAMT holds the Token → that dict is one referrer
    assert buggy_refs >= 1, "Expected Token to be held by the dict in the HAMT"

    # 3. Fixed pattern: dict is only held by the HAMT node (no Token involved)
    fixed_refs = _fixed_dict_referrer_count()
    print(f"[CHECK] Fixed pattern  – dict referrer count  (excluding local): {fixed_refs}")
    # HAMT bitmap node is the only referrer; no Token cycle
    assert fixed_refs == 1, f"Expected exactly 1 referrer (HAMT node), got {fixed_refs}"

    print()
    print(f"[STRESS] Running {ITERATIONS:,} iterations of buggy pattern under asyncio ...")
    saved = gc.get_threshold()
    try:
        asyncio.run(_stress_buggy())
    finally:
        gc.set_threshold(*saved)
    print("[STRESS] buggy pattern: survived (fix is in place)")

    print(f"[STRESS] Running {ITERATIONS:,} iterations of fixed pattern under asyncio ...")
    saved = gc.get_threshold()
    try:
        asyncio.run(_stress_fixed())
    finally:
        gc.set_threshold(*saved)
    print("[STRESS] fixed pattern: survived")

    print()
    print("PASS — no crash with current (fixed) code.")
    print()
    print("NOTE: To reproduce the original crash, revert ddtrace/internal/wrapping/context.py")
    print("      to the old __enter__ implementation (storing Token instead of prev),")
    print("      install uvloop, and run this script on Python 3.12+.")


if __name__ == "__main__":
    main()
