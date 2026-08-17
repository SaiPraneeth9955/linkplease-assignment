# LinkPlease Assignment

A webhook-driven backend service that receives comment events, matches
comments against configured rules, queues direct messages, sends DMs
through the PseudoGram API, prevents duplicate DMs, and exposes processing
statistics.

## Tech Stack

- Python
- FastAPI
- SQLite
- Requests
- PseudoGram API
- Railway

## Features

### Webhook Processing

The `/webhook` endpoint receives `comment.created` events and stores them
in SQLite before processing them asynchronously.

### Rule Matching

Rules can be created using:

`POST /rules`

A comment is matched against the configured keyword. Matching comments
create a DM job.

### Duplicate Protection

Incoming webhook events are protected using a unique `event_id`.

DM jobs also use:

`UNIQUE(rule_id, user_id)`

This prevents the same user from receiving multiple DMs for the same rule.

### Background Processing

Two background workers process the system asynchronously:

1. Event processing worker
2. DM sending worker

### Rate Limiting

Outgoing PseudoGram API calls are limited to 10 calls per 60 seconds.

### Retry Handling

Temporary request failures are retried up to 5 attempts.

### Idempotency

Each DM request uses the queue `job_id` as the PseudoGram
`Idempotency-Key`.

### Database Reliability

SQLite uses:

- WAL journal mode
- 30 second connection timeout
- `check_same_thread=False`

This allows concurrent webhook and worker access more reliably.

## API Endpoints

### GET `/`

Health check.

### POST `/rules`

Create a DM rule.

Example:

```json
{
  "keyword": "price",
  "dm_message": "Here's the price list!"
}