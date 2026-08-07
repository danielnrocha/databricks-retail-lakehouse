"""Lakeflow pipeline source files for the silver layer.

Every module here imports `pyspark.pipelines`, expects a session the runtime injects, and cannot
be imported outside a pipeline. They are listed in `resources/bronze_pipeline.yml` under a
`glob: ../src/retail_lakehouse/silver/pipeline/**`.

The directory exists because the glob had to be able to exclude `silver/lib/`. The pipelines API
rejects single-asterisk patterns — "Single asterisk glob pattern is not supported in included
path ... Use a double asterisk" — and a `libraries` list may contain either globs or explicit
file entries but not both, so `silver/**` would have swept the pure helpers into the graph.
Splitting the directory is the only way to say "these files run on a pipeline, those run on a
laptop" in a language the API accepts.
"""
