import assert from "node:assert/strict";
import {
  assertLocalDevelopmentDatabase,
  assertSyncPolicy,
  isLocalDatabaseHost,
  roleBootstrapSql,
  selfHostedRoleBootstrap,
} from "./db-safety.mjs";

const remoteProd = "postgresql://user:pass@prod.example.invalid:5432/prod";
const localTarget = "postgresql://user:pass@127.0.0.1:5432/quant";
const selfHostedTarget = "postgresql://user:pass@quant-db:5432/quant";

assert.equal(isLocalDatabaseHost("[::1]"), true);
assert.doesNotThrow(() => assertLocalDevelopmentDatabase(localTarget));
assert.doesNotThrow(() =>
  assertLocalDevelopmentDatabase("postgresql://user:pass@[::1]:5432/quant"),
);
assert.throws(() => assertLocalDevelopmentDatabase(remoteProd));
assert.throws(() =>
  assertLocalDevelopmentDatabase("postgresql://user:pass@127.0.0.1:15432/quant"),
);
assert.throws(() =>
  assertLocalDevelopmentDatabase("postgresql://user:pass@127.0.0.1:5432/other"),
);
assert.throws(() => assertLocalDevelopmentDatabase(`${localTarget}?host=prod.example.invalid`));
assert.throws(() =>
  assertSyncPolicy({
    prodUrl: remoteProd,
    targetUrl: `${localTarget}?host=quant-db`,
    env: {
      QUANT_ALLOW_REMOTE_SYNC: "1",
      QUANT_ALLOW_PRODUCTION_CUTOVER: "1",
    },
  }),
);
assert.throws(() =>
  assertSyncPolicy({
    prodUrl: "postgresql://reader:pass@127.0.0.1:15432/postgres",
    targetUrl: localTarget,
    env: {},
  }),
);
assert.doesNotThrow(() =>
  assertSyncPolicy({
    prodUrl: "postgresql://reader:pass@127.0.0.1:15432/postgres",
    targetUrl: localTarget,
    env: { QUANT_ALLOW_REMOTE_SYNC: "1" },
  }),
);
assert.throws(() =>
  assertSyncPolicy({
    prodUrl: "https://prod.example.invalid/quant",
    targetUrl: localTarget,
    env: { QUANT_ALLOW_REMOTE_SYNC: "1" },
  }),
);
assert.throws(() => assertSyncPolicy({ prodUrl: remoteProd, targetUrl: localTarget, env: {} }));
assert.throws(() =>
  assertSyncPolicy({
    prodUrl: remoteProd,
    targetUrl: localTarget,
    env: { QUANT_ALLOW_REMOTE_SYNC: "0" },
  }),
);
assert.throws(() =>
  assertSyncPolicy({
    prodUrl: selfHostedTarget,
    targetUrl: selfHostedTarget,
    env: {
      QUANT_ALLOW_REMOTE_SYNC: "1",
      QUANT_ALLOW_PRODUCTION_CUTOVER: "1",
    },
  }),
);
assert.throws(() =>
  assertSyncPolicy({
    prodUrl: "postgresql://reader:pass@localhost:5432/quant",
    targetUrl: localTarget,
    env: { QUANT_ALLOW_REMOTE_SYNC: "1" },
  }),
  /same database/,
);
assert.throws(() =>
  assertSyncPolicy({
    prodUrl: "postgresql://reader:pass@[::1]:5432/quant",
    targetUrl: localTarget,
    env: { QUANT_ALLOW_REMOTE_SYNC: "1" },
  }),
  /same database/,
);
assert.throws(() =>
  assertSyncPolicy({
    prodUrl: "postgresql://reader:pass@quant-db:5432/%71uant",
    targetUrl: selfHostedTarget,
    env: {
      QUANT_ALLOW_REMOTE_SYNC: "1",
      QUANT_ALLOW_PRODUCTION_CUTOVER: "1",
    },
  }),
);
assert.throws(() =>
  assertSyncPolicy({
    prodUrl: remoteProd,
    targetUrl: "postgresql://owner:pass@127.0.0.1:15432/postgres",
    env: { QUANT_ALLOW_REMOTE_SYNC: "1" },
  }),
);
assert.throws(() =>
  assertSyncPolicy({
    prodUrl: remoteProd,
    targetUrl: "postgresql://owner:pass@127.0.0.1:5433/quant",
    env: { QUANT_ALLOW_REMOTE_SYNC: "1" },
  }),
);
assert.throws(() =>
  assertSyncPolicy({
    prodUrl: remoteProd,
    targetUrl: "postgresql://owner:pass@127.0.0.1:5432/other",
    env: { QUANT_ALLOW_REMOTE_SYNC: "1" },
  }),
);
assert.throws(() =>
  assertSyncPolicy({
    prodUrl: remoteProd,
    targetUrl: "postgresql://owner:pass@quant-db:5433/quant",
    env: {
      QUANT_ALLOW_REMOTE_SYNC: "1",
      QUANT_ALLOW_PRODUCTION_CUTOVER: "1",
    },
  }),
);
assert.throws(() =>
  assertSyncPolicy({
    prodUrl: remoteProd,
    targetUrl: "postgresql://owner:pass@quant-db:5432/other",
    env: {
      QUANT_ALLOW_REMOTE_SYNC: "1",
      QUANT_ALLOW_PRODUCTION_CUTOVER: "1",
    },
  }),
);
assert.doesNotThrow(() =>
  assertSyncPolicy({
    prodUrl: remoteProd,
    targetUrl: localTarget,
    env: { QUANT_ALLOW_REMOTE_SYNC: "1" },
  }),
);
assert.throws(() =>
  assertSyncPolicy({
    prodUrl: remoteProd,
    targetUrl: "postgresql://user:pass@other-db.example.invalid:5432/quant",
    env: {
      QUANT_ALLOW_REMOTE_SYNC: "1",
      QUANT_ALLOW_PRODUCTION_CUTOVER: "1",
    },
  }),
);
assert.throws(() =>
  assertSyncPolicy({
    prodUrl: remoteProd,
    targetUrl: selfHostedTarget,
    env: { QUANT_ALLOW_REMOTE_SYNC: "1" },
  }),
);
assert.doesNotThrow(() =>
  assertSyncPolicy({
    prodUrl: remoteProd,
    targetUrl: selfHostedTarget,
    env: {
      QUANT_ALLOW_REMOTE_SYNC: "1",
      QUANT_ALLOW_PRODUCTION_CUTOVER: "1",
    },
  }),
);

