/**
 * `@staffops/otel-helper/register` — preload entry for `node --import` /
 * `node --require`: installs the hooks, then (from task B9) calls `setup()`
 * from the environment. Until B9 it installs the hooks only.
 */
import { runRegister } from './internal/register';

runRegister();
