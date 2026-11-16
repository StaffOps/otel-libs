# demo-dotnet-backend (specs/demo-environment, task DE3d). Build context: the
# repository root, so the shared contract under demo/ is visible (DE-D3):
#   docker build --platform linux/amd64 -t otel-libs-demo-dotnet-backend:dev -f dotnet/demo/Backend/Dockerfile .
# Dockerfile.dockerignore next to this file limits the context to what the
# build needs. The helper comes in by project reference (no package feed).
# The SDK stage runs on the build machine's platform: the output is portable IL
# (framework-dependent, no runtime identifier), so the linux/amd64 runtime
# stage runs it and protoc (Grpc.Tools) never runs under emulation. Base
# images are pinned by digest (multi-arch index of the 10.0 tag, 2026-11-16).
FROM --platform=$BUILDPLATFORM mcr.microsoft.com/dotnet/sdk:10.0@sha256:fe0fd4e93bd2fbb47c883b7a6369e5c8d3db8bf01cdc5981f6ee35fbdf3ff4fb AS build
WORKDIR /src
# Project files first: the restore layer is reused while only sources change.
COPY dotnet/Directory.Build.props dotnet/Directory.Build.targets dotnet/
COPY dotnet/StaffOps.OtelHelper/StaffOps.OtelHelper.csproj dotnet/StaffOps.OtelHelper/
COPY dotnet/StaffOps.OtelHelper.Sql/StaffOps.OtelHelper.Sql.csproj dotnet/StaffOps.OtelHelper.Sql/
COPY dotnet/demo/Directory.Build.props dotnet/demo/
COPY dotnet/demo/Common/Demo.Common.csproj dotnet/demo/Common/
COPY dotnet/demo/Backend/Demo.Backend.csproj dotnet/demo/Backend/
RUN dotnet restore dotnet/demo/Backend/Demo.Backend.csproj
COPY demo/proto/ demo/proto/
COPY demo/sql/ demo/sql/
COPY dotnet/StaffOps.OtelHelper/ dotnet/StaffOps.OtelHelper/
COPY dotnet/StaffOps.OtelHelper.Sql/ dotnet/StaffOps.OtelHelper.Sql/
COPY dotnet/demo/Common/ dotnet/demo/Common/
COPY dotnet/demo/Backend/ dotnet/demo/Backend/
RUN dotnet publish dotnet/demo/Backend/Demo.Backend.csproj -c Release --no-restore -o /app

FROM mcr.microsoft.com/dotnet/aspnet:10.0@sha256:bfd912c83295ce66d4b706ebb8541b9f14259b0e76fbb88b2751f58ce2399d2e
WORKDIR /app
COPY --from=build /app .
# The shared schema at the DEMO_SCHEMA_FILE default (DE-D7 r3.4).
COPY demo/sql/schema.sql /etc/otel-libs-demo/schema.sql
# Non-root: the image's numeric app user (runAsNonRoot can verify it).
USER $APP_UID
ENTRYPOINT ["dotnet", "Demo.Backend.dll"]
