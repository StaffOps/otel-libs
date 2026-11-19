using System.Runtime.InteropServices;

namespace StaffOps.OtelHelper.Profiling
{
    /// <summary>
    /// Inspects the environment for the native Pyroscope profiler prerequisites.
    ///
    /// The native profiler is configured entirely via process environment variables
    /// read at CLR bootstrap (the lib cannot set them in time). This type exists to
    /// detect a missing/incomplete bootstrap and surface a clear warning, instead of
    /// the profiler silently doing nothing.
    /// </summary>
    /// <remarks>
    /// Only variable names are reported (<see cref="Unmet"/>, <see cref="DescribeMissing"/>), never
    /// their values: <c>PYROSCOPE_SERVER_ADDRESS</c> may carry credentials, and the Pyroscope
    /// authentication variables are not read at all.
    /// </remarks>
    public sealed class ProfilingPrerequisites
    {
        /// <summary>CLR profiler bootstrap — must be "1" for the native profiler to load.</summary>
        public const string EnableProfilingEnvVar = "CORECLR_ENABLE_PROFILING";

        /// <summary>CLSID of the profiler the CLR loads; must be <see cref="PyroscopeProfilerClsid"/>.</summary>
        public const string ProfilerEnvVar = "CORECLR_PROFILER";

        /// <summary>Path of the native profiler library (<c>Pyroscope.Profiler.Native.so</c>).</summary>
        /// <remarks>
        /// The runtime reads the architecture-specific variable first (<c>CORECLR_PROFILER_PATH_64</c> on
        /// x64, <c>CORECLR_PROFILER_PATH_ARM64</c> on Arm64, <c>_32</c> / <c>_ARM32</c> on 32-bit), then this one.
        /// </remarks>
        public const string ProfilerPathEnvVar = "CORECLR_PROFILER_PATH";

        /// <summary>Linux only: preloads <c>Pyroscope.Linux.ApiWrapper.x64.so</c>, which the managed API needs.</summary>
        public const string PreloadEnvVar = "LD_PRELOAD";

        /// <summary>Pyroscope master switch.</summary>
        public const string ProfilingEnabledEnvVar = "PYROSCOPE_PROFILING_ENABLED";

        /// <summary>Pyroscope server address (where profiles are pushed).</summary>
        public const string ServerAddressEnvVar = "PYROSCOPE_SERVER_ADDRESS";

        /// <summary>Pyroscope application name (becomes the service_name label).</summary>
        public const string ApplicationNameEnvVar = "PYROSCOPE_APPLICATION_NAME";

        /// <summary>The CLSID the Pyroscope .NET profiler registers (Grafana Pyroscope documentation).</summary>
        public const string PyroscopeProfilerClsid = "{BD1A650D-AC5D-4896-B64F-D6FA25D6B26A}";

        /// <summary>File name fragment of the Pyroscope API wrapper expected in <c>LD_PRELOAD</c>.</summary>
        internal const string ApiWrapperName = "Pyroscope.Linux.ApiWrapper";

        /// <summary>True when the CLR profiler bootstrap is enabled.</summary>
        public bool ClrProfilerEnabled { get; init; }

        /// <summary>The value of <c>CORECLR_PROFILER</c>, if any.</summary>
        public string? Profiler { get; init; }

        /// <summary>The variable that supplies the profiler path (architecture-specific first), or null.</summary>
        public string? ProfilerPathVariable { get; init; }

        /// <summary>True when the profiler path names an existing file.</summary>
        public bool ProfilerPathExists { get; init; }

        /// <summary>True when the platform needs <c>LD_PRELOAD</c> (Linux).</summary>
        public bool PreloadRequired { get; init; }

        /// <summary>True when <c>LD_PRELOAD</c> holds the Pyroscope API wrapper.</summary>
        public bool PreloadHasApiWrapper { get; init; }

        /// <summary>True when Pyroscope profiling is enabled.</summary>
        public bool ProfilingEnabled { get; init; }

        /// <summary>The configured Pyroscope server address, if any. Never written to diagnostics.</summary>
        public string? ServerAddress { get; init; }

        /// <summary>The configured Pyroscope application name, if any.</summary>
        public string? ApplicationName { get; init; }

