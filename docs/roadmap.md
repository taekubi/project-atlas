# Roadmap

## Where this stands

All five phases from the original plan are implemented and deployed:

| Phase | Goal | Status |
|---|---|---|
| Phase 1 | CloudWatch metric collection | Done — config-driven, cross-account, scheduled every 15 min |
| Phase 2 | S3 data lake foundation | Done — Raw JSON + Curated Parquet, Glue/Athena |
| Phase 3 | Scheduled orchestration | Done for today's needs — EventBridge Scheduler; Airflow/MWAA still planned (see below) |
| Phase 4 | Performance Insights analytics | Done — Top SQL ranking, correlated against live health |
| Phase 5 | AI-powered database summary | Done — Bedrock interpretation across health, storage, Top SQL, and monthly reports |

The original "generate database operation reports" goal is met by `/atlas report`, which aggregates a calendar month from the Curated layer and compares it against the month before, per resource, rather than reporting isolated averages.

**Airflow/MWAA is still part of the plan**, not a rejected alternative — it was in the original tech stack for a reason: as Atlas grows past a couple of independent scheduled Lambdas into a pipeline with real cross-stage dependencies (backfills, retries-with-state, a DAG that needs to show its own history), that's exactly what Airflow is for. It just hasn't been reached yet: today's pipeline is two independently-scheduled stages (collect, curate) with no dependency between their runs, so EventBridge Scheduler covers the current need without paying for MWAA's always-on environment ahead of actually needing it. `dags/` is reserved for that migration. See item 12 below for what would trigger it.

## Priority order for what's next

Ranked by return relative to effort, not strictly by severity — a cheap, high-visibility fix goes ahead of an expensive, low-visibility one.

### Now

1. **Fill `docs/` and keep it current.** In progress — this file is part of that. A portfolio repo is judged by what a reader can see without running the code; undocumented depth doesn't count.
2. **Test coverage for the Curated transform and config loader.** `src/transforms/cloudwatch_curated.py` (the module that turns Raw into what every query reads) and `src/config/atlas_config.py` have no tests. A silent correctness bug in either one would violate the data-correctness requirements (see [requirements.md](requirements.md)) without anything catching it.
3. **CI on push.** No `.github/workflows` yet — tests only run when someone remembers to run them locally. A green badge is also a specific, checkable portfolio signal.

### Next

4. **Batch the CloudWatch calls in `live_health.py`.** Currently one `GetMetricData` call per resource in a sequential loop; `GetMetricData` supports up to 500 queries per call, so an account-wide query should be one round trip, not N. Becomes a real latency/timeout risk as the number of monitored instances grows.
5. **Honor every configured region, not just the first.** `TargetSettings.regions` is a list; only `regions[0]` is ever queried. Silent under-coverage for anyone who configures more than one region per target.
6. **Incremental Curated processing.** The transform currently reprocesses the entire Raw prefix every run — simple and safe (each hourly partition is overwritten deterministically, so a missed run needs no catch-up), but its cost scales with total Raw volume rather than new data. Measured baseline as of the first scheduled run: 1,563 Raw objects in 77–97s against a 300s timeout. Worth redesigning before that ratio gets much worse, not after a timeout happens in production.

### Later

7. **Anomaly detection / proactive alerts.** Everything currently is pull — a person has to ask. `src/query/baseline.py` already computes each resource's own trailing average and standard deviation; a scheduled Lambda comparing live values against that baseline and posting to Slack only when something crosses a threshold would turn Atlas from a query tool into a monitoring system. Needs a suppression/dedup design so the same ongoing issue doesn't re-alert every cycle.
8. **Wait-event / blocking analysis.** `/atlas topsql` answers "which SQL is loading the system"; the natural next question is "why is it slow" — Performance Insights' `db.wait_event` dimension answers that with the same `describe_dimension_keys` call already in use, just a different `GroupBy`. The IAM permission (`pi:DescribeDimensionKeys`) is already granted, so this is mostly a query/prompt change.
9. **Thread-aware follow-up questions.** Every `/atlas` question is independent today; "그럼 어제는 어땠어?" as a Slack-thread follow-up would need the previous question's target/mode kept somewhere keyed by thread timestamp (a short-TTL DynamoDB table is the natural fit) and fed to `intent_parser` as context.
10. **Data-freshness and cost transparency in every reply.** A line like "데이터 기준: 2026-08-21 14:00 KST" surfaces exactly the kind of staleness that motivated the Curated-transform scheduling work, and helps a user reason about what they're trusting.
11. **RDS event/log correlation.** Metrics alone can't answer "why did this spike happen" — `DescribeEvents` (failovers, reboots, parameter changes) and error-log excerpts (OOM, connection refusals) on the same timeline would give the AI interpretation a materially better basis for root-cause framing.
12. **Migrate orchestration to Airflow/MWAA.** Part of the original plan, not a later addition. The trigger isn't a calendar date, it's the pipeline outgrowing "independent schedules": once the curated-transform run needs to depend on the collector's run finishing (rather than just being safe to run stale), once a failed run needs a backfill rather than a same-shape retry, or once there are enough stages that their schedule relationships need to be seen rather than inferred from separate `rate()` expressions, that's the point EventBridge Scheduler stops being enough and `dags/` stops being a placeholder.

## Smaller items tracked, not yet prioritized

- Slack replies are `ephemeral` (visible only to the requester); sharing a result with a channel needs an explicit `in_channel` option.
- A Slack signature failure currently propagates as an uncaught exception (API Gateway returns 502); should return 401 directly so Slack doesn't interpret it as a transient failure worth retrying.
- No upper bound on how many resources a loose name match or an account-wide query can pull in, or on Athena result-row count — both feed directly into the Bedrock prompt.
- `database`/`table` names reach SQL query builders without the same strict-pattern validation applied to `account_id`/`region`/`resource_id`. Low risk today (both come from environment variables, not user input), but inconsistent with the rest of the validation layer.

## Guiding constraint

Every item above is additive to, not a replacement for, the principle stated in [architecture.md](architecture.md): Atlas computes the numbers, AI interprets them. A feature that asks the model to estimate a figure Atlas could compute itself is out of scope regardless of how useful it sounds.
