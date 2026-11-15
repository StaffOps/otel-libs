# syntax=docker/dockerfile:1
# .NET e2e smoke image (testing/e2e/dotnet-smoke) for a Kubernetes Job.
# Build context: the repo tree root (testing/k8s/build-images.sh exports HEAD).
# As run-dotnet.sh: StaffOps.OtelHelper is packed from source into /packages and
# the smoke program restores it from there (no project reference).
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1
COPY dotnet/ /work/dotnet/
RUN dotnet pack /work/dotnet/StaffOps.OtelHelper/StaffOps.OtelHelper.csproj -c Release -o /packages --nologo -v quiet
COPY testing/e2e/dotnet-smoke/ /work/smoke/
RUN set -eu; \
    set -- /packages/StaffOps.OtelHelper.[0-9]*.nupkg; \
    [ $# -eq 1 ] && [ -f "$1" ] || { echo "expected one StaffOps.OtelHelper nupkg, found: $*" >&2; exit 1; }; \
    ver=$(basename "$1" .nupkg | sed 's/^StaffOps\.OtelHelper\.//'); \
    echo "packed StaffOps.OtelHelper $ver"; \
    dotnet restore /work/smoke/dotnet-smoke.csproj --configfile /work/dotnet/example/nuget.packages.config \
      -p:OtelHelperVersion="$ver" -v quiet; \
    dotnet publish /work/smoke/dotnet-smoke.csproj -c Release -o /out --nologo -v quiet --no-restore \
      -p:OtelHelperVersion="$ver"

# aspnet, not runtime: StaffOps.OtelHelper references the Microsoft.AspNetCore.App
# shared framework (on runtime:8.0 the app fails: "No frameworks were found").
FROM mcr.microsoft.com/dotnet/aspnet:8.0-noble-chiseled
WORKDIR /app
COPY --from=build /out /app
USER 65532:65532
# One run: span e2e-smoke-span, counter e2e.smoke.runs, WARN log e2e-smoke-log.
# Exits 1 when ForceFlushOtelHelper reports an incomplete flush.
ENTRYPOINT ["dotnet", "/app/dotnet-smoke.dll"]
