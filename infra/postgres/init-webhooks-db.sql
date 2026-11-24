-- webhook-service owns its own database, separate from the payments ledger.
-- Runs only when the Postgres volume is first created.
CREATE DATABASE settle_webhooks;
