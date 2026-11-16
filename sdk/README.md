# ShadowHeal SDK transport

The SDK provides three Layer 1 transport primitives:

- `fingerprint_error(error)` creates a deterministic SHA-256 fingerprint from
  an error mapping.
- `DuplicateErrorCache` suppresses duplicate fingerprints in memory.
- `ErrorReporter` queues unique errors and sends them with a daemon worker so
  reporting does not block the caller.

```python
from shadowheal_sdk import ErrorReporter

reporter = ErrorReporter("http://localhost:8000/api/errors")
try:
    ...
except Exception as error:
    reporter.report({"type": type(error).__name__, "message": str(error)})
finally:
    reporter.close()
```
