# Fault-injection evidence

This matrix is the minimum resilience gate for a timed paper candidate. Unit and
PostgreSQL cases run in CI. Process-kill drills run only on a disposable candidate before
the official 24-hour soak; their generated evidence is retained under
`artifacts/run-state/recovery-evidence/`.

| Fault | Direct evidence | Required invariant |
|---|---|---|
| WebSocket disconnect | `tests/unit/market_data/test_binance_websocket_connector.py::test_disconnect_enters_recovery_and_rebuilds_depth_before_ready` and `::test_startup_does_not_claim_ready_before_each_order_book_is_published` | Connector cannot become ready until every configured symbol has both mark-price coverage and a reconciled, published order book. |
| REST transport disconnect | `tests/unit/market_data/test_http_transport.py` plus the transient-failure cases in each public REST connector test | Transport-only failures retry with bounded backoff and structured source evidence; exhaustion retains the exact failed task and remains fail-closed. |
| Repeated funding settlement | `tests/unit/market_data/test_public_runtime.py::test_runtime_advances_funding_window_after_each_successful_poll`, `tests/unit/market_data/test_binance_rest_connector.py::test_funding_history_rejects_an_event_outside_the_requested_window`, and the funding restart cases in `tests/integration/test_continuous_paper_observer.py` | Successful polls advance to the first unqueried millisecond; an out-of-window venue row fails closed; re-observing the same venue settlement after restart is an idempotent no-op only when rate, rate type, mark, and settlement time still match. |
| Missing bars | `tests/unit/market_data/test_gap_recovery.py` and `tests/integration/test_recovery_store.py` | Exact closed-bar range is validated, persisted, and resumed at the first uncommitted event. |
| Duplicate event | `tests/unit/market_data/test_frame_builder.py::test_identical_duplicate_is_idempotent_but_conflicting_duplicate_fails` and `tests/integration/test_decision_lineage.py::test_identical_retry_is_idempotent_but_changed_retry_conflicts` | Identical retry is a no-op; conflicting content fails closed. |
| Reordered event | `tests/unit/market_data/test_integrity_state_machine.py::test_clock_drift_sequence_regression_and_stale_book_each_fail_closed` | Sequence regression quarantines the frame. |
| Stale book | `tests/unit/market_data/test_integrity_state_machine.py::test_clock_drift_sequence_regression_and_stale_book_each_fail_closed` | New entries are blocked with recorded quality evidence. |
| Venue/schema error | `tests/unit/market_data/test_binance_websocket_connector.py::test_malformed_contract_halts_with_operator_visible_failure` and `::test_output_queue_saturation_halts_instead_of_dropping` | No malformed or dropped event is silently admitted. |
| Database outage | `tests/integration/faults/test_database_outage.py` | Worker tasks stop, the outage and failed halt write remain visible, and only an expired-lease takeover can resume. |
| Large ledger / slow verification | `tests/unit/test_replay_index.py::test_event_stream_indexes_resolve_ids_and_aggregates_in_one_pass`, `tests/integration/test_replay_consistency.py::test_ledger_verification_uses_a_bounded_query_count_for_projected_rows`, and `::test_health_verification_keeps_one_snapshot_during_concurrent_write` | Stream lookup is one-pass; ledger queries remain bounded as experiments, decisions, and orders grow; lease health is evaluated at the PostgreSQL snapshot time; and a verification that completes outside the allowed freshness window fails health explicitly. |
| Disk full | `tests/faults/test_disk_full.py` | No partial backup bundle becomes authoritative. |
| Worker kill | Disposable process drill plus `tests/integration/test_operational_state_repository.py::test_expired_lease_takeover_can_restart_existing_checkpoint` | Higher lease epoch, restored checkpoint, passing ledger, and no duplicate decision/order/fill/report identity. |
| API kill | Disposable process drill plus `scripts/recover-paper-week.sh dashboard REASON` | Worker remains alive and advances independently; restarted API is read-only and reconciles to PostgreSQL. |
| Clock drift | `tests/unit/market_data/test_integrity_state_machine.py::test_clock_drift_sequence_regression_and_stale_book_each_fail_closed` | Excess drift blocks admission. |

Run the deterministic suite with:

```bash
uv run pytest tests/faults tests/unit/market_data/test_binance_websocket_connector.py \
  tests/unit/market_data/test_gap_recovery.py \
  tests/unit/market_data/test_frame_builder.py \
  tests/unit/market_data/test_integrity_state_machine.py -q

uv run pytest tests/integration/faults tests/integration/test_recovery_store.py \
  tests/integration/test_operational_state_repository.py \
  tests/integration/test_decision_lineage.py -q
```

The PostgreSQL command requires `MAAIS_TEST_DATABASE_URL` to already reference a dedicated
local database whose name ends in `_test`.

Run both disposable process drills through `scripts/run-process-drills.sh`. It
refuses to signal a run unless `run_purpose` is `process_drill`, kills only the
exact recorded dashboard and worker PIDs, retains authoritative before/recovery/
after snapshots, and freezes a hash-verified verdict. The verdict requires that
counts never regress, ledgers always pass, the dashboard fault does not stop the
worker checkpoint, and worker recovery acquires a higher lease epoch without
restarting Mission Control or the daily supervisor. The worker fault is injected
only during seconds `10` through `15`, after the prior 10-symbol close cycle and
with enough time for expired-lease takeover before the next close. The verdict
then waits for a stable, recovery-free post-recovery symbol cycle. Any open
incident or operator-review incident in either recovery snapshot fails the
verdict. Before freezing the bundle, the runner disables the Docker command and
executes the same daily report, backup, and status path used at Berlin midnight.
The verdict requires the immutable report pointer, valid ledger, backup paths,
all four live processes, local API health, and the unchanged PostgreSQL system
identifier. The runner stops the disposable candidate after both drills. Neither
drill is performed inside the official 24-hour or seven-day measurement window.
