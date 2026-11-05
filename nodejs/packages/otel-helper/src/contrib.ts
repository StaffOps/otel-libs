/**
 * `@staffops/otel-helper/internal/contrib` — NOT public API (Node design
 * D-JS9, D-JS11). The contrib-instance machinery the opt-in instrumentation
 * packages (`-sql`, `-redis`, `-aws`) share, so it has one implementation and
 * not a copy per package. Those packages peer-depend on the core at the same
 * version; this entry may change in any release, so applications must not
 * import it.
 *
 * - {@link contribRegistry}: one registry per opt-in package, holding its
 *   per-process state — the contrib instrumentations built once and shared by
 *   every factory result (a second instance would register its require hook
 *   after the library was handed to require-in-the-middle, which never calls
 *   it for that copy), their enable counts (several results enable a contrib
 *   instrumentation once; the last one disabled turns it off), the tracer and
 *   meter bindings (the same TracerProvider is not re-bound; every meter bind
 *   is a new generation, an older one records nothing), the re-require of
 *   library files loaded before the hooks existed, and the one-time reports.
 * - {@link appDirectories}, {@link resolvesFromApp}, {@link contribLoadError}:
 *   the library detection and the load errors in the contract D14 words (a
 *   cause is named only when it is certain; an unresolved library is reported,
 *   never an error — a nested, non-hoisted copy is invisible to resolution).
 * - {@link report}, {@link reportOnce}: the helper's diagnostics sink, so the
 *   packages' warnings reach the same sink (and the parity `helper_log`) as
 *   the core's, with `origin: 'helper'`.
 *
 * The span and metric rules stay with each package (and `internal/db` for the
 * database ones): a registry only calls the package's `tracerProvider` and
 * `meterProvider` hooks.
 *
 * @internal
 * @packageDocumentation
 */
import { createRequire } from 'node:module';
import * as path from 'node:path';
import type { LoggerProvider, MeterProvider, TracerProvider } from '@opentelemetry/api';
import type { Instrumentation, InstrumentationConfig } from '@opentelemetry/instrumentation';
import { redactText, report, type DiagnosticValue } from './internal/diagnostics';
import { debugEnabled } from './internal/diag-logger';

export { report, reportOnce } from './internal/diagnostics';
export type { DiagnosticValue } from './internal/diagnostics';

/**
 * How a library report is emitted on the first enable, on the helper's
 * diagnostics sink: `warn` always, `debug` only when `OTEL_LOG_LEVEL` asks
 * for debug records.
 *
 * @internal
 */
export type ReportLevel = 'warn' | 'debug';

/**
 * What an opt-in package tells its registry.
 *
 * @internal
 */
export interface ContribSpec<L extends string> {
  /** The package name: the result's `instrumentationName` and the prefix of its reports. */
  readonly name: string;
  /** The package version: the result's `instrumentationVersion`. */
  readonly version: string;
  /** The libraries, in the order they are instrumented and reported. */
  readonly libraries: readonly L[];
  /** The contrib module of each library, which is also its `instrumentationName`. */
  readonly contrib: Readonly<Record<L, string>>;
  /** Package names that count as the library being present in the app. */
  readonly packages: Readonly<Record<L, readonly string[]>>;
  /**
   * Builds the contrib instrumentation of `library` (with the package's fixed
   * configuration); throws the package's D14 load error when its module does
   * not load. Called once per process per library.
   */
  readonly build: (library: L) => Instrumentation;
  /**
   * Files each library's instrumentation patches, required again on the first
   * enable when they are cached: [package, file inside it, '' = its main entry].
   */
  readonly patchedFiles: Readonly<Record<L, readonly (readonly [string, string])[]>>;
  /** The TracerProvider a child is bound to, from the one `setup()` passes. */
  readonly tracerProvider: (library: L, provider: TracerProvider) => TracerProvider;
  /** The MeterProvider a child is bound to; `current()` is false once a newer bind replaced this one. */
  readonly meterProvider: (library: L, provider: MeterProvider, current: () => boolean) => MeterProvider;
}

/**
 * What a factory returns: one `Instrumentation` covering the selected libraries.
 *
 * @internal
 */
export interface ContribInstrumentation<L extends string> extends Instrumentation {
  /** The libraries this result instruments. */
  readonly libraries: readonly L[];
  /** The contrib instrumentation names it covers: the core rejects another instrumentation for them (M2). */
  readonly coveredInstrumentations: readonly string[];
}

