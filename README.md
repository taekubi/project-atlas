# Project Atlas

**AI-powered Database Observability Platform built with AWS, Airflow and Python.**

Project Atlas is a personal engineering project designed to collect, process, analyze, and summarize cloud database operational data using modern data engineering pipelines.

The long-term goal of this project is to transform traditional database administration work into a modern cloud data platform approach.

---

## Vision

Database operations generate valuable signals every day: CPU usage, connections, storage, I/O, slow queries, top SQL, events, alarms, and performance trends.

However, these signals are often scattered across multiple tools such as CloudWatch, Performance Insights, database logs, and monitoring dashboards.

Project Atlas aims to collect those signals into a centralized data platform and turn them into actionable insights.

---

## Why This Project Exists

Traditional DBA work is often focused on backup, recovery, migration, and manual troubleshooting.

This project explores a more modern direction:

- Database Observability
- Data Engineering
- Cloud Automation
- Airflow-based Orchestration
- AI-assisted Operations
- Operational Analytics

The purpose is not only to monitor databases, but also to build a data pipeline that helps engineers understand database behavior over time.

---

## Current Status

> Phase 0: Project setup and architecture planning

This repository is currently in the early stage.

The first milestone is to collect AWS CloudWatch metrics for Amazon RDS using Python and store the result in Amazon S3.

---

## Target Architecture

```text
AWS CloudWatch
Performance Insights
Database Logs
RDS Events
        |
        v
Python Collector
        |
        v
Amazon S3
        |
        v
AWS Glue Data Catalog
        |
        v
Amazon Athena
        |
        v
Dashboard / Report / AI Summary
