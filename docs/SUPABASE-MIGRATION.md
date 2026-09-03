# Supabase Deployment

The Web console now stores business data in Supabase PostgreSQL 17.6. The
Python signal engine keeps its local SQLite cache and outbox.

Set these server-side variables for both `docker-compose.yml` and
`docker-compose.oracle.yml` before starting the service:

```bash
export DATABASE_URL='postgresql://postgres.<project-ref>:<url-encoded-password>@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres'
export DATABASE_SSL_REJECT_UNAUTHORIZED=true
export DATABASE_POOL_MAX=5
```

`DATABASE_URL` is the only required variable. Use the Supabase **Session
pooler** connection string (port `5432`) and URL-encode the database password.
The session pooler is required because the scheduler holds a PostgreSQL
advisory-lock session.

The Supabase publishable and secret API keys are not used by this application;
the Web server connects directly with PostgreSQL through `DATABASE_URL`. Do not
put either key in browser-side environment variables.

The old local relay-to-Supabase workflow is retired. Local development must use
the Docker PostgreSQL instance on `127.0.0.1:5432` with scheduler disabled:

```dotenv
DATABASE_URL=postgresql://quant:<local-password>@127.0.0.1:5432/quant
DATABASE_SSL_MODE=disable
SCHEDULER_DISABLED=1
```

When `DATABASE_URL` points directly to the Supabase pooler, omit
`DATABASE_SSL_SERVERNAME` and keep `DATABASE_SSL_REJECT_UNAUTHORIZED=true`.
`DATABASE_POOL_MAX` defaults to `5`; `WEB_DATA_DIR`, `PYTHON_BIN`, Tushare,
notification, and signal-delivery variables are optional feature settings.

For Oracle automated deployments, put these values in
`/opt/docker/quant/quant-python/quant-python/.env` and run `chmod 600 .env`.
Docker Compose reads that file when the non-interactive deployment script runs;
the repository Docker ignore rules keep it outside image layers.

## Oracle Upgrade Checklist

Supabase is used only by the legacy Oracle production deployment; local
development uses its own Docker PostgreSQL and must never share the production
database. If the Supabase migration has already been completed, do **not** run
`npm run db:migrate` again on Oracle. Configure the production `DATABASE_URL`
and rebuild the container:

```bash
cd /opt/docker/quant/quant-python/quant-python
umask 077
nano .env
chmod 600 .env
git pull --ff-only origin master
docker compose -f docker-compose.oracle.yml config --quiet
docker compose -f docker-compose.oracle.yml up -d --build
docker compose -f docker-compose.oracle.yml exec quant-web npm run db:verify
docker compose -f docker-compose.oracle.yml ps
curl -fsS http://127.0.0.1:3111/api/config >/dev/null && echo OK
```

The `.env` file should contain at least:

```dotenv
DATABASE_URL=postgresql://postgres.qutqrxicwrnorvujdrvp:<URL-encoded-password>@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres
DATABASE_SSL_REJECT_UNAUTHORIZED=true
DATABASE_POOL_MAX=5
```

Do not set `DATABASE_SSL_SERVERNAME` on Oracle. It belonged to the retired
local relay workflow. The Publishable key and Secret key are not required
by this server-side PostgreSQL connection.

If the Oracle host has SQLite data that is newer than the rows already in
Supabase, stop before rebuilding and reconcile that data first. Do not copy
`web/data/app.db` into the production container as a Web database; only the
Python signal state (`signal_monitor.db*`) and analysis output remain local to
the Oracle host.

The historical local SQLite-to-Supabase migration is complete and retired.
Never point `db:migrate` or `db:rollback-snapshot` at production; both commands
are now restricted to `loopback:5432/quant`. Local test, backtest, signal-state,
and output data must not be uploaded to production.

The only supported business-data direction is production to local through the
guarded `npm run db:sync-from-prod` command, which overwrites the local database.
Production schema changes use only the idempotent `npm run db:setup` workflow,
followed by `npm run db:verify`. Never put `DATABASE_URL` in Git or in a Docker
build context. The repository `.dockerignore` excludes `web/.env*`; provide the
value only via Compose runtime environment or a secret manager.

PostgreSQL contract tests must use an explicitly configured local disposable
database. Normal `npm test` does not connect to Supabase or the production database.
