# Traceability matrix

> Machine-checked by `scripts/check_traceability.py`, run in CI.
> A requirement in `REQUIREMENTS.md` with no row here is a **build failure**.
> A row here pointing at a test that does not exist is a **build failure**.
>
> The point is not bookkeeping. It is that "we have tests" and "our requirements are tested" are
> different claims, and only the second one is worth anything.

**Status legend:** `PASSING` · `PLANNED` (row registered, test not yet written — allowed only while
the owning phase is open) · `WAIVED` (requires paid-tier features; see note).

| Requirement | Test | Kind | Status |
|---|---|---|---|
| ING-001 | `tests/integration/test_autoloader_incremental.py::test_second_run_processes_only_new_files` | integration | PLANNED |
| ING-002 | `tests/unit/test_lineage_columns.py::test_bronze_lineage_columns_non_null` | unit | PLANNED |
| ING-003 | `tests/integration/test_schema_drift.py::test_additive_column_survives` | integration | PASSING |
| ING-004 | `tests/integration/test_schema_drift.py::test_incompatible_field_lands_in_rescued_data` | integration | PASSING |
| ING-005 | `tests/integration/test_cdc_idempotency.py::test_replay_window_is_idempotent` | integration | PLANNED |
| ING-006 | `tests/integration/test_watermark.py::test_late_within_watermark_is_counted` | integration | PLANNED |
| ING-007 | `tests/integration/test_watermark.py::test_late_beyond_watermark_is_recorded_not_silent` | integration | PLANNED |
| GEN-001 | `tests/unit/test_generator_sampler.py::test_uniform_draw_preserves_store_distribution` | unit | PASSING |
| GEN-002 | `tests/unit/test_generator_sampler.py::test_baskets_are_drawn_intact` | unit | PASSING |
| GEN-003 | `tests/unit/test_generator_sampler.py::test_generation_is_deterministic` | unit | PASSING |
| GEN-004 | `tests/unit/test_generator_emit.py::test_control_run_has_no_stress_events` | unit | PASSING |
| GEN-005 | `tests/unit/test_generator_emit.py::test_every_event_labelled_synthetic` | unit | PASSING |
| QLT-001 | `tests/unit/test_quality_rules.py::test_every_silver_table_has_rules` | unit | PASSING |
| QLT-002 | `tests/integration/test_quarantine.py::test_quarantine_row_carries_reason` | integration | PLANNED |
| QLT-003 | `tests/integration/test_quarantine.py::test_row_conservation` | integration | PLANNED |
| QLT-004 | `tests/integration/test_referential_integrity.py::test_no_orphan_facts` | integration | PLANNED |
| QLT-005 | `tests/unit/test_quality_rules.py::test_generated_rules_require_review` | unit | PASSING |
| QLT-006 | `tests/integration/test_dq_metrics.py::test_metrics_written_per_run` | integration | PLANNED |
| QLT-007 | `tests/integration/test_dq_metrics.py::test_regression_fails_gate` | integration | PLANNED |
| MOD-001 | `tests/unit/test_scd2.py::test_attribute_change_creates_version` | unit | PASSING |
| MOD-002 | `tests/unit/test_scd2.py::test_no_overlapping_validity_windows` | unit | PASSING |
| MOD-003 | `tests/unit/test_scd2.py::test_point_in_time_join_is_one_to_one` | unit | PASSING |
| MOD-004 | `tests/integration/test_gold_determinism.py::test_rerun_produces_identical_checksums` | integration | PLANNED |
| MOD-005 | `tests/unit/test_metrics_register.py::test_no_duplicate_kpi_definitions` | unit | PLANNED |
| MOD-006 | `tests/unit/test_naming_conventions.py::test_monetary_columns_declare_currency` | unit | PLANNED |
| PRF-001 | `tests/integration/test_skew_lab.py::test_baseline_profile_shows_skew` | integration | PLANNED |
| PRF-002 | `tests/integration/test_skew_lab.py::test_mitigation_reduces_task_time_ratio` | integration | PLANNED |
| PRF-003 | `tests/integration/test_file_sizing.py::test_median_file_size_above_floor` | integration | PLANNED |
| PRF-004 | `tests/integration/test_shuffle.py::test_shuffle_write_reduced` | integration | PLANNED |
| PRF-005 | `tests/unit/test_perf_evidence_doc.py::test_every_claim_has_measurements` | unit | PASSING |
| PRF-006 | `tests/unit/test_table_properties.py::test_po_and_manual_optimize_disjoint` | unit | PASSING |
| ENV-001 | `tests/unit/test_no_hardcoded_catalog.py::test_no_literal_catalog_in_src` | unit | PASSING |
| ENV-002 | `tests/integration/test_bundle_targets.py::test_test_target_writes_only_to_test_catalog` | integration | PASSING |
| ENV-003 | `tests/unit/test_deploy_provenance.py::test_prod_deploy_references_tested_sha` | unit | PASSING |
| ENV-004 | `docs/architecture/production-delta.md#11` | integration | WAIVED |
| ENV-005 | `tests/integration/test_rollback.py::test_redeploy_prior_sha_restores_state` | integration | PASSING |
| ENV-006 | `tests/unit/test_offline_capable.py::test_unit_suite_needs_no_workspace` | unit | PASSING |
| MLR-001 | `tests/integration/test_ml_reproducibility.py::test_rerun_from_logged_params` | integration | PLANNED |
| MLR-002 | `tests/integration/test_model_gate.py::test_beats_recency_baseline` | integration | PLANNED |
| MLR-003 | `tests/unit/test_eval_dataset.py::test_eval_contains_no_synthetic_rows` | unit | PLANNED |
| MLR-004 | `tests/integration/test_model_gate.py::test_failing_model_cannot_be_promoted` | integration | PLANNED |
| MLR-005 | `tests/integration/test_drift_monitor.py::test_data_and_model_drift_reported_separately` | integration | PLANNED |
| MLR-006 | `tests/unit/test_feature_leakage.py::test_no_future_information_in_features` | unit | PLANNED |
| AGT-001 | `src/retail_lakehouse/agents/evaluate.py::gate` | integration | PASSING |
| AGT-002 | `tests/integration/test_agent_tracing.py::test_trace_logged_per_request` | integration | PLANNED |
| AGT-003 | `src/retail_lakehouse/agents/evaluate.py::judge` | integration | PASSING |
| AGT-004 | `src/retail_lakehouse/agents/evaluate.py::gate` | integration | PASSING |
| AGT-005 | `tests/integration/test_agent_monitoring.py::test_production_traces_sampled` | integration | PLANNED |
| AGT-006 | `src/retail_lakehouse/agents/evaluate.py::gate` | integration | PASSING |
| GOV-001 | `scripts/document_gold_columns.py::uncommented` | integration | PASSING |
| GOV-002 | `tests/integration/test_governance.py::test_lineage_terminates_at_source` | integration | PLANNED |
| GOV-003 | `tests/integration/test_governance.py::test_gold_tables_have_domain` | integration | PLANNED |
| GOV-004 | `tests/integration/test_governance.py::test_metric_terms_in_glossary` | integration | PLANNED |

