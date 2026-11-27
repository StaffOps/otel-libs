// ESM entry for `@staffops/otel-helper/internal/db` (see esm/index.mjs). Not public API.
import cjs from '../dist/db.js';

export const {
  ATTR_DB_SYSTEM,
  ATTR_DB_SYSTEM_NAME,
  ENV_SEMCONV_OPT_IN,
  LEGACY_DB_SYSTEM,
  dbMeterProvider,
  dbTracerProvider,
  legacyKeysEnabled,
  legacyKeysMode,
  withLegacyKey,
} = cjs;
