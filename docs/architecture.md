# Architecture

## Overview

Atlas runs as a set of independent, scheduled AWS Lambda functions sharing one S3 bucket and one Glue/Athena table. There is no always-on server and no orchestrator with task dependencies yet — each stage runs on its own EventBridge schedule and hands data to the next stage entirely through S3. Apache Airflow/MWAA is still planned for the point where the pipeline has real cross-stage dependencies (backfills, retry-with-state, a DAG UI); see [roadmap.md](roadmap.md) for the current thinking on when that point arrives.

```
┌─────────────────────────┐        cross-account
│  Target AWS account(s)  │        AssumeRole
│  RDS / Aurora            │◀───────────────────┐
│  CloudWatch metrics      │                     │
│  Performance Insights    │                     │
└──────────────┬───────────┘                     │
               │ read-only Describe/Get calls     │
               ▼                                  │
┌──────────────────────────────────────────────────────────────┐
│  Atlas storage account (ap-northeast-2)                       │
│                                                                 │
│  ┌────────────────────┐  every 15 min                          │
│  │ Collector Lambda    │──────────────┐                        │
│  │ (configured_rds_    │              ▼                        │
│  │  metrics_handler)   │      S3: raw/cloudwatch/               │
│  └──────────┬───────────┘      account_id=.../region=.../       │
│             │ assumes the target's                              │
│             │ observer role per account   date=YYYY-MM-DD/       │
│             ▼                              *.json                │
│  ┌────────────────────┐  hourly                                 │
│  │ Curated-transform   │──────────────┐                         │
│  │ Lambda              │              ▼                         │
│  └──────────┬───────────┘      S3: curated/cloudwatch/           │
│             │ MSCK REPAIR TABLE  account_id=.../region=.../       │
│             ▼                    date=.../hour=.../*.parquet      │
│      Glue Catalog / Athena table (project_atlas.cloudwatch_metrics)│
│             │                                                    │
│             │ query layer (Athena + live CloudWatch/PI calls)     │
│             ▼                                                    │
│  ┌────────────────────┐         ┌────────────────────┐          │
│  │ Slack command       │────────▶│ Slack query job     │          │
│  │ Lambda (ack < 3s)    │  async  │ Lambda (does the work,│         │
│  └──────────┬───────────┘ invoke │  posts back to Slack) │         │
│             │                    └──────────┬───────────┘          │
│             │                               │ Bedrock Converse/    │
│             ▼                               ▼ tool-use              │
│         Slack API                    Amazon Bedrock (Claude)        │
└──────────────────────────────────────────────────────────────┘
```

## Why two Lambdas for one Slack command

Slack requires an HTTP 200 within 3 seconds of a slash command, but a real answer needs an Athena query (or a live CloudWatch call) plus a Bedrock interpretation — routinely 5–40+ seconds. `handle_slash_command` only verifies the Slack request signature, parses nothing beyond the raw text, and asynchronously invokes `handle_query_job` before acknowledging. `handle_query_job` does the actual work and posts the result to Slack's `response_url` when it's done. This split is why a Slack answer arrives as a second message a few seconds after the immediate "질문을 확인하고 있습니다..." acknowledgement.

`handle_query_job` is written to always reach `_post_to_response_url`, whatever happens internally — user errors, AWS/Bedrock failures, and unexpected exceptions are each caught and turned into a reply rather than left to fail the invocation silently. A delivery failure to Slack itself is logged rather than raised, since raising there would make Lambda retry the entire query.

## Storage layout

**Raw** (`s3://<bucket>/raw/cloudwatch/`) — one JSON file per (resource, metric, day), Hive-partitioned by `account_id`, `region`, `metric`, `date`. Written by the collector every 15 minutes; never modified afterward.

**Curated** (`s3://<bucket>/curated/cloudwatch/`) — Parquet, partitioned by `account_id`, `region`, `date`, `hour`. The curated-transform Lambda reprocesses the *entire* Raw prefix on every run and overwrites each hourly partition deterministically, so a missed or retried run is always safe. This is simple at the current data volume but its cost scales with total Raw volume, not new data — see the "known gaps" note in [roadmap.md](roadmap.md).

**Athena table** (`project_atlas.cloudwatch_metrics`) reads Curated directly; `MSCK REPAIR TABLE` runs at the end of every curated-transform invocation so new partitions are queryable immediately.

## Query layer

Three ways of answering "how is this database doing," each suited to a different latency/freshness tradeoff:

