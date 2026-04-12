# dd-trace-py Performance Improvement Opportunities

Identified via static analysis of the tracing hot paths. Ordered by impact within each category.

---

## HIGH Impact

### 1. `INT_TYPES` tuple allocated on every `set_tag()` call
**File:** `ddtrace/_trace/span.py:228`

`set_tag()` is the single most-called method in the library. A tuple is created fresh on every invocation:
```python
# Current — new tuple object on every call:
INT_TYPES = (net.TARGET_PORT,)
if key in INT_TYPES and not val_is_an_int:
```
```python
# Fix — module-level constant:
_INT_KEY_TYPES = frozenset([net.TARGET_PORT])
```

---

### 2. Double dict lookup in `_set_attribute()` and `set_metric()`
**File:** `ddtrace/_trace/span.py:268-269, 292-294, 298-300, 303-304, 313-314, 396-397`

Every string tag write does a membership test then a delete — two hash lookups for the same key. This pattern appears four times across the type-dispatch branches of `_set_attribute()`, and once in `set_metric()`:
```python
# Current — 2 lookups:
if key in self._metrics:
    del self._metrics[key]
```
```python
# Fix — 1 lookup:
self._metrics.pop(key, None)
```

---

### 3. Double dict lookup in `_get_attribute()`
**File:** `ddtrace/_trace/span.py:327-330`

```python
# Current — 2 lookups per dict (check + fetch):
if key in self._meta:
    return self._meta[key]
elif key in self._metrics:
    return self._metrics[key]
```
```python
# Fix — 1 lookup per dict using a sentinel:
_MISSING = object()
v = self._meta.get(key, _MISSING)
if v is not _MISSING:
    return v
return self._metrics.get(key)
```

---

### 4. Two list allocations in `_get_metas_to_propagate()`
**File:** `ddtrace/internal/utils/__init__.py:78-79`

Called on every child span in a distributed trace. Currently creates a full snapshot list, then filters it into a second list:
```python
# Current — 2 allocations:
items = list(context._meta.items())
return [(k, v) for k, v in items if isinstance(k, str) and k.startswith("_dd.p.")]
```
```python
# Fix — 1 allocation, hold lock during iteration:
with context._lock:
    return [(k, v) for k, v in context._meta.items()
            if isinstance(k, str) and k.startswith("_dd.p.")]
```

---

### 5. `_traceparent` property recomputes string on every access
**File:** `ddtrace/_trace/context.py:151-157`

Called on every W3C header injection. Does `str.split()`, f-string formatting, and property evaluation on each call with no caching:
```python
# Current — full recompute every access:
trace_id = tp.split("-")[1]          # or
trace_id = f"{self.trace_id:032x}"
return f"00-{trace_id}-{self.span_id:016x}-{self._traceflags}"
```
**Fix:** Cache the result in `_traceparent_cached`; invalidate when `span_id` or `sampling_priority` changes.

---

## MEDIUM Impact

### 6. `_update_tags_from_context` double-indexes context dicts
**File:** `ddtrace/_trace/span.py:152-155`

Iterates keys then accesses values separately — two dict lookups per item:
```python
# Current:
for tag in self.context._meta:
    self._meta.setdefault(tag, self.context._meta[tag])
```
```python
# Fix:
for tag, val in self.context._meta.items():
    self._meta.setdefault(tag, val)
```

---

### 7. Regex compiled on every `_tracestate` access
**File:** `ddtrace/_trace/context.py:171`

`re.sub()` with a string pattern recompiles (or re-looks-up from cache) on every call. Python's `re` module does cache recent patterns, but a pre-compiled object is unambiguously faster:
```python
# Current:
ts_w_out_dd = re.sub("dd=(.+?)(?:,|$)", "", ts)
```
```python
# Fix — module-level:
_DD_TRACESTATE_RE = re.compile(r"dd=(.+?)(?:,|$)")
# then:
ts_w_out_dd = _DD_TRACESTATE_RE.sub("", ts)
```

---

### 8. `chain()` iterator created on every span start/finish
**File:** `ddtrace/_trace/tracer.py:561, 597`

Two `chain()` objects plus a temporary 1-element list are allocated on every single span start and finish:
```python
# Current — allocates on every call:
for p in chain(self._span_processors, SpanProcessor.__processors__, [self._span_aggregator]):
```
**Fix:** Pre-compute and cache the flat list; invalidate only when processors change.

---

