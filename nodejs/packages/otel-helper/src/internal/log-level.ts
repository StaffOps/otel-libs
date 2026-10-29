/**
 * Deployment environment and the log level it implies (contract R4, ADR-003).
 */
import { trimWS } from './env';

/**
 * The four behavior-switching values of `deployment.environment.name`. Any
 * other value is emitted verbatim and behaves like an unknown environment.
 */
export type DeploymentEnvironment = 'LOCAL' | 'DEV' | 'HML' | 'PRD';

/** Log levels of the helper's logger, lowest first. */
export type LogLevel = 'debug' | 'info' | 'warn' | 'error';

/** Every {@link LogLevel}, lowest first. */
export const LOG_LEVELS: readonly LogLevel[] = Object.freeze(['debug', 'info', 'warn', 'error']);

const KNOWN: ReadonlySet<string> = new Set<DeploymentEnvironment>(['LOCAL', 'DEV', 'HML', 'PRD']);

/** The known environment `value` names (trimmed, case-insensitive), else `undefined`. */
export function knownEnvironment(value: string | undefined): DeploymentEnvironment | undefined {
  if (value === undefined) return undefined;
  const upper = trimWS(value).toUpperCase();
  return KNOWN.has(upper) ? (upper as DeploymentEnvironment) : undefined;
}

/**
 * Default log level for an environment (case-insensitive), as Go
 * `DefaultLogLevel` and Python `get_default_log_level`: `LOCAL` debug,
 * `DEV`/`HML` info, `PRD` warn; unset or any other value info (ADR-003).
 * `debug` true always gives debug.
 */
export function defaultLogLevel(environment?: string, debug = false): LogLevel {
  if (debug) return 'debug';
  switch (knownEnvironment(environment)) {
    case 'LOCAL':
      return 'debug';
    case 'PRD':
      return 'warn';
    default:
      return 'info';
  }
}
