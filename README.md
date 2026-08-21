# Project Atlas

**Slack-native AI observability for AWS RDS/Aurora.** Ask a database's health, storage trend, top SQL, or monthly report in plain Korean or English — Atlas computes the numbers from CloudWatch/Performance Insights and has Amazon Bedrock write up what they mean.

Built solo as a portfolio project by an AWS Cloud DBA moving into cloud/data platform engineering. Runs entirely on serverless AWS (Lambda + EventBridge Scheduler + S3 + Athena/Glue + Bedrock) across a cross-account setup: one storage/AI account, any number of monitored target accounts.

---

## What it does

A `/atlas` Slack slash command answers four kinds of question, each backed by a real query — Bedrock is never asked to estimate a number Atlas can compute itself:

| Command | Answers | Source |
|---|---|---|
| `/atlas <name> [질문]` | Free-form natural language (Korean or English) | Bedrock tool-use parses intent, then routes to one of the below |
| `/atlas health <target> [date \| 30m \| 2h]` | Is this DB healthy right now / on a given day, compared to its own recent baseline? | Live CloudWatch, or day-granularity Curated Parquet via Athena |
| `/atlas storage <target> [30d]` | At this rate, when does it run out of disk? | Daily `FreeStorageSpace`/`VolumeBytesUsed` history, linear trend fit |
| `/atlas topsql <target> [10m]` | Which SQL is driving the load right now? | Performance Insights `db.load.avg`, correlated against live CloudWatch |
| `/atlas report <target> [2026-07]` | How did last month go, and what changed vs. the month before? | A month of Curated history aggregated and compared month-over-month |

A target can be a configured account name, an exact instance/cluster identifier, or a loose/partial name ("watchcon" resolves to every instance in the `watchcon` cluster) — resolved against live RDS/Aurora discovery rather than requiring exact IDs.

## How it's built

```
RDS/Aurora (target accounts)
   │  cross-account AssumeRole
   ▼
CloudWatch + Performance Insights
   │
   ▼
Collector Lambda ──(every 15 min)──▶ Raw JSON on S3 (Hive-partitioned)
   │
   ▼
Curated-transform Lambda ──(hourly)──▶ Curated Parquet on S3 + Athena partition repair
   │
   ▼
Query layer (Athena / live CloudWatch / Performance Insights)
   │
   ▼
Bedrock (Claude) ── interprets the numbers Atlas already computed
   │
   ▼
Slack (/atlas slash command, two Lambdas split to respect Slack's 3s ack window)
```

**Design principle carried through every feature**: Atlas computes exact figures from its own metric history; Bedrock is only ever asked to *interpret* those figures — never to estimate a number that wasn't already in the prompt. Every AI summary is written to say "not enough history yet" rather than guess when data is thin.

**Cross-account by design.** The storage/Bedrock account never holds long-lived credentials in a target account — each query assumes a read-only observer role scoped to CloudWatch, RDS, and Performance Insights `Describe*`/`Get*` calls, nothing else.

**SQL built from Athena queries.** Query builders interpolate `account_id`/`region`/`date`/`resource_id` directly into SQL, but every one of those values is validated against a strict whitelist regex first (`^[A-Za-z0-9_-]+$` for resource IDs, `^\d{12}$` for account IDs, and so on) — user input never reaches Athena unvalidated.

## Slack, without watching it

Live monitoring and historical lookups both go through the same `/atlas` command; the monthly report is the same idea stretched over a month instead of a moment — "connections are up 41% vs. last month" rather than a column of averages. Every summary states its own data coverage (`active_days`/`days_in_month`) so a thin or in-progress month is never presented as a full one.

## Tech stack

- **Compute**: AWS Lambda (Python 3.12), EventBridge Scheduler
- **Storage/query**: Amazon S3 (Raw JSON + Curated Parquet, Hive-partitioned), AWS Glue Catalog, Amazon Athena
- **Metrics**: Amazon CloudWatch, RDS Performance Insights
- **AI**: Amazon Bedrock (Claude, Converse API + tool-use for structured intent parsing)
- **Interface**: Slack slash commands (signed requests, async job dispatch)
- **IAM**: cross-account AssumeRole, least-privilege per-Lambda execution roles
- **Testing**: pytest (156 tests — SQL injection guards, calendar edge cases, failure-path guarantees, prompt formatting)

## Status

All five original roadmap phases are implemented and deployed: CloudWatch collection, the S3/Glue/Athena data lake, scheduled orchestration, Performance Insights analytics, and AI-powered summaries delivered to Slack. The monthly report closes the original "operations report" goal. Orchestration today runs on EventBridge Scheduler, with Airflow/MWAA still planned for when the pipeline needs real cross-stage dependencies — see below.

**Known gaps, tracked deliberately rather than hidden:**
- Multi-region config is accepted but only the first region per target is queried.
- The curated-transform Lambda reprocesses the full Raw prefix every run; this is simple and safe at current volume but will need an incremental design before Raw grows much further (see the module docstring in `src/handlers/cloudwatch_curated_handler.py`).
- No CI yet — tests run locally, not on push.
- Apache Airflow/MWAA is part of the original plan for orchestrating the pipeline once it has real cross-stage dependencies (backfills, retry-with-state, a DAG UI) — not yet started. Today's pipeline is a handful of independently-scheduled Lambdas, so EventBridge Scheduler covers it in the meantime; `dags/` is reserved for the move to Airflow.

## Repository layout

```
src/
  collectors/   RDS/Aurora + CloudWatch discovery and metric collection
  transforms/   Raw JSON → Curated Parquet
  query/        Athena/CloudWatch/Performance Insights query builders + validators
  ai/           Bedrock prompts: interpretation, never invention
  handlers/     Lambda entry points
  auth/         Cross-account session handling
  observability/ Structured JSON logging
infra/          IAM policies, Lambda + scheduler definitions, deployment notes
config/         Portable TOML config (targets, regions, collection settings)
tests/          156 tests, mirrors src/ layout
```

## License

MIT License — see [LICENSE](LICENSE).
