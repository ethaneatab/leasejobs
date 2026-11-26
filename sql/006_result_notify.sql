-- 006: tell API processes when a result becomes final, so they can warm their cache.
CREATE FUNCTION notify_succeeded() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('leasejobs_succeeded', NEW.id::text);
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER jobs_notify_succeeded AFTER UPDATE OF state ON jobs
    FOR EACH ROW WHEN (NEW.state = 'SUCCEEDED' AND OLD.state IS DISTINCT FROM 'SUCCEEDED')
    EXECUTE FUNCTION notify_succeeded();
