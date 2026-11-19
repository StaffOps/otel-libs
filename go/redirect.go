package otelhelper

import (
	"io"
	"net"
	"net/http"
	"strconv"
	"time"
)

// OTLP export redirects (design D1 row "OTLP export redirects", task A31):
// an OTLP/HTTP export answered with a 3xx is a failed export. The helper's
// client never follows it — net/http would replay the request, custom OTLP
// headers included, to whatever host the Location names — and the helper
// reports it once per signal and status code, never printing the Location.

// maxRedirectBodyDrain bounds how much of a 3xx body is read (and discarded)
// before the response reaches the exporter with an empty body.
const maxRedirectBodyDrain = 64 << 10

// defaultOTLPTimeout is the SDK's export timeout when none is resolved.
const defaultOTLPTimeout = 10 * time.Second

// newOTLPHTTPClient is the http.Client passed to an OTLP/HTTP exporter
// (WithHTTPClient). That option replaces the SDK's own client, so this one
// reproduces it: the transport settings of the SDK's ourTransport (v1.47.0,
// itself a copy of net/http's DefaultTransport, proxy from the environment),
// the helper-resolved TLS configuration, and the signal's export timeout as
// the client timeout. It differs only in refusing redirects.
func newOTLPHTTPClient(signal string, e *signalExport) *http.Client {
	transport := &http.Transport{
		Proxy: http.ProxyFromEnvironment,
		DialContext: (&net.Dialer{
			Timeout:   30 * time.Second,
			KeepAlive: 30 * time.Second,
		}).DialContext,
		ForceAttemptHTTP2:     true,
		MaxIdleConns:          100,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   10 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
	}
	if e.tlsConfig != nil {
		transport.TLSClientConfig = e.tlsConfig.Clone()
	}
	timeout := e.timeout
	if timeout <= 0 {
		timeout = defaultOTLPTimeout
	}
	return &http.Client{
		Transport: &redirectReporter{base: transport, signal: signal},
		Timeout:   timeout,
		// Return the 3xx response to the exporter as is: it fails the
		// export (non-retryable status) and the request is never replayed.
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
}

// redirectReporter reports a 3xx answer of the OTLP endpoint. The response
// reaches the exporter with its status and headers but an empty body:
// redirectors often repeat the Location there ("Redirecting to <url>"), and
// the SDK exporters copy the body into their export error.
type redirectReporter struct {
	base   http.RoundTripper
	signal string
}

// RoundTrip implements http.RoundTripper.
func (r *redirectReporter) RoundTrip(req *http.Request) (*http.Response, error) {
	resp, err := r.base.RoundTrip(req)
	if err == nil && resp != nil && resp.StatusCode >= 300 && resp.StatusCode <= 399 {
		reportRedirect(r.signal, resp.StatusCode)
		// Drain a bounded amount so the connection can be reused, then drop it.
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, maxRedirectBodyDrain))
		_ = resp.Body.Close()
		resp.Body = http.NoBody
		resp.ContentLength = 0
	}
	return resp, err
}

// CloseIdleConnections forwards to the base transport, so a caller of
// http.Client.CloseIdleConnections still reaches the transport.
func (r *redirectReporter) CloseIdleConnections() {
	if c, ok := r.base.(interface{ CloseIdleConnections() }); ok {
		c.CloseIdleConnections()
	}
}

// reportRedirect writes the redirect ERROR once per process for each signal
// and status code. The wording is shared with .NET (contract fragment
// "redirects are not followed", then signal= and status=); the Location
// value, which may carry credentials, is never printed.
func reportRedirect(signal string, status int) {
	code := strconv.Itoa(status)
	if _, seen := deprecationsWarned.LoadOrStore("redirect:"+signal+":"+code, true); seen {
		return
	}
	diagLogger.Error("otelhelper: the OTLP endpoint for "+signal+" answered HTTP "+code+
		" (a redirect); redirects are not followed, so these "+signal+
		" are not exported: configure the final endpoint URL",
		"signal", signal, "status", status)
}
