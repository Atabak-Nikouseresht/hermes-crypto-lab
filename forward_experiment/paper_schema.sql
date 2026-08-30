-- Generated from a temporary DuckDB; production was not modified.
-- cash_ledger
CREATE TABLE cash_ledger(event_id VARCHAR PRIMARY KEY, run_id VARCHAR, account_id VARCHAR NOT NULL, event_type VARCHAR NOT NULL, amount DOUBLE NOT NULL, balance_after DOUBLE NOT NULL, created_at_utc TIMESTAMP WITH TIME ZONE NOT NULL);;

-- equity_snapshots
CREATE TABLE equity_snapshots(snapshot_id VARCHAR PRIMARY KEY, run_id VARCHAR NOT NULL UNIQUE, account_id VARCHAR NOT NULL, cash DOUBLE NOT NULL, positions_value DOUBLE NOT NULL, equity DOUBLE NOT NULL, snapshot_at_utc TIMESTAMP WITH TIME ZONE NOT NULL);;

-- forward_baselines
CREATE TABLE forward_baselines(experiment_id VARCHAR PRIMARY KEY, run_id VARCHAR NOT NULL UNIQUE, observed_at_utc TIMESTAMP WITH TIME ZONE NOT NULL, equity DOUBLE NOT NULL);;

-- forward_experiment_incidents
CREATE TABLE forward_experiment_incidents(experiment_id VARCHAR, incident_id VARCHAR, PRIMARY KEY(experiment_id, incident_id));;

-- forward_experiment_windows
CREATE TABLE forward_experiment_windows(experiment_id VARCHAR, schedule_key VARCHAR, PRIMARY KEY(experiment_id, schedule_key));;

-- forward_experiments
CREATE TABLE forward_experiments(experiment_id VARCHAR PRIMARY KEY, started_at_utc TIMESTAMP WITH TIME ZONE NOT NULL, locked_candidate_id VARCHAR NOT NULL, locked_strategy_hash VARCHAR NOT NULL, governance_hash VARCHAR NOT NULL UNIQUE, specification JSON NOT NULL, status VARCHAR NOT NULL);;

-- forward_incidents
CREATE TABLE forward_incidents(incident_id VARCHAR PRIMARY KEY, run_id VARCHAR, incident_type VARCHAR NOT NULL, scheduled_for_utc TIMESTAMP WITH TIME ZONE, reason VARCHAR NOT NULL, created_at_utc TIMESTAMP WITH TIME ZONE NOT NULL, resolved_at_utc TIMESTAMP WITH TIME ZONE, UNIQUE(incident_type, scheduled_for_utc));;

-- forward_market_observations
CREATE TABLE forward_market_observations(run_id VARCHAR, observed_at_utc TIMESTAMP WITH TIME ZONE NOT NULL, symbol VARCHAR, price DOUBLE NOT NULL, PRIMARY KEY(run_id, symbol));;

-- forward_schedule_windows
CREATE TABLE forward_schedule_windows(schedule_key VARCHAR PRIMARY KEY, scheduled_for_utc TIMESTAMP WITH TIME ZONE NOT NULL, run_id VARCHAR, outcome VARCHAR NOT NULL, created_at_utc TIMESTAMP WITH TIME ZONE NOT NULL);;

-- notification_attempts
CREATE TABLE notification_attempts(attempt_id VARCHAR PRIMARY KEY, run_id VARCHAR NOT NULL, attempted_at_utc TIMESTAMP WITH TIME ZONE NOT NULL, status VARCHAR NOT NULL, "error" VARCHAR);;

-- paper_accounts
CREATE TABLE paper_accounts(account_id VARCHAR PRIMARY KEY, initial_cash DOUBLE NOT NULL, cash DOUBLE NOT NULL, status VARCHAR NOT NULL, created_at_utc TIMESTAMP WITH TIME ZONE NOT NULL, updated_at_utc TIMESTAMP WITH TIME ZONE NOT NULL);;

-- paper_execution_context
CREATE TABLE paper_execution_context(run_id VARCHAR, symbol VARCHAR, execution_protocol_version VARCHAR NOT NULL, signal_timestamp_utc TIMESTAMP WITH TIME ZONE NOT NULL, finalized_candle_open_utc TIMESTAMP WITH TIME ZONE NOT NULL, finalized_candle_close_utc TIMESTAMP WITH TIME ZONE NOT NULL, quote_timestamp_utc TIMESTAMP WITH TIME ZONE NOT NULL, bid DOUBLE NOT NULL, ask DOUBLE NOT NULL, midpoint DOUBLE NOT NULL, full_spread DOUBLE NOT NULL, execution_timestamp_utc TIMESTAMP WITH TIME ZONE NOT NULL, execution_delay_seconds DOUBLE NOT NULL, data_age_seconds DOUBLE NOT NULL, PRIMARY KEY(run_id, symbol));;

-- paper_fills
CREATE TABLE paper_fills(fill_id VARCHAR PRIMARY KEY, order_id VARCHAR NOT NULL UNIQUE, run_id VARCHAR NOT NULL, symbol VARCHAR NOT NULL, side VARCHAR NOT NULL, filled_quantity DOUBLE NOT NULL, mid_price DOUBLE NOT NULL, execution_price DOUBLE NOT NULL, spread_cost DOUBLE NOT NULL, slippage_cost DOUBLE NOT NULL, fee DOUBLE NOT NULL, filled_at_utc TIMESTAMP WITH TIME ZONE NOT NULL, execution_protocol_version VARCHAR);;

