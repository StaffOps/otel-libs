namespace StaffOps.OtelHelper
{
    /// <summary>
    /// The deployment environment names the helper knows. <see cref="TelemetryOptions.Environment"/>
    /// is a string: these constants are the values that pick a default log level
    /// (<see cref="TelemetryOptions.GetDefaultLogLevel"/>, matched ignoring ASCII case);
    /// any value, known or not, is emitted as written.
    /// </summary>
    public static class DeploymentEnvironments
    {
        /// <summary>Local development machine.</summary>
        public const string LOCAL = "LOCAL";

        /// <summary>Development environment.</summary>
        public const string DEV = "DEV";

        /// <summary>Homologation (pre-production) environment.</summary>
        public const string HML = "HML";

        /// <summary>Production environment.</summary>
        public const string PRD = "PRD";

        internal static readonly string[] Known = { LOCAL, DEV, HML, PRD };
    }
}
