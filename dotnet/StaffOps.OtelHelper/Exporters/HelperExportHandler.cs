using System.Globalization;
using System.Net.Http;
using StaffOps.OtelHelper.Diagnostics;

namespace StaffOps.OtelHelper.Exporters
{
    /// <summary>
    /// The outer handler of every helper exporter client (ADR-013 client duties, task C8). It
    /// forwards both <c>Send</c> (HTTP/protobuf exports call the synchronous path) and
    /// <c>SendAsync</c> (gRPC), and:
    /// <list type="bullet">
    /// <item>on gRPC, sends the export deadline as <c>grpc-timeout</c> (the .NET OTLP exporter sends
    /// none; <see cref="HttpClient.Timeout"/> enforces the same deadline on the client side);</item>
    /// <item>refuses redirects loudly: the inner handler never follows them, and a <c>3xx</c> answer
    /// is reported once per signal and status code, naming both and never the <c>Location</c>
    /// value (it may carry credentials). The export then fails as the SDK reports any non-2xx answer.</item>
    /// </list>
    /// </summary>
    internal sealed class HelperExportHandler : DelegatingHandler
    {
        private readonly string _signal;
        private readonly string? _grpcTimeout;
        private readonly IOtelHelperDiagnostics _diagnostics;

        /// <param name="inner">The transport (a <see cref="SocketsHttpHandler"/> with redirects off).</param>
        /// <param name="signal">The signal, named in diagnostics.</param>
        /// <param name="grpcTimeoutMs">The export deadline to send as <c>grpc-timeout</c>; null for HTTP/protobuf.</param>
        /// <param name="diagnostics">The diagnostics sink.</param>
        internal HelperExportHandler(HttpMessageHandler inner, string signal, int? grpcTimeoutMs, IOtelHelperDiagnostics diagnostics)
            : base(inner)
        {
            _signal = signal;
            _grpcTimeout = grpcTimeoutMs is { } ms ? GrpcTimeout(ms) : null;
            _diagnostics = diagnostics;
        }

        /// <summary>The <c>grpc-timeout</c> value of a deadline in milliseconds (at most 8 digits, gRPC over HTTP/2).</summary>
        internal static string GrpcTimeout(int milliseconds)
            => milliseconds <= 99_999_999
                ? milliseconds.ToString(CultureInfo.InvariantCulture) + "m"
                : ((milliseconds + 999) / 1000).ToString(CultureInfo.InvariantCulture) + "S";

        /// <inheritdoc />
        protected override HttpResponseMessage Send(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            Prepare(request);
            var response = base.Send(request, cancellationToken);
            Inspect(response);
            return response;
        }

        /// <inheritdoc />
        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            Prepare(request);
            var response = await base.SendAsync(request, cancellationToken).ConfigureAwait(false);
            Inspect(response);
            return response;
        }

        private void Prepare(HttpRequestMessage request)
        {
            if (_grpcTimeout is not null && !request.Headers.Contains("grpc-timeout"))
            {
                request.Headers.TryAddWithoutValidation("grpc-timeout", _grpcTimeout);
            }
        }

        private void Inspect(HttpResponseMessage response)
        {
            var status = (int)response.StatusCode;
            if (status is < 300 or > 399)
            {
                return;
            }

            var code = status.ToString(CultureInfo.InvariantCulture);
            _diagnostics.ReportOnce($"redirect:{_signal}:{code}", DiagnosticLevel.Error,
                $"the OTLP endpoint for {_signal} answered HTTP {code} (a redirect); redirects are not followed, so these {_signal} are not exported: configure the final endpoint URL",
                ("signal", _signal), ("status", code));
        }
    }
}