---

## Why most rows read PLANNED

This matrix was written **before** the tests, on purpose. Writing it first forces the acceptance
criterion to be stated in terms a test can check, which is the moment most vague requirements
collapse — you discover you cannot say what "good data quality" would assert.

The rows flip to `PASSING` as each phase lands. The CI gate enforces two things from day one:
no requirement without a row, and no row pointing at a nonexistent test path. It does **not**
require every test to pass immediately, because that would force either fake tests or a
big-bang merge, and both are worse.

## A `PASSING` row is not proof the test is any good

Worth stating plainly, since this matrix could otherwise be read as a completeness score.

`GEN-005` went green while the emitter was producing 73% duplicates against a configured 0.5%,
because the duplicate test asserted only that *some* duplicates appeared. The requirement was
mapped, the test ran, the row was green, and the generator was badly wrong. It was caught by
running the thing and looking at the output, not by the suite.

Three separate rate parameters in `generator/config.py` shipped with the wrong units, and each
time the code matched the variable name. The fix was a test comparing configured against observed
for every rate at once — `test_configured_rates_match_observed_rates` — which is now the general
defence against that whole class.

The matrix tracks whether a requirement has an assertion behind it. It cannot tell you whether the
assertion is strong. Nothing can, except reading it.

## Waivers

Anything requiring service principals, multi-workspace, or private networking is marked `WAIVED`
with a pointer to `docs/architecture/production-delta.md`, which describes what the design would be
on a paid tier. A waiver is a documented gap; a silent omission is a lie.

**ENV-004 — a clean deploy is reproducible.** Waived on cost, with the cost measured rather than
estimated. The acceptance criterion is a deploy to an *empty* catalog from a fixed input snapshot
producing gold that matches a golden fixture, which requires a full medallion run over 2.6M
transaction lines and 36.8M causal rows. Free Edition's quota is shared account-wide and
exhausting it shuts down all compute for the rest of the day — including the environment the rest
of this repository is demonstrated in. See `production-delta.md` §11.

ENV-004 was separated from ENV-005 by measurement, not by grouping them as "the expensive CI/CD
ones". `bundle deploy` applies definitions without starting a pipeline update: three deploys
against `test` were timed at 19.2s / 12.6s / 13.2s with every pipeline in the account staying
`IDLE`. ENV-005 needs only those, so it is implemented and passes. Waiving both would have been
waiving one requirement for a cost the other one has.

What remains genuinely unproven is stated rather than implied: **nothing in this repository
demonstrates that a fixed input produces byte-identical gold.** MOD-004 (`PLANNED`) covers re-run
stability of gold within an existing catalog, which is a weaker and different claim. NFR-4 in the
North Star calls reproducibility "the requirement most often claimed and least often tested", and
this row is that claim going untested here too — named, not quietly dropped.