/**
 * Which libraries a factory call selects: `true` named (a load failure
 * throws, an unresolved library is warned), `undefined` default (skipped on a
 * load failure, an unresolved library is a debug line), `false` left out.
 *
 * @internal
 */
export type ContribSelection<L extends string> = Readonly<Partial<Record<L, boolean | undefined>>>;

/**
 * The per-process state of one opt-in package.
 *
 * @internal
 */
export interface ContribRegistry<L extends string> {
  /**
   * The factory body: the selected libraries' shared children, with the
   * reports for libraries that did not load (skipped in default mode) or do
   * not resolve. Throws the load error of a library named `true`, or the first
   * one when none loads.
   */
  instrument(selection: ContribSelection<L>): ContribInstrumentation<L>;
  /** Test seam: the contrib instrumentations of a result of this registry (`[]` for anything else). */
  children(instrumentation: unknown): readonly Instrumentation[];
}

/**
 * Directories the app resolves packages from: its entry script's, the
 * working directory, then the main module's `node_modules` lookup paths.
 *
 * @internal
 */
export function appDirectories(
  argv1: string | undefined = process.argv[1],
  cwd: string = process.cwd(),
  mainPaths: readonly string[] = require.main?.paths ?? [],
): string[] {
  const dirs: string[] = [];
  if (argv1 !== undefined && argv1 !== '') dirs.push(path.dirname(path.resolve(cwd, argv1)));
  dirs.push(cwd, ...mainPaths);
  return [...new Set(dirs)];
}

/**
 * Whether one of `names` resolves from `dirs` (never loaded: requiring the
 * library before its instrumentation is enabled would leave the app's copy
 * unpatched).
 *
 * @internal
 */
export function resolvesFromApp(names: readonly string[], dirs: readonly string[] = appDirectories()): boolean {
  return names.some((name) => {
    try {
      require.resolve(name, { paths: [...dirs] });
      return true;
    } catch {
      return false;
    }
  });
}

/**
 * The report for a library the app dirs do not resolve; it is still
 * instrumented. WARN when the library was named `true`, debug in default mode
 * (an absent library is the common case).
 */
function unresolvedReport(pkg: string, library: string, dirs: readonly string[], level: ReportLevel): LibraryReport {
  return { message: `${pkg}: a library could not be resolved from the app directories; instrumented if it loads`, attrs: { library, dirs: oneLine(dirs.join(', ')) }, level };
}

/**
 * One library report: a constant message (per package), the variable parts
 * (library, directories, the load error) in attributes, quoted by the sink so
 * they can never forge a record.
 */
interface LibraryReport {
  readonly message: string;
  readonly attrs: Readonly<Record<string, DiagnosticValue>>;
  readonly level: ReportLevel;
}

/**
 * The error for a failed load of a contrib instrumentation module (`module`):
 * "not installed" only when the module itself is the one not found, else the
 * load error verbatim (first line: the require stack lists local paths). The
 * returned error's `cause` is the original load error, for debugging (never
 * printed by the helper). `label` names the instrumentation (`SQL`), `pkg` the
 * opt-in package that depends on `module`.
 *
 * @internal
 */
export function contribLoadError(label: string, pkg: string, module: string, err: unknown): Error {
  const message = err instanceof Error ? err.message : String(err);
  const newline = message.indexOf('\n');
  const first = newline < 0 ? message : message.slice(0, newline);
  const code = typeof err === 'object' && err !== null ? (err as { code?: unknown }).code : undefined;
  if (code === 'MODULE_NOT_FOUND' && first.includes(`'${module}'`)) {
    return new Error(`${label} instrumentation not installed: ${module} is missing. Reinstall ${pkg} (it depends on ${module}).`, { cause: err });
  }
  return new Error(`failed to import the ${label} instrumentation: ${first}`, { cause: err });
}

/** Longest `error` attribute of a library report, in code points (then `…`). */
const MAX_ERROR_ATTRIBUTE = 512;

/** The first line of `text`, redacted, cut to {@link MAX_ERROR_ATTRIBUTE} code points. */
function errorAttribute(text: string): string {
  const newline = text.search(/[\r\n\u2028\u2029]/);
  const redacted = redactText(oneLine(newline < 0 ? text : text.slice(0, newline)));
  const points = Array.from(redacted);
  return points.length > MAX_ERROR_ATTRIBUTE ? `${points.slice(0, MAX_ERROR_ATTRIBUTE).join('')}…` : redacted;
}

