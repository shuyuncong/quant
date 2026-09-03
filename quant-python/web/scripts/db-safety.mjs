const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "::1"]);

export function normalizeHostname(hostname) {
  return hostname.trim().toLowerCase().replace(/^\[|\]$/g, "");
}

function databaseLocation(urlString, variableName) {
  try {
    const parsed = new URL(urlString);
    if (parsed.protocol !== "postgres:" && parsed.protocol !== "postgresql:") {
      throw new Error(`${variableName} must use the postgres or postgresql protocol`);
    }
    if (parsed.search || parsed.hash) {
      throw new Error(
        `${variableName} must not contain query parameters or fragments; ` +
          "connection options such as host and port could bypass the safety policy",
      );
    }
    return {
      host: normalizeHostname(parsed.hostname),
      port: parsed.port || "5432",
      database: decodeURIComponent(parsed.pathname.replace(/^\//, "")),
    };
  } catch (error) {
    if (error instanceof Error && error.message.startsWith(variableName)) throw error;
    throw new Error(`${variableName} is not a valid PostgreSQL URL`);
  }
}

export function isLocalDatabaseHost(hostname) {
  return LOCAL_HOSTS.has(normalizeHostname(hostname));
}

export function assertLocalDevelopmentDatabase(databaseUrl) {
  const location = databaseLocation(databaseUrl, "DATABASE_URL");
  if (!isLocalDatabaseHost(location.host)) {
    throw new Error(
      `DATABASE_URL points to non-local host "${location.host}"; ` +
        "development must use the local Docker PostgreSQL instance",
    );
  }
  if (location.port !== "5432" || location.database !== "quant") {
    throw new Error(
      "DATABASE_URL must point to the local quant database on port 5432; " +
        `received port ${location.port}, database "${location.database}"`,
    );
  }
  return location;
}

export function assertSyncPolicy({ prodUrl, targetUrl, env = process.env }) {
  const prod = databaseLocation(prodUrl, "PROD_DATABASE_URL");
  const target = databaseLocation(targetUrl, "LOCAL_DATABASE_URL");
  const prodHost = prod.host;
  const targetHost = target.host;
  const sameHost =
    prod.host === target.host ||
    (isLocalDatabaseHost(prod.host) && isLocalDatabaseHost(target.host));

  if (
    sameHost &&
    prod.port === target.port &&
    prod.database === target.database
  ) {
    throw new Error("PROD_DATABASE_URL and LOCAL_DATABASE_URL must not point to the same database");
  }

  if (env.QUANT_ALLOW_REMOTE_SYNC !== "1") {
    throw new Error(
      `PROD_DATABASE_URL points to source host "${prodHost}"; ` +
        "set QUANT_ALLOW_REMOTE_SYNC=1 only for an approved one-way sync, including a loopback relay",
    );
  }

  if (targetHost === "quant-db") {
    if (target.port !== "5432" || target.database !== "quant") {
      throw new Error(
        "LOCAL_DATABASE_URL production target must be exactly quant-db:5432/quant",
      );
    }
    if (env.QUANT_ALLOW_PRODUCTION_CUTOVER !== "1") {
      throw new Error(
        "LOCAL_DATABASE_URL points to production target quant-db; " +
          "set QUANT_ALLOW_PRODUCTION_CUTOVER=1 only inside the approved cutover window",
      );
    }
  } else if (isLocalDatabaseHost(targetHost)) {
    if (target.port !== "5432" || target.database !== "quant") {
      throw new Error(
        "LOCAL_DATABASE_URL local target must be exactly loopback:5432/quant; " +
          "a loopback relay or another local database is not a safe writable target",
      );
    }
  } else {
    throw new Error(
      `LOCAL_DATABASE_URL points to unsupported remote target "${targetHost}"; ` +
        "only a local PostgreSQL instance or the production-only quant-db service is allowed",
    );
  }

  return { prodHost, targetHost };
}

function requiredRolePassword(env, name) {
  const value = env[name]?.trim();
  if (!value) throw new Error(`${name} is required when QUANT_SETUP_ROLES=1`);
  if (!/^[A-Za-z0-9_]+$/.test(value)) {
    throw new Error(`${name} may contain only letters, digits, and underscores`);
  }
  return value;
}

export function selfHostedRoleBootstrap({ databaseUrl, env = process.env }) {
  if (env.QUANT_SETUP_ROLES !== "1") return null;

  const location = databaseLocation(databaseUrl, "DATABASE_URL");
  if (
    (location.host !== "quant-db" && !isLocalDatabaseHost(location.host)) ||
    location.port !== "5432" ||
    location.database !== "quant"
  ) {
    throw new Error(
      "QUANT_SETUP_ROLES=1 is only allowed for the quant database on " +
        `quant-db:5432 or loopback:5432, received "${location.host}:${location.port}/${location.database}"`,
    );
  }

  return {
    appPassword: requiredRolePassword(env, "PG_APP_PASSWORD"),
    backupPassword: requiredRolePassword(env, "PG_BACKUP_PASSWORD"),
  };
}

export function roleBootstrapSql({ appPassword, backupPassword }) {
  return `DO $roles$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'quant_app') THEN
    CREATE ROLE quant_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      PASSWORD '${appPassword}';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'quant_backup') THEN
    CREATE ROLE quant_backup WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
      PASSWORD '${backupPassword}';
  END IF;
END
$roles$;`;
}