- **Athena over Curated** (`src/query/db_health.py`, `storage_forecast.py`, `baseline.py`, `monthly_report.py`) — day granularity, batch latency, used for historical/date queries, storage trend fitting, per-resource baselines, and the monthly report.
- **Live CloudWatch** (`src/query/live_health.py`) — `GetMetricData` with the whole lookback window as a single period, for "how is it right now" questions where Curated's latency isn't acceptable.
- **Performance Insights** (`src/query/top_sql.py`) — `DescribeDimensionKeys` grouped by SQL statement, for "what's causing the load" questions. Only available for instances with PI explicitly enabled; Atlas detects this via each instance's `DbiResourceId` and reports which resources were skipped rather than silently omitting them.

All three return the same per-resource row shape (`cpu_avg`, `connections_avg`, etc.) so the AI summary and Slack formatting layers don't need to know which one produced a given row.

Every query builder validates its inputs — `account_id`, `region`, `date`/`month`, `resource_id` — against a strict whitelist regex (`src/query/validators.py`) before interpolating them into SQL, since these values ultimately come from a Slack request or Bedrock-parsed free text rather than a trusted operator typing SQL by hand.

## Resource resolution

A `/atlas` target can be:
1. A configured account name (`atlas.toml`'s `[[targets]]`), which resolves to every instance discovered in that account.
2. An exact DB instance or cluster identifier.
3. A loose or partial name, matched case-insensitively against live RDS/Aurora discovery — a cluster name expands to every member instance (writer + reader together), and a substring match ("watchcon" → every `watchcon-*` resource) means a user never has to memorize exact identifiers. Every enabled target account is searched; a match in more than one account raises an error asking for a more specific name, since a single query is scoped to one account/region.

For storage and monthly-report queries, resolved instance identifiers are also extended with each matched instance's own `cluster_identifier` — Aurora publishes its storage metric (`VolumeBytesUsed`) once per cluster, not per instance, so the cluster's own identifier has to be in scope to find it.

## AI interpretation

Every `src/ai/*_summary.py` module follows the same shape: fetch/compute exact figures with the query layer, then send those figures — never raw permission to estimate — to Bedrock's Converse API with a system prompt that states the numbers are already correct and asks only for interpretation. `parse_health_intent` (`src/ai/intent_parser.py`) is the exception in kind rather than principle: it uses Bedrock's forced tool-use to turn free-form text into the same structured `(target, mode, value)` shape the fixed command grammar produces, so downstream handling never needs to know which parser was used.

The monthly report's prompt is deliberately different from the point-in-time summaries: it leads with month-over-month change rather than absolute figures, and every resource's line carries `active_days`/`days_in_month` and a `coverage_note` so a thinly-collected or in-progress month is never presented as a complete one.

## Cross-account model

The Atlas storage account never holds long-lived credentials for a monitored target account. Each query assumes a per-target `project-atlas-rds-observer-role`, scoped to read-only `Describe*`/`Get*` calls against RDS, CloudWatch, and Performance Insights (`infra/iam/cross-account/target-observer-read-policy.json`). Lambda execution roles within the storage account are similarly scoped per function — the Slack command function can only invoke the job function; the job function can read/write only the paths and Athena workgroup it needs.

The Slack signing secret lives in SSM Parameter Store as a SecureString (`/project-atlas/slack/signing-secret`), read at invocation time rather than kept as a Lambda environment variable, so it never appears in function configuration or deployment tooling output.

## Configuration

`config/atlas.toml` (portable, not hardcoded) defines the storage account's bucket/region, default collection settings (lookback window, period, metric profile), and the list of target accounts (`name`, `account_id`, `role_name`, `regions`, `enabled`). Loaded and validated by `src/config/atlas_config.py`; downloaded from S3 at invocation time by each handler rather than baked into the deployment package, so adding a target account doesn't require a redeploy.

## Observability

Every handler that runs unattended (the Slack job handler and the three batch handlers) logs through `src/observability/logger.py`, a JSON-line structured logger — CloudWatch Logs Insights can filter on fields directly (`filter mode = "topsql" and level = "ERROR"`) rather than matching substrings. The Slack job handler additionally logs a `request_id` on every reply so a user-reported failure can be traced to its exact traceback.

## Deployment

Deployed by hand with the AWS CLI; see [infra/README.md](../infra/README.md) for the exact commands, the two packaging shapes (vendored boto3 vs. the layer-backed src-only package the curated-transform function uses to avoid vendoring pyarrow), and the current schedules.