### 9. Processor `chain()` rebuilt on every trace flush
**File:** `ddtrace/_trace/processor/__init__.py:394-398`

The last three processors never change after `__init__`, but a new `chain()` + anonymous list are created for every flushed trace:
```python
# Current:
for tp in chain(
    self.dd_processors,
    self.user_processors,
    [self.sampling_processor, self.tags_processor, self.service_name_processor],
):
```
```python
# Fix — build once in __init__:
self._all_processors = (
    self.dd_processors + self.user_processors +
    [self.sampling_processor, self.tags_processor, self.service_name_processor]
)
```

---

### 10. `_Trace.remove_finished()` iterates span list twice
**File:** `ddtrace/_trace/processor/__init__.py:278-280`

```python
# Current — two full passes:
finished = [s for s in self.spans if s.duration_ns is not None]
if finished:
    self.spans[:] = [s for s in self.spans if s.duration_ns is None]
```
```python
# Fix — single pass partition:
finished, remaining = [], []
for s in self.spans:
    (finished if s.duration_ns is not None else remaining).append(s)
self.spans[:] = remaining
```

---

### 11. `core.dispatch` allocates a tuple even when no listeners
**File:** `ddtrace/_trace/tracer.py:564, 593`

Both `trace.span_start` and `trace.span_finish` fire on every span. When AppSec/remote-config listeners are absent, the tuple `(span,)` is still allocated before the early-exit check inside `dispatch()`:
```python
# Fix — guard the allocation:
if core.has_listeners("trace.span_start"):
    core.dispatch("trace.span_start", (span,))
```

---

### 12. Span link dedup via O(n) list scan
**File:** `ddtrace/_trace/span.py:686`

Builds a full list of span IDs just to find one duplicate:
```python
# Current — O(n) list creation + O(n) search:
existing_link_idx_with_same_span_id = [link.span_id for link in self._links].index(link.span_id)
```
```python
# Fix — generator with early exit:
existing_idx = next(
    (i for i, l in enumerate(self._links) if l.span_id == link.span_id), -1
)
```
Or maintain a `dict[span_id → index]` for O(1) lookup.

---

### 13. `any()` over a list comprehension instead of a generator
**File:** `ddtrace/_trace/span.py:500`

The list comprehension evaluates all elements before `any()` can short-circuit:
```python
# Current — builds full list first:
any([issubclass(exc_type, e) for e in self._ignored_exceptions])
```
```python
# Fix — short-circuits on first match:
any(issubclass(exc_type, e) for e in self._ignored_exceptions)
```

---

### 14. `sum()` over metrics dict on every span finish batch
**File:** `ddtrace/_trace/processor/__init__.py:479`

Re-sums all values in the metrics counter dict on every `_queue_span_count_metrics()` call instead of maintaining a running total.

**Fix:** Keep a separate `self._span_metrics_total: dict[str, int]` counter and increment it alongside the per-integration dict.

---

### 15. `defaultdict` auto-creates empty `_Trace` for unknown `trace_id`
**File:** `ddtrace/_trace/processor/__init__.py:323, 350-351`

```python
self._traces: defaultdict[int, _Trace] = defaultdict(lambda: _Trace())
...
trace = self._traces[trace_id]  # silently creates _Trace() on miss
```
Any lookup of a non-existent `trace_id` allocates a `_Trace` object. The subsequent `if trace_id not in self._traces: return` check at line 367 shows this is already guarded — but the `on_span_start` path at line 351 uses the defaultdict auto-create unconditionally.

**Fix:** Use a regular dict with an explicit `setdefault` or `get`+create pattern so object creation is visible and intentional.

---

### 16. Lock contention in `SpanAggregator.on_span_finish()`
**File:** `ddtrace/_trace/processor/__init__.py:359-380`

Telemetry queuing (`_queue_span_count_metrics`) runs inside the `RLock` critical section even though it doesn't need shared state. On high-concurrency workloads, multiple threads finishing spans simultaneously compete for the lock while telemetry is being queued.

**Fix:** Move `_queue_span_count_metrics()` calls outside the `with self._lock:` block.

---

## LOW Impact

### 17. Redundant `iter()` in `set_tags()`
**File:** `ddtrace/_trace/span.py:367`

```python
for k, v in iter(tags.items()):   # iter() is redundant
```
`for ... in dict.items()` already yields an iterator. Remove the `iter()` wrapper.

---

### 18. `math.isnan(v) or math.isinf(v)` → `not math.isfinite(v)`
**File:** `ddtrace/_trace/span.py:295, 392`