assert.equal(selfHostedRoleBootstrap({ databaseUrl: selfHostedTarget, env: {} }), null);
assert.throws(() =>
  selfHostedRoleBootstrap({
    databaseUrl: remoteProd,
    env: {
      QUANT_SETUP_ROLES: "1",
      PG_APP_PASSWORD: "AppPassword_1",
      PG_BACKUP_PASSWORD: "BackupPassword_1",
    },
  }),
);
assert.throws(() =>
  selfHostedRoleBootstrap({
    databaseUrl: "postgresql://owner:pass@127.0.0.1:15432/postgres",
    env: {
      QUANT_SETUP_ROLES: "1",
      PG_APP_PASSWORD: "AppPassword_1",
      PG_BACKUP_PASSWORD: "BackupPassword_1",
    },
  }),
);
assert.throws(() =>
  selfHostedRoleBootstrap({
    databaseUrl: selfHostedTarget,
    env: {
      QUANT_SETUP_ROLES: "1",
      PG_APP_PASSWORD: "unsafe-password",
      PG_BACKUP_PASSWORD: "BackupPassword_1",
    },
  }),
);
const roles = selfHostedRoleBootstrap({
  databaseUrl: selfHostedTarget,
  env: {
    QUANT_SETUP_ROLES: "1",
    PG_APP_PASSWORD: "AppPassword_1",
    PG_BACKUP_PASSWORD: "BackupPassword_1",
  },
});
assert.throws(() =>
  selfHostedRoleBootstrap({
    databaseUrl: `${localTarget}?host=quant-db`,
    env: {
      QUANT_SETUP_ROLES: "1",
      PG_APP_PASSWORD: "AppPassword_1",
      PG_BACKUP_PASSWORD: "BackupPassword_1",
    },
  }),
);
assert.match(roleBootstrapSql(roles), /CREATE ROLE quant_app/);
assert.match(roleBootstrapSql(roles), /CREATE ROLE quant_backup/);

console.log("database safety self-test passed");
