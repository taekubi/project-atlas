# Requirements

This documents what Atlas is built to do — partly as a spec for what's already implemented, partly as the bar new features are held to. Where a requirement isn't yet met, it's marked as a known gap rather than left silent (see [roadmap.md](roadmap.md) for the prioritized list).

## Functional requirements

### Slack interface

- **FR-1.** A user issues `/atlas <target> [question]` and receives a reply in the same channel/DM, without needing to know a resource's exact identifier.
- **FR-2.** A fixed grammar is tried first for speed and determinism: `/atlas health <target> [YYYY-MM-DD | Nm | Nh]`, `/atlas storage <target> [Nd]`, `/atlas topsql <target> [Nm]`, `/atlas report <target> [YYYY-MM]`. Anything that doesn't match falls back to Bedrock-based natural-language parsing, so "watchcon-a 최근 30분 상태 확인해줘" and "지난달 리포트 보여줘" work without memorizing the grammar.
- **FR-3.** A target name resolves against live RDS/Aurora discovery: exact instance ID, exact cluster ID (expanding to every member instance), or a case-insensitive substring match. A name matching resources in more than one target account is rejected with a request to be more specific, rather than guessing which account was meant.
- **FR-4.** Every Slack request is signature-verified (HMAC-SHA256 over the Slack signing secret, timestamp within 300 seconds) before any other processing.
- **FR-5.** A Slack retry of the same command (sent because the original ack was slow) must not re-dispatch a duplicate query job.

### Query modes

- **FR-6 (health).** Report current-status or single-day metrics (CPU, connections, latency, IOPS, disk queue depth, Aurora replica lag, free storage/memory) for one or more resources, compared against each resource's own trailing baseline where available.
- **FR-7 (storage).** Project a storage-exhaustion date for standard RDS engines from a linear fit of daily `FreeStorageSpace` history; report Aurora `VolumeBytesUsed` growth informationally, never as an exhaustion risk, since Aurora storage auto-scales.
- **FR-8 (topsql).** Rank SQL statements by Average Active Sessions (`db.load.avg`) over a recent window for instances with Performance Insights enabled; explicitly report which requested resources were skipped because PI is off, rather than omitting them silently.
- **FR-9 (report).** Aggregate a full calendar month and compare it against the month directly before it, per resource, for a defined set of metrics. Default to the last *complete* month when none is specified. A resource present in only one of the two months (newly provisioned or decommissioned) must still appear in the report.

### AI interpretation

- **FR-10.** Every number an AI summary states must come from a value Atlas itself computed and included in the prompt. The model is never asked to produce a figure that isn't already present.
- **FR-11.** When there isn't enough history to support a claim (a new resource, Performance Insights just enabled, a partially-collected month), the summary must say so explicitly rather than presenting a thin sample as a full one.
- **FR-12.** Summaries are written in Korean, addressed to a DBA audience, concise rather than exhaustive.

### Data pipeline

- **FR-13.** Metrics are collected from every enabled target account/region on a schedule, without a code change required to add a new target — target accounts are config-driven (`config/atlas.toml`), downloaded at invocation time.
- **FR-14.** Collected data is retained in two forms: an unmodified Raw layer (source of truth) and a Curated layer optimized for Athena queries. The Curated layer must stay queryable (partitions repaired) after every refresh, not just written.

## Non-functional requirements

### Reliability

- **NFR-1.** A `/atlas` request must always receive a reply, including when an AWS call fails, times out, or throws an exception the code didn't anticipate. Silence is the one outcome the Slack job handler must never produce.
- **NFR-2.** A failure that cannot be resolved by the user (an AWS/system error) must not expose internal detail — role ARNs, account IDs, raw exception text — in the Slack reply. It carries an error type and a request ID that ties back to the full traceback in logs.
- **NFR-3.** A failed delivery back to Slack (expired `response_url`, network error) must not fail the Lambda invocation, since that would trigger a retry of the entire query rather than just the delivery.

### Observability

- **NFR-4.** Every unattended Lambda invocation logs a start/success or start/failure record as structured JSON, with fields queryable in CloudWatch Logs Insights rather than requiring substring search.
- **NFR-5.** A traceback for an unhandled failure must be preserved in logs, not discarded after being converted into a user-facing message.

### Security

- **NFR-6.** No user-supplied value (Slack text, an AI-parsed value) is interpolated into SQL without being validated against a strict allow-list pattern first.
- **NFR-7.** No cross-account credential is long-lived; every cross-account call assumes a role scoped to read-only `Describe*`/`Get*` actions.
- **NFR-8.** Secrets (the Slack signing secret) are stored in SSM Parameter Store as SecureString, not as plaintext Lambda environment variables.

### Data correctness

- **NFR-9.** A figure presented as covering a period (a day, a month) must be accompanied by enough context to judge whether the underlying data actually covers that period, rather than silently averaging over whatever partial data exists.
- **NFR-10.** A metric specific to one storage model (e.g., Aurora's auto-scaling `VolumeBytesUsed` vs. standard RDS's `FreeStorageSpace`) must never be interpreted using the other model's framing.

### Operability

- **NFR-11.** Adding a monitored target account requires only a config change (`atlas.toml` + the target account's IAM role), not a code change or redeploy.
- **NFR-12.** The Curated layer must be automatically refreshed on a schedule; it must not depend on a person remembering to run it by hand. *(Met as of the curated-transform Lambda's deployment; previously a known gap.)*

## Known gaps against these requirements

- **FR-13 partially met**: multi-region targets are accepted by config but only the first region is actually queried (NFR-11 holds for accounts, not yet for a target's additional regions).
- **NFR-1 / observability**: 14 modules still have no test coverage, including the Curated transform itself — a correctness bug there would violate NFR-9/NFR-10 without any test catching it first.
- No CI enforces any of the above on push; adherence currently depends on running `pytest` locally before merging.

See [roadmap.md](roadmap.md) for how these are prioritized.
