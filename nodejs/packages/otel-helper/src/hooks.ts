/**
 * `@staffops/otel-helper/hooks` — side-effect entry that installs the module
 * loading hooks (ESM and CJS) before the app's own modules load. Use it first
 * in a custom preload (`import '@staffops/otel-helper/hooks'`), then call
 * `setup({...})`.
 */
import { installHooks } from './internal/hooks';

installHooks();
