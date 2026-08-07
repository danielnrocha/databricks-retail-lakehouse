"""Two-layer data quality.

Layer A — **DQX profiling** (`profile_candidates.py`). Generates *candidate* rules from observed
distributions. Candidates are written to a review file and are never applied. QLT-005.

Layer B — **Reviewed rules** (`rules.py`), published to a Unity Catalog table by `publish.py` and
read at pipeline graph-construction time by the silver modules. QLT-001.

The separation is the whole point. A profiler measures what *is*; a rule asserts what *must be*.
Turning the first into the second without a human in between encodes last Tuesday's anomalies as
law — see finding F6 in `docs/architecture/dataset-findings.md`.
"""
