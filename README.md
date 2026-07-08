# Project Atlas

**AI-powered Database Observability Platform built with AWS, Airflow and Python.**

Project Atlas is a personal engineering project designed to collect, process, analyze, and summarize cloud database operational data using modern data engineering pipelines.

---

## Vision

Project Atlas aims to transform traditional database operations into a modern cloud data platform.

The project collects database operational signals such as CPU usage, connections, storage, I/O, logs, events, and performance metrics, then turns them into actionable insights.

---

## Current Status

> Phase 0: Project setup and architecture planning

This repository is currently in the early stage.

The first milestone is to collect AWS CloudWatch metrics for Amazon RDS using Python and store the result in Amazon S3.

---

## Planned Features

- Collect RDS CloudWatch metrics using Python
- Store collected data in Amazon S3
- Build a data lake structure using S3, Glue, and Athena
- Create Airflow DAGs for workflow orchestration
- Collect Performance Insights data
- Generate database operation reports
- Add AI-based database health summary
- Send summaries to Slack or email

---

## Tech Stack

- Python
- SQL
- AWS CloudWatch
- Amazon RDS
- Amazon S3
- AWS Glue
- Amazon Athena
- Apache Airflow / Amazon MWAA
- AI Summary

---

## Roadmap

| Phase | Goal | Status |
|---|---|---|
| Phase 0 | Repository setup and planning | In Progress |
| Phase 1 | CloudWatch metric collection | Planned |
| Phase 2 | S3 data lake foundation | Planned |
| Phase 3 | Airflow orchestration | Planned |
| Phase 4 | Performance Insights analytics | Planned |
| Phase 5 | AI-powered database summary | Planned |

---

## License

MIT License
