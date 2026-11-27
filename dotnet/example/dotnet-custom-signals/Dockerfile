# Build context: dotnet/ (the helper source must be in the context):
#   docker build -t dotnet-custom-signals -f dotnet/example/dotnet-custom-signals/Dockerfile dotnet
# Stage "pack" packs StaffOps.OtelHelper from this tree; the example restores
# that .nupkg from the local feed /packages (example/nuget.packages.config),
# never a project reference (task C17). Dockerfile.demo takes CI's packages instead.
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS pack
WORKDIR /src
# Only the core project: the examples use no opt-in package.
COPY Directory.Build.props Directory.Build.targets ./
COPY StaffOps.OtelHelper/ StaffOps.OtelHelper/
RUN dotnet pack StaffOps.OtelHelper/StaffOps.OtelHelper.csproj -c Release -o /packages --nologo

FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY --from=pack /packages/ /packages/
COPY Directory.Build.props Directory.Build.targets ./
COPY example/nuget.packages.config example/
COPY example/dotnet-custom-signals/dotnet-custom-signals.csproj example/dotnet-custom-signals/
# Exactly one core package (StaffOps.OtelHelper.<version>.nupkg): its version is
# passed as OtelHelperVersion, which switches the csproj to a PackageReference.
RUN set -- /packages/StaffOps.OtelHelper.[0-9]*.nupkg && \
    if [ $# -ne 1 ] || [ ! -f "$1" ]; then echo "expected exactly one StaffOps.OtelHelper.<version>.nupkg in /packages, found: $*" >&2; exit 1; fi && \
    ver=$(basename "$1" .nupkg | sed 's/^StaffOps\.OtelHelper\.//') && echo "$ver" > /tmp/helper-version && \
    dotnet restore example/dotnet-custom-signals/dotnet-custom-signals.csproj --configfile example/nuget.packages.config -p:OtelHelperVersion="$ver"
COPY example/dotnet-custom-signals/ example/dotnet-custom-signals/
RUN dotnet publish example/dotnet-custom-signals/dotnet-custom-signals.csproj -c Release --no-restore \
    -p:OtelHelperVersion="$(cat /tmp/helper-version)" -o /app

FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /app .
USER $APP_UID
ENTRYPOINT ["dotnet", "dotnet-custom-signals.dll"]
