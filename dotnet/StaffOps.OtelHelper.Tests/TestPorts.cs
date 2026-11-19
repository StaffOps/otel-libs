using System.Net;
using System.Net.Sockets;

namespace StaffOps.OtelHelper.Tests;

/// <summary>Shared helpers for tests that bind local listeners.</summary>
internal static class TestPorts
{
    /// <summary>
    /// A TCP port that was free a moment ago (ephemeral loopback bind, released
    /// at once). The window before the caller binds it is small and accepted.
    /// </summary>
    internal static int FreeTcpPort()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }
}
