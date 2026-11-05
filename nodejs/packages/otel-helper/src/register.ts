/**
 * `@staffops/otel-helper/register` — preload entry for `node --import` /
 * `node --require`: installs the module hooks, then calls `setup()` from the
 * environment. Apps that pass options in code preload their own file instead:
 * `import '@staffops/otel-helper/hooks'`, then `setup({...})`.
 */
import { runRegister } from './internal/register';

runRegister();
