-- Schema of the otel-libs demo (specs/demo-environment, DE-D3, DE-D4).
-- Each language's backend applies this file at start, connected as its own
-- role demo_<lang> to its own database demo_<lang>; every statement is
-- idempotent, so a restart re-applies it safely.
--
-- Send it without bind parameters as one command. PostgreSQL runs a
-- multi-statement simple-query string as one implicit transaction, so the
-- advisory lock below is held until the last statement and two backends
-- starting at once apply the schema one after the other. Drivers that split
-- the text into statements (Npgsql sends a multi-statement command as an
-- extended-protocol batch with one Sync, which is also one implicit
-- transaction) must keep the dollar-quoted function body intact.

SELECT pg_advisory_xact_lock(4242);

CREATE TABLE IF NOT EXISTS orders (
    id         text PRIMARY KEY,
    customer   text,
    amount     numeric,
    scenario   text,
    updated_at timestamptz
);

-- The db_error scenario inserts the row dup-<run>. This function raises
-- SQLSTATE 23505 unconditionally, with the id in the PRIMARY message (every
-- driver exposes it; DETAIL is not relied on). No seed row exists, so no
-- insert of a dup- id ever succeeds and every db_error fails the same way.
CREATE OR REPLACE FUNCTION orders_raise_dup() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23505',
        MESSAGE = format('duplicate order %s', NEW.id);
END;
$$;

-- Fires only for dup- ids: the ok upserts (ok-<seq mod 500>) never reach it.
CREATE OR REPLACE TRIGGER orders_dup
    BEFORE INSERT ON orders
    FOR EACH ROW
    WHEN (NEW.id LIKE 'dup-%')
    EXECUTE FUNCTION orders_raise_dup();