        /// <summary>
        /// The unmet prerequisites, in check order: a variable name when it is unset or off, or
        /// <c>NAME: reason</c> (<c>not the Pyroscope profiler</c>, <c>file not found</c>, <c>no Pyroscope.Linux.ApiWrapper</c>).
        /// Empty when every prerequisite is met.
        /// </summary>
        public IReadOnlyList<string> Unmet
        {
            get
            {
                var unmet = new List<string>();
                if (!ClrProfilerEnabled) unmet.Add($"{EnableProfilingEnvVar}=1");
                if (string.IsNullOrWhiteSpace(Profiler)) unmet.Add(ProfilerEnvVar);
                else if (!string.Equals(Profiler, PyroscopeProfilerClsid, StringComparison.OrdinalIgnoreCase))
                    unmet.Add($"{ProfilerEnvVar}: not the Pyroscope profiler");
                if (ProfilerPathVariable is null) unmet.Add(ProfilerPathEnvVar);
                else if (!ProfilerPathExists) unmet.Add($"{ProfilerPathVariable}: file not found");
                if (PreloadRequired && !PreloadHasApiWrapper) unmet.Add($"{PreloadEnvVar}: no {ApiWrapperName}");
                if (!ProfilingEnabled) unmet.Add($"{ProfilingEnabledEnvVar}=1");
                if (string.IsNullOrWhiteSpace(ServerAddress)) unmet.Add(ServerAddressEnvVar);
                return unmet;
            }
        }

        /// <summary>
        /// True when the minimum prerequisites for the native profiler to run are present.
        /// </summary>
        public bool IsSatisfied => Unmet.Count == 0;

        /// <summary>
        /// Reads the prerequisite environment variables set by the deployment.
        /// </summary>
        public static ProfilingPrerequisites FromEnvironment()
            => From(Environment.GetEnvironmentVariable, RuntimeInformation.IsOSPlatform(OSPlatform.Linux), RuntimeInformation.ProcessArchitecture, File.Exists);

        /// <summary>The prerequisites read through <paramref name="env"/> for a platform (tests).</summary>
        internal static ProfilingPrerequisites From(Func<string, string?> env, bool isLinux, Architecture architecture, Func<string, bool> fileExists)
        {
            var pathVariable = PathVariables(architecture).FirstOrDefault(name => !string.IsNullOrWhiteSpace(env(name)));
            var preload = env(PreloadEnvVar);
            return new ProfilingPrerequisites
            {
                ClrProfilerEnabled = IsClrEnabled(env(EnableProfilingEnvVar)),
                Profiler = env(ProfilerEnvVar),
                ProfilerPathVariable = pathVariable,
                ProfilerPathExists = pathVariable is not null && fileExists(env(pathVariable)!),
                PreloadRequired = isLinux,
                PreloadHasApiWrapper = preload is not null && preload.Contains(ApiWrapperName, StringComparison.Ordinal),
                ProfilingEnabled = IsPyroscopeTrue(env(ProfilingEnabledEnvVar)),
                ServerAddress = env(ServerAddressEnvVar),
                ApplicationName = env(ApplicationNameEnvVar),
            };
        }

        /// <summary>
        /// Returns a human-readable description of what is missing, or null when satisfied.
        /// </summary>
        public string? DescribeMissing()
        {
            var unmet = Unmet;
            return unmet.Count == 0 ? null : string.Join(", ", unmet);
        }

        /// <summary>The profiler path variables the runtime reads, architecture-specific first.</summary>
        internal static IEnumerable<string> PathVariables(Architecture architecture)
        {
            var specific = architecture switch
            {
                Architecture.X64 => "_64",
                Architecture.X86 => "_32",
                Architecture.Arm64 => "_ARM64",
                Architecture.Arm => "_ARM32",
                _ => null,
            };
            if (specific is not null) yield return ProfilerPathEnvVar + specific;
            yield return ProfilerPathEnvVar;
        }

        // The runtime reads CORECLR_ENABLE_PROFILING as a number: "1" enables, "true" does not.
        private static bool IsClrEnabled(string? value) => value == "1";

        // Pyroscope's own boolean rule (Pyroscope 1.2.0 StringExtensions.ToBoolean): 1, t, y, true, yes.
        private static bool IsPyroscopeTrue(string? value)
        {
            var text = value;
            return text is not null
                && (text is "1" || text.Equals("t", StringComparison.OrdinalIgnoreCase) || text.Equals("y", StringComparison.OrdinalIgnoreCase)
                    || text.Equals("true", StringComparison.OrdinalIgnoreCase) || text.Equals("yes", StringComparison.OrdinalIgnoreCase));
        }
    }
}
