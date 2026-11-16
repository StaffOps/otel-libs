// demo-dotnet-backend (specs/demo-environment, task DE3d): the gRPC server of
// demo.v1.OrderService and the PostgreSQL client. Every setting comes from the
// environment (DR0; DE-D13 r3.3):
//   DEMO_GRPC_PORT (50051), DEMO_PG_HOST (demo-postgres[:5432]), DEMO_PG_DB and
//   DEMO_PG_USER (demo_dotnet), DEMO_PG_PASSWORD_FILE (required, read once).
// Also serves grpc.health.v1.Health on the same port (probe target). The schema is read from
// DEMO_SCHEMA_FILE (default /etc/otel-libs-demo/schema.sql, in the image; embedded copy otherwise).
// The gRPC server spans are RPC-shaped only with
// OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION=true (DE-D2).
using Microsoft.AspNetCore.Server.Kestrel.Core;
using Microsoft.Extensions.Diagnostics.HealthChecks;
using Npgsql;
using StaffOps.OtelHelper.Sql;
using StaffOps.OtelLibs.Demo;
using StaffOps.OtelLibs.Demo.Backend;

var builder = WebApplication.CreateBuilder(args);
// The helper's exporter (or its stdout fallback) is the only log output.
builder.Logging.ClearProviders();

var grpcPort = DemoSettings.Port("DEMO_GRPC_PORT", 50051);
builder.WebHost.ConfigureKestrel(kestrel =>
{
    // Istio ambient terminates TLS in front of the pod; the :scheme then differs (dotnet/HOW-TO.md).
    kestrel.AllowAlternateSchemes = true;
    kestrel.ListenAnyIP(grpcPort, listen => listen.Protocols = HttpProtocols.Http2);
});

builder.Services.AddDemoTelemetry();
builder.Services.AddOtelHelperSql();
builder.Services.AddGrpc();
// grpc.health.v1.Health/Check and Watch for a gRPC readiness/liveness probe (DE4). The server
// listens only after the schema is applied; one liveness check makes the overall service ""
// SERVING (with no registered check the health service answers UNKNOWN, measured in the smoke).
builder.Services.AddGrpcHealthChecks().AddCheck("self", () => HealthCheckResult.Healthy());

var (pgHost, pgPort) = DemoSettings.HostPort(DemoSettings.Get("DEMO_PG_HOST", "demo-postgres"), 5432);
var connection = new NpgsqlConnectionStringBuilder
{
    Host = pgHost,
    Port = pgPort,
    Database = DemoSettings.Get("DEMO_PG_DB", "demo_" + DemoSettings.Lang),
    Username = DemoSettings.Get("DEMO_PG_USER", "demo_" + DemoSettings.Lang),
    // Read once from the mounted file; the connection string lives in memory only, never logged.
    Password = DemoSettings.ReadSecretFile("DEMO_PG_PASSWORD_FILE"),
    // No GSSAPI: the runtime image has no libgssapi_krb5, and Npgsql's default (Prefer) tries to load it.
    GssEncryptionMode = GssEncryptionMode.Disable,
};
builder.Services.AddSingleton(_ => NpgsqlDataSource.Create(connection));
builder.Services.AddSingleton<IOrderStore, PostgresOrderStore>();
builder.Services.AddSingleton(sp => new OrderProcessor(
    sp.GetRequiredService<IOrderStore>(), sp.GetRequiredService<ILogger<OrderProcessor>>(), DemoSettings.CurrentRunId()));
// Hosted services start before the server listens: no call is served before the schema exists.
builder.Services.AddHostedService<SchemaInitializer>();

var app = builder.Build();
app.MapGrpcService<OrderGrpcService>();
app.MapGrpcHealthChecksService();
app.Run();