/** The message of a load error (anything thrown); never throws. */
function loadErrorText(err: unknown): string {
  try {
    return err instanceof Error && typeof err.message === 'string' ? err.message : String(err);
  } catch {
    return '<unprintable error>';
  }
}

/** `text` on one line: a control character in a load error would split the diagnostic record. */
function oneLine(text: string): string {
  // eslint-disable-next-line no-control-regex -- matching control characters is the point
  return text.replace(/[\x00-\x1f\x7f-\x9f\u2028\u2029]+/g, ' ');
}

/** Cached files of `pkg` (every copy in `require.cache`): `file` inside it, or its main entry when that is cached. */
function cachedFiles(pkg: string, file: string): string[] {
  const marker = `${path.sep}node_modules${path.sep}${pkg.split('/').join(path.sep)}${path.sep}`;
  const roots = new Set<string>();
  for (const loaded of Object.keys(require.cache)) {
    const at = loaded.lastIndexOf(marker);
    if (at >= 0) roots.add(loaded.slice(0, at + marker.length));
  }
  const files: string[] = [];
  for (const root of roots) {
    let target: string;
    if (file !== '') {
      target = path.join(root, file);
    } else {
      try {
        target = createRequire(path.join(root, 'package.json')).resolve(root);
      } catch {
        continue; // a copy whose main entry does not resolve was never required as the package
      }
    }
    if (require.cache[target] !== undefined) files.push(target);
  }
  return files;
}

/**
 * The registry of one opt-in package. Create it once, at module load of the
 * package (its state is the package's per-process state).
 *
 * @internal
 */
