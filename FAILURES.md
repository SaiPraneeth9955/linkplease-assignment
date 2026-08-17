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

Therefore, an HTTP 200 response alone cannot be treated as proof that
the recipient ultimately received the DM.

The implementation records the API-reported result and performs delivery
status reconciliation where a DM ID is available, but full post-acceptance
delivery guarantees depend on the PseudoGram delivery-status endpoint.

Fix: reconcile accepted DM IDs using `GET /v1/dm/{dm_id}` until a terminal
`delivered` or `failed` status is reached.

## 8. One 500-event simulator run showed a recipient reconciliation difference

During a 500-event simulator run, the PseudoGram truth endpoint reported:

- 500 events generated
- 531 webhook deliveries attempted
- 531 webhook requests returned HTTP 200
- 99 expected unique recipients

The application created 94 unique DM jobs for that run.

All 94 application jobs corresponded to users in the simulator's expected
recipient set, and no extra recipients were created.

The five users present in the simulator's expected unique-recipient list
but absent from the application's DM queue were:

- `usr_1505cd24f4`
- `usr_5a75ce5f91`
- `usr_82445fd15`
- `usr_baf89253f9`
- `usr_e93e3725be`

The available truth response exposes the expected unique-recipient set
but does not expose the individual comments or the rule-match decision
for each recipient. Therefore, this test result alone does not establish
that these five users should have received a DM for the configured
`PRICE` rule.

For the 94 jobs that were created, the run completed with:

- 79 DMs sent successfully
- 15 jobs failed after retry handling
- 0 jobs remaining queued
- 61 duplicate DM attempts blocked

This discrepancy should be investigated further if recipient-level
reconciliation against simulator truth is required.

Fix: add event-level reconciliation and diagnostic tooling that records
which incoming comments matched each rule and allows those decisions to
be compared directly with the simulator's truth data.