Two function calls where one suffices:
```python
# Current:
if math.isnan(value) or math.isinf(value):

# Fix:
if not math.isfinite(value):
```

---

### 19. `%` string formatting in `_set_sampling_decision_maker`
**File:** `ddtrace/_trace/span.py:211`

```python
# Current:
value = "-%d" % sampling_mechanism

# Fix:
value = f"-{sampling_mechanism}"
```

---

### 20. `.format()` instead of f-strings in `_dd_id_to_b3_id`
**File:** `ddtrace/propagation/http.py:141-142`

f-strings compile to faster bytecode than `.format()`. Called on every B3 header injection:
```python
# Current:
return "{:032x}".format(dd_id)
return "{:016x}".format(dd_id)

# Fix:
return f"{dd_id:032x}"
return f"{dd_id:016x}"
```

---

### 21. `get_tags()` / `get_metrics()` return full dict copies
**File:** `ddtrace/_trace/span.py:360, 423`

These create a full shallow copy of the meta/metrics dicts on every call. If the callers don't mutate the result, the copies are unnecessary. Consider returning a read-only view (`types.MappingProxyType`) or documenting that the returned dict must not be mutated so the copy can be removed.

---

### 22. Log-level check missing before building large string list
**File:** `ddtrace/_trace/processor/__init__.py:457-465`

At shutdown, builds a list of f-strings for all unsent spans unconditionally — even if the `WARNING` log level is not enabled:
```python
# Fix — guard with level check:
if log.isEnabledFor(logging.WARNING):
    unsent_spans = [...]
    log.warning(...)
```

---

## Summary Table

| # | File | Lines | Description | Impact |
|---|------|-------|-------------|--------|
| 1 | `_trace/span.py` | 228 | `INT_TYPES` tuple allocated per `set_tag()` call | **HIGH** |
| 2 | `_trace/span.py` | 268, 292, 298, 303, 313, 396 | Double dict lookup: `in` + `del` → `pop()` | **HIGH** |
| 3 | `_trace/span.py` | 327–330 | Double dict lookup in `_get_attribute()` | **HIGH** |
| 4 | `internal/utils/__init__.py` | 78–79 | Two list allocs in `_get_metas_to_propagate()` | **HIGH** |
| 5 | `_trace/context.py` | 151–157 | `_traceparent` recomputed on every access | **HIGH** |
| 6 | `_trace/span.py` | 152–155 | Double-index in `_update_tags_from_context` | MEDIUM |
| 7 | `_trace/context.py` | 171 | Regex not pre-compiled in `_tracestate` | MEDIUM |
| 8 | `_trace/tracer.py` | 561, 597 | `chain()` + list alloc per span start/finish | MEDIUM |
| 9 | `_trace/processor/__init__.py` | 394–398 | Processor `chain()` rebuilt per trace flush | MEDIUM |
| 10 | `_trace/processor/__init__.py` | 278–280 | `remove_finished()` iterates span list twice | MEDIUM |
| 11 | `_trace/tracer.py` | 564, 593 | `core.dispatch` tuple alloc with no listeners | MEDIUM |
| 12 | `_trace/span.py` | 686 | Span link dedup via O(n) list scan | MEDIUM |
| 13 | `_trace/span.py` | 500 | `any([...])` list comprehension vs generator | MEDIUM |
| 14 | `_trace/processor/__init__.py` | 479 | `sum()` over metrics dict per flush | MEDIUM |
| 15 | `_trace/processor/__init__.py` | 323, 351 | `defaultdict` auto-creates `_Trace` on miss | MEDIUM |
| 16 | `_trace/processor/__init__.py` | 359–380 | Telemetry queuing inside lock in `on_span_finish` | MEDIUM |
| 17 | `_trace/span.py` | 367 | Redundant `iter()` in `set_tags()` | LOW |
| 18 | `_trace/span.py` | 295, 392 | `isnan + isinf` → `isfinite` | LOW |
| 19 | `_trace/span.py` | 211 | `%` formatting → f-string | LOW |
| 20 | `propagation/http.py` | 141–142 | `.format()` → f-string in `_dd_id_to_b3_id` | LOW |
| 21 | `_trace/span.py` | 360, 423 | `get_tags()`/`get_metrics()` copy entire dicts | LOW |
| 22 | `_trace/processor/__init__.py` | 457–465 | String list built before log-level check | LOW |
