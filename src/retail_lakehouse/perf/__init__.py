"""Skew and spill performance lab.

Everything in this package targets a *serverless SQL warehouse*, because that is the only
compute surface on Databricks Free Edition that emits per-query metrics we can read back
programmatically. There is no Spark UI on serverless, so the evidence layer is the query
history API plus `system.query.history`.

The package is deliberately measurement-first: `runner` executes a labelled statement and
returns a `RunMetrics` record; nothing in here prints a conclusion that is not backed by one.
"""
