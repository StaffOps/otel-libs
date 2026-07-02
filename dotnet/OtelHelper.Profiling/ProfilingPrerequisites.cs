namespace OtelHelper.Profiling
{
    /// <summary>
    /// Inspects the environment for the native Pyroscope profiler prerequisites.
    ///
    /// The native profiler is configured entirely via process environment variables
    /// read at CLR bootstrap (the lib cannot set them in time). This type exists to
    /// detect a missing/incomplete bootstrap and surface a clear warning, instead of
    /// the profiler silently doing nothing.
    /// </summary>
    public sealed class ProfilingPrerequisites
    {
        /// <summary>CLR profiler bootstrap — must be "1" for the native profiler to load.</summary>
        public const string EnableProfilingEnvVar = "CORECLR_ENABLE_PROFILING";

        /// <summary>Pyroscope master switch.</summary>
        public const string ProfilingEnabledEnvVar = "PYROSCOPE_PROFILING_ENABLED";

        /// <summary>Pyroscope server address (where profiles are pushed).</summary>
        public const string ServerAddressEnvVar = "PYROSCOPE_SERVER_ADDRESS";

        /// <summary>Pyroscope application name (becomes the service_name label).</summary>
        public const string ApplicationNameEnvVar = "PYROSCOPE_APPLICATION_NAME";

        /// <summary>True when the CLR profiler bootstrap is enabled.</summary>
        public bool ClrProfilerEnabled { get; init; }

        /// <summary>True when Pyroscope profiling is enabled.</summary>
        public bool ProfilingEnabled { get; init; }

        /// <summary>The configured Pyroscope server address, if any.</summary>
        public string? ServerAddress { get; init; }

        /// <summary>The configured Pyroscope application name, if any.</summary>
        public string? ApplicationName { get; init; }

        /// <summary>
        /// True when the minimum prerequisites for the native profiler to run are present.
        /// </summary>
        public bool IsSatisfied =>
            ClrProfilerEnabled
            && ProfilingEnabled
            && !string.IsNullOrWhiteSpace(ServerAddress);

        /// <summary>
        /// Reads the prerequisite environment variables set by the deployment.
        /// </summary>
        public static ProfilingPrerequisites FromEnvironment() => new()
        {
            ClrProfilerEnabled = IsTruthy(Environment.GetEnvironmentVariable(EnableProfilingEnvVar)),
            ProfilingEnabled = IsTruthy(Environment.GetEnvironmentVariable(ProfilingEnabledEnvVar)),
            ServerAddress = Environment.GetEnvironmentVariable(ServerAddressEnvVar),
            ApplicationName = Environment.GetEnvironmentVariable(ApplicationNameEnvVar),
        };

        /// <summary>
        /// Returns a human-readable description of what is missing, or null when satisfied.
        /// </summary>
        public string? DescribeMissing()
        {
            if (IsSatisfied) return null;

            var missing = new List<string>();
            if (!ClrProfilerEnabled) missing.Add($"{EnableProfilingEnvVar}=1");
            if (!ProfilingEnabled) missing.Add($"{ProfilingEnabledEnvVar}=1");
            if (string.IsNullOrWhiteSpace(ServerAddress)) missing.Add(ServerAddressEnvVar);

            return string.Join(", ", missing);
        }

        // Pyroscope/CLR treat "1" and "true" as enabled.
        private static bool IsTruthy(string? value)
            => value is "1" || string.Equals(value, "true", StringComparison.OrdinalIgnoreCase);
    }
}
