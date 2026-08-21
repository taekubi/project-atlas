# Atlas infrastructure

Deployed by hand with the AWS CLI; these files are the definitions those
commands read, kept in the repo so a deployment can be reproduced or
reviewed without reading it back out of the console.

Everything below lives in the Atlas storage account
`714933490352` / `ap-northeast-2`, except the cross-account observer
role, which is created once in each monitored target account.

## Lambda functions

| Function | Handler | Trigger |
|---|---|---|
| `project-atlas-cloudwatch-s3` | `configured_rds_metrics_handler` | EventBridge, every 15 min |
| `project-atlas-curated-transform` | `cloudwatch_curated_handler` | EventBridge, hourly |
| `project-atlas-slack-command` | `slack_command_handler.handle_slash_command` | Slack slash command (Function URL) |
| `project-atlas-slack-db-health-job` | `slack_command_handler.handle_query_job` | async invoke from the command function |

`project-atlas-cloudwatch-s3` keeps its original name from when it ran a
single-resource pipeline; it now runs the config-driven discovery
collector. The name is left alone because the scheduler, IAM policy, and
log group all reference it.

## Packaging

Two shapes of deployment package, both built from the repo root so that
`src/` sits at the archive root and handlers resolve as
`src.handlers.<module>.<function>`:

- **Vendored** (`project-atlas-lambda.zip`, `project-atlas-slack-lambda.zip`)
  — `src/` plus a pinned boto3/botocore, for the collector and the two
  Slack functions.
- **Layer-backed** (curated transform) — `src/` only, ~80 KB. boto3 comes
  from the runtime and pyarrow from the public AWS SDK for pandas layer,
  which avoids vendoring pyarrow's ~150 MB into the package.

```bash
# Update code on an existing function
aws lambda update-function-code \
  --function-name <name> --zip-file fileb://<package>.zip \
  --profile atlas-test --region ap-northeast-2
```

## Creating the curated transform function

Not yet created at time of writing. `project-atlas-curated-transform-role`
already exists with `AWSLambdaBasicExecutionRole` and
`project-atlas-curated-transform-policy` attached.

```bash
aws lambda create-function \
  --cli-input-json file://infra/lambda/curated-transform-function.json \
  --zip-file fileb://<src-only-package>.zip \
  --profile atlas-test --region ap-northeast-2
```

Then register its schedule:

```bash
aws scheduler create-schedule \
  --name project-atlas-curated-transform-hourly \
  --schedule-expression 'rate(1 hour)' \
  --flexible-time-window Mode=OFF \
  --target file://infra/scheduler/curated-transform-target.json \
  --profile atlas-test --region ap-northeast-2
```

The transform reprocesses the whole Raw prefix on every run and
overwrites each hourly partition deterministically, so reruns are safe
and a missed run needs no catch-up. That also means its cost grows with
total Raw volume rather than with new data — see the module docstring in
`src/handlers/cloudwatch_curated_handler.py` for when this needs to
become incremental.

## Schedules

| Schedule | Expression | Target | Notes |
|---|---|---|---|
| `project-atlas-cloudwatch-s3-every-15m` | `rate(15 minutes)` | collector | Toggled off outside working hours to control cost |
| `project-atlas-curated-transform-hourly` | `rate(1 hour)` | curated transform | Keeps the Athena layer behind Raw by at most an hour |

Enable or disable a schedule without redefining it:

```bash
aws scheduler get-schedule --name <name> --profile atlas-test --region ap-northeast-2 \
  | jq 'del(.Arn, .CreationDate, .LastModificationDate) | .State = "ENABLED"' > /tmp/sched.json
aws scheduler update-schedule --cli-input-json file:///tmp/sched.json \
  --profile atlas-test --region ap-northeast-2
```

`update-schedule` replaces the whole definition rather than patching it,
so any field left out is reset to its default — always start from the
current definition as above.

## Secrets

The Slack signing secret lives in SSM Parameter Store as a SecureString
at `/project-atlas/slack/signing-secret`, read at invocation time by
`handle_slash_command`. It is deliberately not a Lambda environment
variable, where it would show up in the function configuration and in
any tooling output that dumps it.
