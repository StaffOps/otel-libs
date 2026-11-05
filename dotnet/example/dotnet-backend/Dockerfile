FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY Directory.Build.props Directory.Build.targets ./
COPY StaffOps.OtelHelper/StaffOps.OtelHelper.csproj StaffOps.OtelHelper/
COPY example/dotnet-backend/dotnet-backend.csproj example/dotnet-backend/
COPY example/Protos/ example/Protos/
RUN dotnet restore example/dotnet-backend/dotnet-backend.csproj
COPY StaffOps.OtelHelper/ StaffOps.OtelHelper/
COPY example/dotnet-backend/ example/dotnet-backend/
RUN dotnet publish example/dotnet-backend/dotnet-backend.csproj -c Release -o /app

FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /app .
ENV ASPNETCORE_HTTP_PORTS=5100
ENV ASPNETCORE_URLS=http://+:5100
EXPOSE 5100
ENTRYPOINT ["dotnet", "dotnet-backend.dll"]
