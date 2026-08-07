"""Pure Spark helpers, deliberately outside the pipeline's source glob.

Nothing in this package imports `pyspark.pipelines`, so every function here runs on a plain local
Spark session with no workspace connection (ENV-006). The pipeline modules one directory up are
the opposite: they cannot be imported outside a Lakeflow runtime at all.

The split exists because the interesting failure modes in a dimensional model — fan-out on a
point-in-time join, overlapping validity windows, a dedupe that is not idempotent — are properties
of the *semantics*, not of the platform. Testing them should not require a pipeline, a cluster, or
a quota.
"""
