# Durable acceptance for the CosmoTech regional recovery

The feature is disabled by default. With `ORDERS_DURABLE_OUTBOX=true`, the API
requires an explicit stable order ID and commits an immutable acceptance record to
`cosmotech-orders-outbox` in us-east-1 before returning HTTP 202. A duplicate returns
the original event; conflicting content returns 409. Journal failure returns 503
without exposing SDK details. Direct SQS publishing remains the default legacy path.

`python -m orders.producer.outbox_dispatcher` publishes the durable journal through
bounded leases. It requires `ORDERS_OUTBOX_ACTIVATION_FILE` with a current source
region activation (positive epoch, expiry within five minutes), the exact primary
queue URL and temporary application-role credentials. No lease means no dispatch.
An uncertain SQS send can duplicate an event; stable IDs and the consumer's atomic
business transaction remain required. Never discard pending journal records when
rolling the feature back. Drain/reconcile first and retain the journal and backups.

The source journal, PITR, hourly Ohio backup copy and bounded publisher identity are
managed separately by Infrastructure IaC. The primary API receives only GetItem /
PutItem on the new journal. The publisher receives Query / UpdateItem on that journal
and SendMessage on the existing primary queue. The deployment must supply regional
configuration and a reviewed activation control; this change alone does not enable
or certify regional failover. Source writer fencing is mandatory before Ohio writes.

The shared core is vendored from the reviewed IaC integration kit; its original and
adapted hashes are recorded in integration-manifest.json. Validation: 79 tests pass,
including six native boto3/moto conditional-write/lease/pagination/uncertain-send
cases and four producer acceptance/SDK-error/duplicate/conflict cases. Existing
legacy API/consumer tests still pass. Repository lint and deployment validation pass.