export function contribRegistry<L extends string>(spec: ContribSpec<L>): ContribRegistry<L> {
  /** One shared contrib instrumentation and its state. */
  interface Child {
    readonly library: L;
    readonly instrumentation: Instrumentation;
    /** Results that enabled it (it is on while > 0). */
    enables: number;
    /** The TracerProvider it was last bound to (the input, before wrapping): the same again is not re-bound. */
    boundTracer: TracerProvider | undefined;
    /** The current meter bind: instruments of an older one record nothing. */
    meterGeneration: object | undefined;
  }
  const built = new Map<L, Child>();
  const preloadChecked = new Set<L>();
  const reportedUnresolved = new Set<string>();
  const childrenOf = new WeakMap<object, readonly Instrumentation[]>();
  /** A `setConfig()` call is warned on the next enable (when `setup()` uses the result). */
  let setConfigCalled = false;
  let warnedSetConfig = false;

  function child(library: L): Child {
    let entry = built.get(library);
    if (entry === undefined) {
      const instrumentation = spec.build(library);
      // Off until setup() enables it: InstrumentationBase enables itself in its
      // constructor, bound to the API's global providers.
      instrumentation.disable();
      entry = { library, instrumentation, enables: 0, boundTracer: undefined, meterGeneration: undefined };
      built.set(library, entry);
    }
    return entry;
  }

  /**
   * Libraries the app loaded before the instrumentation's require hooks
   * existed are unknown to require-in-the-middle, which patches on `require`.
   * Requiring the cached files again by path hands them to the hooks, which
   * patch their prototypes in place, so the app's copy (and clients it already
   * built) is instrumented — the core does the same for express and grpc-js.
   */
  function patchPreloaded(libraries: readonly L[]): void {
    for (const library of libraries) {
      if (preloadChecked.has(library)) continue;
      preloadChecked.add(library);
      for (const [pkg, file] of spec.patchedFiles[library]) {
        for (const cached of cachedFiles(pkg, file)) {
          // eslint-disable-next-line @typescript-eslint/no-require-imports -- re-require so the require hook patches it
          require(cached);
        }
      }
    }
  }

  function warnSetConfig(): void {
    if (warnedSetConfig) return;
    warnedSetConfig = true;
    report(`${spec.name}: setConfig() is ignored; the configuration is fixed and enabled follows setup()/shutdown()`);
  }

  class Result implements ContribInstrumentation<L> {
    readonly instrumentationName = spec.name;
    readonly instrumentationVersion = spec.version;
    readonly libraries: readonly L[];
    readonly coveredInstrumentations: readonly string[];
    readonly #children: readonly Child[];
    readonly #unresolved: readonly LibraryReport[];
    #enabled = false;

    constructor(children: readonly Child[], unresolved: readonly LibraryReport[]) {
      this.#children = children;
      this.#unresolved = unresolved;
      this.libraries = Object.freeze(children.map((c) => c.library));
      this.coveredInstrumentations = Object.freeze(this.libraries.map((library) => spec.contrib[library]));
      childrenOf.set(this, Object.freeze(children.map((c) => c.instrumentation)));
    }

    enable(): void {
      if (this.#enabled) return;
      this.#enabled = true;
      for (const c of this.#children) {
        c.enables += 1;
        if (c.enables === 1) c.instrumentation.enable();
      }
      // Reported when setup() enables the result, not when the factory runs.
      if (setConfigCalled) warnSetConfig();
      for (const { message, attrs, level } of this.#unresolved) {
        const key = `${level}:${message}:${JSON.stringify(attrs)}`;
        if (reportedUnresolved.has(key)) continue;
        if (level === 'debug' && !debugEnabled()) continue;
        reportedUnresolved.add(key);
        report(message, attrs, { origin: 'helper', level });
      }
      patchPreloaded(this.libraries);
    }

    disable(): void {
      if (!this.#enabled) return;
      this.#enabled = false;
      for (const c of this.#children) {
        c.enables -= 1;
        if (c.enables === 0) c.instrumentation.disable();
      }
    }

    setTracerProvider(tracerProvider: TracerProvider): void {
      for (const c of this.#children) {
        if (c.boundTracer === tracerProvider) continue;
        c.boundTracer = tracerProvider;
        c.instrumentation.setTracerProvider(spec.tracerProvider(c.library, tracerProvider));
      }
    }

    setMeterProvider(meterProvider: MeterProvider): void {
      for (const c of this.#children) {
        // Always a new bind: instrumentation-pg re-creates its instruments and
        // moves the pool max onto them; the older instruments go quiet.
        // Current during the call (pg subtracts the old max inside it), restored if it throws.
        const previous = c.meterGeneration;
        const generation = {};
        c.meterGeneration = generation;
        try {
          c.instrumentation.setMeterProvider(spec.meterProvider(c.library, meterProvider, () => c.meterGeneration === generation));
        } catch (err) {
          c.meterGeneration = previous;
          throw err;
        }
      }
    }

    setLoggerProvider(loggerProvider: LoggerProvider): void {
      for (const c of this.#children) c.instrumentation.setLoggerProvider?.(loggerProvider);
    }

    /** Ignored (warned once per process, on the next enable): the contrib configurations are fixed by the package and `enabled` follows `enable()`/`disable()`. */
    setConfig(_config: InstrumentationConfig): void {
      setConfigCalled = true;
      if (this.#enabled) warnSetConfig();
    }

    /** `{ enabled }`: the real state of this result. */
    getConfig(): InstrumentationConfig {
      return { enabled: this.#enabled };
    }
  }

  function instrument(selection: ContribSelection<L>): ContribInstrumentation<L> {
    const dirs = appDirectories();
    const children: Child[] = [];
    const unresolved: LibraryReport[] = [];
    let firstError: unknown;
    for (const library of spec.libraries.filter((l) => selection[l] !== false)) {
      let entry: Child;
      try {
        entry = child(library);
      } catch (err) {
        // Named explicitly: certain, thrown. Default mode: skipped (thrown below if none loads).
        if (selection[library] === true) throw err;
        firstError ??= err;
        // Reported on the first enable when another library is instrumented.
        // The load error's first line (it carries the "Reinstall …" hint; the
        // rest is a require stack of local paths), redacted, capped.
        unresolved.push({ message: `${spec.name}: a library is not instrumented`, attrs: { library, error: errorAttribute(loadErrorText(err)) }, level: 'warn' });
        continue;
      }
      children.push(entry);
      // Named true but not resolvable: WARN (the app expects it), naming the
      // entry directory and cwd; the full lookup chain goes on a debug line.
      // Default mode: usually just absent (a pg-only app has no mysql2), so debug only.
      if (!resolvesFromApp(spec.packages[library], dirs)) {
        if (selection[library] === true) unresolved.push(unresolvedReport(spec.name, library, appDirectories(undefined, undefined, []), 'warn'));
        unresolved.push(unresolvedReport(spec.name, library, dirs, 'debug'));
      }
    }
    if (children.length === 0) throw firstError;
    return new Result(children, unresolved);
  }

  return {
    instrument,
    children: (instrumentation) => childrenOf.get(instrumentation as object) ?? [],
  };
}
