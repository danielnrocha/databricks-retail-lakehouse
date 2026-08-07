"""Silver — conformed entities, SCD Type 2 dimensions, and the quality gate.

The package is split in two, and the split is enforced by the pipeline's library glob rather than
by convention:

* `silver/pipeline/` — **Lakeflow pipeline source files.** Each imports `pyspark.pipelines`,
  expects a session the runtime injects, and cannot be imported outside one. The glob is
  `silver/pipeline/**`, so adding a file there adds it to the pipeline graph.
* `silver/lib/` — pure Spark helpers with no pipeline import, runnable on a local session with no
  workspace connection (ENV-006), and deliberately outside that glob.

Two conventions hold across every module here:

* **The catalog is never a literal.** It arrives as `dng.catalog` on the Spark conf, injected by
  the bundle target (ADR-0002, ENV-001).
* **Quality rules are never a literal either.** They are read from `<catalog>.ops.dq_rules` at
  graph-construction time. If that table is missing the pipeline fails rather than running with no
  gate, because a quality layer that degrades quietly to "no checks" is worse than none at all —
  it produces a green run.
"""
