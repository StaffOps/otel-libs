FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY OtelHelper/OtelHelper.csproj OtelHelper/
COPY example/dotnet-backend/dotnet-backend.csproj example/dotnet-backend/
COPY example/Protos/ example/Protos/
RUN dotnet restore example/dotnet-backend/dotnet-backend.csproj
COPY OtelHelper/ OtelHelper/
COPY example/dotnet-backend/ example/dotnet-backend/
RUN dotnet publish example/dotnet-backend/dotnet-backend.csproj -c Release -o /app

FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /app .
ENV ASPNETCORE_HTTP_PORTS=5100
ENV ASPNETCORE_URLS=http://+:5100
EXPOSE 5100
ENTRYPOINT ["dotnet", "dotnet-backend.dll"]
