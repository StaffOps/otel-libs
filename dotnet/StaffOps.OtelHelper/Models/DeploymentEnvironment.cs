namespace StaffOps.OtelHelper
{
    /// <summary>
    /// Deployment environment of the service, emitted as the
    /// <c>deployment.environment.name</c> resource attribute and used to pick the
    /// default minimum log level. An unknown <c>ENVIRONMENT</c> value resolves to
    /// <see cref="LOCAL"/>.
    /// </summary>
    public enum DeploymentEnvironment
    {
        /// <summary>Local development machine; the default when <c>ENVIRONMENT</c> is unset or unknown.</summary>
        LOCAL,
        /// <summary>Development environment.</summary>
        DEV,
        /// <summary>Homologation (pre-production) environment.</summary>
        HML,
        /// <summary>Production environment.</summary>
        PRD
    }
}
