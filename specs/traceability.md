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
| ING-003 | `tests/integration/test_schema_drift.py::test_additive_column_survives` | integration | PLANNED |
| ING-004 | `tests/integration/test_schema_drift.py::test_incompatible_field_lands_in_rescued_data` | integration | PLANNED |
| ING-005 | `tests/integration/test_cdc_idempotency.py::test_replay_window_is_idempotent` | integration | PLANNED |
| ING-006 | `tests/integration/test_watermark.py::test_late_within_watermark_is_counted` | integration | PLANNED |
| ING-007 | `tests/integration/test_watermark.py::test_late_beyond_watermark_is_recorded_not_silent` | integration | PLANNED |
| QLT-001 | `tests/unit/test_quality_rules.py::test_every_silver_table_has_rules` | unit | PLANNED |
| QLT-002 | `tests/integration/test_quarantine.py::test_quarantine_row_carries_reason` | integration | PLANNED |
| QLT-003 | `tests/integration/test_quarantine.py::test_row_conservation` | integration | PLANNED |
| QLT-004 | `tests/integration/test_referential_integrity.py::test_no_orphan_facts` | integration | PLANNED |
| QLT-005 | `tests/unit/test_quality_rules.py::test_generated_rules_require_review` | unit | PLANNED |
| QLT-006 | `tests/integration/test_dq_metrics.py::test_metrics_written_per_run` | integration | PLANNED |
| QLT-007 | `tests/integration/test_dq_metrics.py::test_regression_fails_gate` | integration | PLANNED |
| MOD-001 | `tests/unit/test_scd2.py::test_attribute_change_creates_version` | unit | PLANNED |
| MOD-002 | `tests/unit/test_scd2.py::test_no_overlapping_validity_windows` | unit | PLANNED |
| MOD-003 | `tests/unit/test_scd2.py::test_point_in_time_join_is_one_to_one` | unit | PLANNED |
| MOD-004 | `tests/integration/test_gold_determinism.py::test_rerun_produces_identical_checksums` | integration | PLANNED |
| MOD-005 | `tests/unit/test_metrics_register.py::test_no_duplicate_kpi_definitions` | unit | PLANNED |
| MOD-006 | `tests/unit/test_naming_conventions.py::test_monetary_columns_declare_currency` | unit | PLANNED |
| PRF-001 | `tests/integration/test_skew_lab.py::test_baseline_profile_shows_skew` | integration | PLANNED |
| PRF-002 | `tests/integration/test_skew_lab.py::test_mitigation_reduces_task_time_ratio` | integration | PLANNED |
| PRF-003 | `tests/integration/test_file_sizing.py::test_median_file_size_above_floor` | integration | PLANNED |
| PRF-004 | `tests/integration/test_shuffle.py::test_shuffle_write_reduced` | integration | PLANNED |
| PRF-005 | `tests/unit/test_perf_evidence_doc.py::test_every_claim_has_measurements` | unit | PLANNED |
| PRF-006 | `tests/unit/test_table_properties.py::test_po_and_manual_optimize_disjoint` | unit | PLANNED |
| ENV-001 | `tests/unit/test_no_hardcoded_catalog.py::test_no_literal_catalog_in_src` | unit | PLANNED |
| ENV-002 | `tests/integration/test_bundle_targets.py::test_test_target_writes_only_to_test_catalog` | integration | PLANNED |
| ENV-003 | `tests/unit/test_deploy_provenance.py::test_prod_deploy_references_tested_sha` | unit | PLANNED |
| ENV-004 | `tests/integration/test_reproducibility.py::test_clean_deploy_matches_golden_fixture` | integration | PLANNED |
| ENV-005 | `tests/integration/test_rollback.py::test_redeploy_prior_sha_restores_state` | integration | PLANNED |
| ENV-006 | `tests/unit/test_offline_capable.py::test_unit_suite_needs_no_workspace` | unit | PLANNED |
| MLR-001 | `tests/integration/test_ml_reproducibility.py::test_rerun_from_logged_params` | integration | PLANNED |
| MLR-002 | `tests/integration/test_model_gate.py::test_beats_recency_baseline` | integration | PLANNED |
| MLR-003 | `tests/unit/test_eval_dataset.py::test_eval_contains_no_synthetic_rows` | unit | PLANNED |
| MLR-004 | `tests/integration/test_model_gate.py::test_failing_model_cannot_be_promoted` | integration | PLANNED |
| MLR-005 | `tests/integration/test_drift_monitor.py::test_data_and_model_drift_reported_separately` | integration | PLANNED |
| MLR-006 | `tests/unit/test_feature_leakage.py::test_no_future_information_in_features` | unit | PLANNED |
| AGT-001 | `tests/integration/test_agent_grounding.py::test_claims_trace_to_tool_calls` | integration | PLANNED |
| AGT-002 | `tests/integration/test_agent_tracing.py::test_trace_logged_per_request` | integration | PLANNED |
| AGT-003 | `tests/integration/test_agent_eval.py::test_judges_score_eval_set` | integration | PLANNED |
| AGT-004 | `tests/integration/test_agent_eval.py::test_low_score_blocks_deploy` | integration | PLANNED |
| AGT-005 | `tests/integration/test_agent_monitoring.py::test_production_traces_sampled` | integration | PLANNED |
| AGT-006 | `tests/integration/test_agent_eval.py::test_agent_declines_unanswerable` | integration | PLANNED |
| GOV-001 | `tests/integration/test_governance.py::test_all_gold_columns_documented` | integration | PLANNED |
| GOV-002 | `tests/integration/test_governance.py::test_lineage_terminates_at_source` | integration | PLANNED |
| GOV-003 | `tests/integration/test_governance.py::test_gold_tables_have_domain` | integration | PLANNED |
| GOV-004 | `tests/integration/test_governance.py::test_metric_terms_in_glossary` | integration | PLANNED |

---

## Why every row currently reads PLANNED

This matrix was written **before** the tests, on purpose. Writing it first forces the acceptance
criterion to be stated in terms a test can check, which is the moment most vague requirements
collapse — you discover you cannot say what "good data quality" would assert.

The rows flip to `PASSING` as each phase lands. The CI gate enforces two things from day one:
no requirement without a row, and no row pointing at a nonexistent test path. It does **not**
require every test to pass immediately, because that would force either fake tests or a
big-bang merge, and both are worse.

## Waivers

None yet. Anything requiring service principals, multi-workspace, or private networking will be
marked `WAIVED` with a pointer to `docs/architecture/production-delta.md`, which describes what the
design would be on a paid tier. A waiver is a documented gap; a silent omission is a lie.
