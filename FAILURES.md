# FAILURES.md

Honest, specific known failure modes and limitations in this implementation.

## 1. SQLite data is lost on redeploy or container restart

The application uses a SQLite database stored on the deployment host's
ephemeral filesystem. We observed this directly during testing: a
redeploy caused the SQLite database to be recreated, which removed the
previously created test rule.

Therefore, if the application restarts or is redeployed after rules or
jobs have been created, data stored in the local SQLite database can be
lost.

Fix: use a persistent hosted database such as PostgreSQL or a deployment
platform with a persistent volume.

## 2. The client-side rate limiter resets on restart

`wait_for_rate_limit_slot()` tracks recent API call timestamps in an
in-memory deque rather than in the database.

If the process restarts during a rolling 60-second rate-limit window,
that in-memory history is lost. The application could therefore attempt
more than 10 requests within the true rolling window immediately after
the restart, causing PseudoGram to return `429` responses.

Fix: persist rate-limit state or use a distributed rate limiter.

## 3. A DM can remain queued indefinitely if delivery status never resolves

After the PseudoGram API accepts a DM, the application checks its delivery
status.

If PseudoGram continuously reports the DM as `queued` and never eventually
returns `delivered` or `failed`, the corresponding job remains in the
queued state indefinitely.

There is currently no timeout or maximum reconciliation period for this
case.

Fix: add a reconciliation timeout and eventually mark the job as failed
or move it to a separate dead-letter state.

## 4. Unexpected HTTP status codes are treated as generic retries

The DM sending logic explicitly handles the expected `200/202`, `429`,
`500`, and `400` responses.

Other unexpected HTTP status codes fall through to the generic retry
path. This can make the actual cause harder to diagnose and can result
in unnecessary retries.

We encountered this debugging limitation during development and added
explicit response logging so the HTTP status and response body are
visible in the deployment logs.

Fix: explicitly classify authentication, authorization, and other
unexpected status codes and log them separately.

## 5. Webhook signature verification is not implemented

PseudoGram provides an `X-PseudoGram-Signature` HMAC-SHA256 header for
authenticating webhook requests.

The current implementation does not verify this signature before
processing the webhook.

Therefore, a forged request sent directly to the public `/webhook`
endpoint could potentially be accepted as a legitimate event.

Fix: calculate the HMAC-SHA256 signature using the API key and compare it
against the received signature before accepting the event.

## 6. `comment.deleted` events are not handled specially

The application is designed around `comment.created` events.

A `comment.deleted` event does not currently cancel an already queued DM
or otherwise reconcile the deletion with an existing job.

Therefore, if a comment is deleted after a matching event has already
created a DM job, the DM may still be sent.

Fix: persist deletion state and cancel or suppress pending jobs associated
with deleted comments.

## 7. PseudoGram can return HTTP 200 while reporting a failed DM

The PseudoGram API can return a successful HTTP response while the
response body reports that the DM itself failed.

The application therefore cannot treat HTTP 200 alone as proof that the
recipient ultimately received the DM.

The implementation records the API-reported result and retries where
appropriate, but full post-acceptance delivery reconciliation is limited
by the current implementation.

Fix: reconcile accepted DM IDs using `GET /v1/dm/{dm_id}` until a terminal
`delivered` or `failed` status is reached.