-- paper_incidents
CREATE TABLE paper_incidents(incident_id VARCHAR PRIMARY KEY, run_id VARCHAR, account_id VARCHAR NOT NULL, reason VARCHAR NOT NULL, created_at_utc TIMESTAMP WITH TIME ZONE NOT NULL, cleared_at_utc TIMESTAMP WITH TIME ZONE);;

-- paper_legacy_order_semantics
CREATE TABLE paper_legacy_order_semantics(order_id VARCHAR PRIMARY KEY, preserved_at_utc TIMESTAMP WITH TIME ZONE NOT NULL, execution_protocol_version VARCHAR);;

-- paper_notifications
CREATE TABLE paper_notifications(run_id VARCHAR PRIMARY KEY, "target" VARCHAR NOT NULL, report_path VARCHAR NOT NULL, status VARCHAR NOT NULL, attempt_count INTEGER NOT NULL, last_error VARCHAR, created_at_utc TIMESTAMP WITH TIME ZONE NOT NULL, updated_at_utc TIMESTAMP WITH TIME ZONE NOT NULL, delivered_at_utc TIMESTAMP WITH TIME ZONE);;

-- paper_order_rejections
CREATE TABLE paper_order_rejections(run_id VARCHAR NOT NULL, rejection_index INTEGER NOT NULL, symbol VARCHAR NOT NULL, side VARCHAR NOT NULL, stage VARCHAR NOT NULL, reason VARCHAR NOT NULL, notional DOUBLE NOT NULL, rejected_at_utc TIMESTAMP WITH TIME ZONE NOT NULL, PRIMARY KEY(run_id, rejection_index));;

-- paper_orders
CREATE TABLE paper_orders(order_id VARCHAR PRIMARY KEY, idempotency_key VARCHAR NOT NULL UNIQUE, run_id VARCHAR NOT NULL, account_id VARCHAR NOT NULL, signal_timestamp_utc TIMESTAMP WITH TIME ZONE NOT NULL, symbol VARCHAR NOT NULL, side VARCHAR NOT NULL, requested_quantity DOUBLE NOT NULL, target_weight DOUBLE NOT NULL, status VARCHAR NOT NULL, created_at_utc TIMESTAMP WITH TIME ZONE NOT NULL, execution_protocol_version VARCHAR, ledger_semantics_version VARCHAR);;

-- paper_positions
CREATE TABLE paper_positions(account_id VARCHAR, symbol VARCHAR, quantity DOUBLE NOT NULL, average_cost DOUBLE NOT NULL, updated_at_utc TIMESTAMP WITH TIME ZONE NOT NULL, PRIMARY KEY(account_id, symbol));;

-- paper_run_diagnostics
CREATE TABLE paper_run_diagnostics(run_id VARCHAR PRIMARY KEY, outcome VARCHAR NOT NULL, regime VARCHAR, btc_vs_trend DOUBLE, momentum JSON, eligibility JSON, selected_assets JSON, current_weights JSON, target_weights JSON, proposed_orders JSON, turnover DOUBLE, kill_switch_active BOOLEAN NOT NULL, reconciliation_valid BOOLEAN NOT NULL, created_at_utc TIMESTAMP WITH TIME ZONE NOT NULL, rejected_orders JSON);;

-- paper_runs
CREATE TABLE paper_runs(run_id VARCHAR PRIMARY KEY, started_at_utc TIMESTAMP WITH TIME ZONE NOT NULL, completed_at_utc TIMESTAMP WITH TIME ZONE, status VARCHAR NOT NULL, "mode" VARCHAR NOT NULL, schedule_key VARCHAR UNIQUE, signal_timestamp_utc TIMESTAMP WITH TIME ZONE, data_timestamp_utc TIMESTAMP WITH TIME ZONE, message VARCHAR, reconciliation JSON);;

-- paper_schema_versions
CREATE TABLE paper_schema_versions("version" INTEGER PRIMARY KEY, applied_at_utc TIMESTAMP WITH TIME ZONE NOT NULL, description VARCHAR NOT NULL);;

-- position_ledger
CREATE TABLE position_ledger(event_id VARCHAR PRIMARY KEY, run_id VARCHAR NOT NULL, account_id VARCHAR NOT NULL, symbol VARCHAR NOT NULL, quantity_delta DOUBLE NOT NULL, quantity_after DOUBLE NOT NULL, created_at_utc TIMESTAMP WITH TIME ZONE NOT NULL);;

-- schema versions
[(2, 'forward paper operations'), (3, 'forward baseline and monthly benchmark alignment'), (4, 'experiment-scoped windows and incidents'), (5, 'versioned ask-bid execution context'), (6, 'final executable order quantity ledger semantics'), (7, 'explicit preservation of pre-adoption ledger semantics'), (8, 'persist proposal and final execution rejection diagnostics'), (9, 'persist run-attributable paper order rejection audit trail')]
