/**
 * Type surface for the development database gate in `db-safety.mjs`.
 *
 * Only the entry point used by `backtest-worker.ts` is declared; the safety
 * policy itself stays in the single `.mjs` implementation.
 */
export interface LocalDatabaseLocation {
  host: string;
  port: string;
  database: string;
}

export function assertLocalDevelopmentDatabase(databaseUrl: string): LocalDatabaseLocation;
