package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

const exportMethod = "/opentelemetry.proto.collector.trace.v1.TraceService/Export"

// start serves the receiver on a loopback port (h2c + HTTP/1.1, as a PLAIN
// listener) and returns its address.
func start(t *testing.T, hang *hanger) string {
	t.Helper()
	resetRequests()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := newServer("", handler(newGRPCServer(), false, hang), false)
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close() })
	return ln.Addr().String()
}

func resetRequests() { mu.Lock(); reqs = nil; mu.Unlock() }

func recorded() []rec {
	mu.Lock()
	defer mu.Unlock()
	return append([]rec(nil), reqs...)
}

func grpcConn(t *testing.T, addr string) *grpc.ClientConn {
	t.Helper()
	conn, err := grpc.NewClient(addr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return conn
}

func export(ctx context.Context, conn *grpc.ClientConn) error {
	return conn.Invoke(ctx, exportMethod, &emptypb.Empty{}, &emptypb.Empty{})
}

// h2cClient speaks HTTP/2 with prior knowledge, as the OTLP/HTTP exporters do
// against an h2c endpoint.
func h2cClient(timeout time.Duration) *http.Client {
	p := new(http.Protocols)
	p.SetUnencryptedHTTP2(true)
	return &http.Client{Timeout: timeout, Transport: &http.Transport{Protocols: p}}
}

func noRedirect(timeout time.Duration) *http.Client {
	return &http.Client{Timeout: timeout, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}
}

// waitHeld polls until the hanger holds want calls.
func waitHeld(t *testing.T, h *hanger, want int) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		if held, _ := h.stats(); held == want {
			return
		}
		if time.Now().After(deadline) {
			held, evicted := h.stats()
			t.Fatalf("held %d (evicted %d), want %d", held, evicted, want)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func isTimeout(err error) bool {
	var ne net.Error
	return errors.As(err, &ne) && ne.Timeout()
}

// --- hang mode -----------------------------------------------------------

func TestHangGRPCDeadlineExceeded(t *testing.T) {
	h := newHanger(8)
	conn := grpcConn(t, start(t, h))
	const timeout = 300 * time.Millisecond
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()
	began := time.Now()
	err := export(ctx, conn)
	elapsed := time.Since(began)
	if got := status.Code(err); got != codes.DeadlineExceeded {
		t.Fatalf("code %v (%v), want DeadlineExceeded (the connection is up, only the answer is missing)", got, err)
	}
	if elapsed < timeout {
		t.Fatalf("returned after %v, before the %v deadline", elapsed, timeout)
	}
	r := recorded()
	if len(r) != 1 || r[0].Proto != "grpc" || r[0].Path != exportMethod || r[0].Timeout == "" {
		t.Fatalf("recorded %+v, want one gRPC Export with a grpc-timeout", r)
	}
	waitHeld(t, h, 0) // the client's cancel releases the call
	if _, evicted := h.stats(); evicted != 0 {
		t.Fatalf("evicted %d, want 0", evicted)
	}
}

func TestHangHTTPTimesOutWithoutAResponse(t *testing.T) {
	body := bytes.Repeat([]byte{0x0a}, 4096)
	for name, client := range map[string]*http.Client{
		"http1": {Timeout: 300 * time.Millisecond},
		"h2c":   h2cClient(300 * time.Millisecond),
	} {
		t.Run(name, func(t *testing.T) {
			h := newHanger(8)
			addr := start(t, h)
			began := time.Now()
			resp, err := client.Post("http://"+addr+"/v1/traces", "application/x-protobuf", bytes.NewReader(body))
			if resp != nil {
				_ = resp.Body.Close()
				t.Fatalf("got a response %q, want none", resp.Status)
			}
			if !isTimeout(err) {
				t.Fatalf("err %v, want a client timeout", err)
			}
			if elapsed := time.Since(began); elapsed < 300*time.Millisecond {
				t.Fatalf("returned after %v, before the timeout", elapsed)
			}
			r := recorded()
			if len(r) != 1 || r[0].Proto != "http" || r[0].Bytes != len(body) {
				t.Fatalf("recorded %+v, want one HTTP request with the %d-byte body read", r, len(body))
			}
			waitHeld(t, h, 0)
		})
	}
}

func TestHangIgnoresRedirectAndEchoTriggers(t *testing.T) {
	h := newHanger(8)
	addr := start(t, h)
	req, _ := http.NewRequest(http.MethodPost, "http://"+addr+"/parity-redirect-c/v1/traces", strings.NewReader("x"))
	req.Header.Set(echoErrorHeader, "1")
	resp, err := noRedirect(200 * time.Millisecond).Do(req)
	if resp != nil {
		_ = resp.Body.Close()
		t.Fatalf("got %q, want no answer in hang mode", resp.Status)
	}
	if !isTimeout(err) {
		t.Fatalf("err %v, want a timeout", err)
	}
	conn := grpcConn(t, addr)
	ctx, cancel := context.WithTimeout(metadata.AppendToOutgoingContext(context.Background(), grpcRedirectHeader, "307"), 200*time.Millisecond)
	defer cancel()
	if got := status.Code(export(ctx, conn)); got != codes.DeadlineExceeded {
		t.Fatalf("gRPC with the redirect header: %v, want DeadlineExceeded", got)
	}
	waitHeld(t, h, 0)
}

func TestHangCapClosesTheOldest(t *testing.T) {
	h := newHanger(2)
	addr := start(t, h)
	type result struct {
		err     error
		elapsed time.Duration
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	results := make([]chan result, 3)
	for i := range results {
		results[i] = make(chan result, 1)
		req, _ := http.NewRequestWithContext(ctx, http.MethodPost, "http://"+addr+"/v1/logs", strings.NewReader("x"))
		go func(c chan result) {
			began := time.Now()
			resp, err := (&http.Client{Timeout: 30 * time.Second}).Do(req)
			if resp != nil {
				_ = resp.Body.Close()
				err = errors.New("unexpected response " + resp.Status)
			}
			c <- result{err, time.Since(began)}
		}(results[i])
		if i < 2 {
			waitHeld(t, h, i+1) // a deterministic arrival order
		}
	}
	select {
	case r := <-results[0]:
		if r.err == nil || isTimeout(r.err) || strings.Contains(r.err.Error(), "unexpected response") {
			t.Fatalf("oldest call: %v, want a closed connection (no response, no timeout)", r.err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("the oldest call was not closed when the cap was exceeded")
	}
	waitHeld(t, h, 2)
	if _, evicted := h.stats(); evicted != 1 {
		t.Fatalf("evicted %d, want 1", evicted)
	}
	for _, c := range results[1:] {
		select {
		case r := <-c:
			t.Fatalf("a call within the cap returned: %v", r.err)
		default:
		}
	}
	cancel()
	for _, c := range results[1:] {
		<-c
	}
	waitHeld(t, h, 0)
}

func TestHangerStatsOutsideHangMode(t *testing.T) {
	var h *hanger
	if held, evicted := h.stats(); held != 0 || evicted != 0 {
		t.Fatalf("nil hanger: %d, %d", held, evicted)
	}
}

func TestParseOptions(t *testing.T) {
	tests := []struct {
		name    string
		args    []string
		env     map[string]string
		hang    bool
		max     int
		wantErr string
	}{
		{name: "defaults", max: defaultHangMax},
		{name: "flag", args: []string{"--hang"}, hang: true, max: defaultHangMax},
		{name: "single dash flag and cap", args: []string{"-hang", "-hang-max=3"}, hang: true, max: 3},
		{name: "env", env: map[string]string{"HANG": "1", "HANG_MAX": "16"}, hang: true, max: 16},
		{name: "env true", env: map[string]string{"HANG": "true"}, hang: true, max: defaultHangMax},
		{name: "flag beats env", args: []string{"--hang=false", "--hang-max", "5"}, env: map[string]string{"HANG": "true", "HANG_MAX": "9"}, max: 5},
		{name: "env false", env: map[string]string{"HANG": "0"}, max: defaultHangMax},
		{name: "bad HANG", env: map[string]string{"HANG": "yes please"}, wantErr: "HANG="},
		{name: "bad HANG_MAX", env: map[string]string{"HANG_MAX": "many"}, wantErr: "HANG_MAX="},
		{name: "zero cap", args: []string{"--hang-max=0"}, wantErr: "at least 1"},
		{name: "negative env cap", env: map[string]string{"HANG_MAX": "-4"}, wantErr: "at least 1"},
		{name: "unknown flag", args: []string{"--sink"}, wantErr: "sink"},
		{name: "positional", args: []string{"hang"}, wantErr: "unexpected argument"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			hang, limit, err := parseOptions(tt.args, func(k string) string { return tt.env[k] })
			if tt.wantErr != "" {
				if err == nil || !strings.Contains(err.Error(), tt.wantErr) {
					t.Fatalf("err %v, want one containing %q", err, tt.wantErr)
				}
				return
			}
			if err != nil || hang != tt.hang || limit != tt.max {
				t.Fatalf("got (%v, %d, %v), want (%v, %d, nil)", hang, limit, err, tt.hang, tt.max)
			}
		})
	}
}

// --- existing modes, unchanged --------------------------------------------

func TestDefaultGRPCExportSucceeds(t *testing.T) {
	conn := grpcConn(t, start(t, nil))
	ctx, cancel := context.WithTimeout(metadata.AppendToOutgoingContext(context.Background(), "x-parity-case", "c1"), 5*time.Second)
	defer cancel()
	if err := export(ctx, conn); err != nil {
		t.Fatalf("export: %v", err)
	}
	r := recorded()
	if len(r) != 1 || r[0].Proto != "grpc" || r[0].TLS || r[0].Headers["x-parity-case"] != "c1" {
		t.Fatalf("recorded %+v", r)
	}
}

func TestDefaultGRPCEchoErrors(t *testing.T) {
	conn := grpcConn(t, start(t, nil))
	for mode, want := range map[string]string{"1": "echo: request headers", "token": "invalid token tok-1"} {
		ctx, cancel := context.WithTimeout(metadata.AppendToOutgoingContext(context.Background(),
			echoErrorHeader, mode, "x-parity-auth", "Bearer tok-1"), 5*time.Second)
		err := export(ctx, conn)
		cancel()
		if status.Code(err) != codes.InvalidArgument || !strings.Contains(status.Convert(err).Message(), want) {
			t.Fatalf("mode %s: %v, want InvalidArgument with %q", mode, err, want)
		}
	}
}

func TestDefaultGRPCRedirectHeader(t *testing.T) {
	conn := grpcConn(t, start(t, nil))
	ctx, cancel := context.WithTimeout(metadata.AppendToOutgoingContext(context.Background(), grpcRedirectHeader, "307"), 5*time.Second)
	defer cancel()
	err := export(ctx, conn)
	if code := status.Code(err); code == codes.OK || code == codes.DeadlineExceeded {
		t.Fatalf("code %v (%v), want an immediate HTTP-status error", code, err)
	}
	if r := recorded(); len(r) != 1 || r[0].Headers[grpcRedirectHeader] != "307" {
		t.Fatalf("recorded %+v", r)
	}
}

func TestDefaultHTTPExportSucceeds(t *testing.T) {
	addr := start(t, nil)
	for name, client := range map[string]*http.Client{"http1": {Timeout: 5 * time.Second}, "h2c": h2cClient(5 * time.Second)} {
		t.Run(name, func(t *testing.T) {
			resetRequests()
			req, _ := http.NewRequest(http.MethodPost, "http://"+addr+"/v1/metrics", strings.NewReader("abc"))
			req.Header.Set("Content-Encoding", "gzip")
			resp, err := client.Do(req)
			if err != nil {
				t.Fatal(err)
			}
			b, _ := io.ReadAll(resp.Body)
			_ = resp.Body.Close()
			if resp.StatusCode != http.StatusOK || len(b) != 0 || resp.Header.Get("Content-Type") != "application/x-protobuf" {
				t.Fatalf("got %d %q body %q", resp.StatusCode, resp.Header.Get("Content-Type"), b)
			}
			if r := recorded(); len(r) != 1 || r[0].Bytes != 3 || r[0].Encoding != "gzip" || r[0].Path != "/v1/metrics" {
				t.Fatalf("recorded %+v", r)
			}
		})
	}
}

func TestDefaultHTTPRedirects(t *testing.T) {
	addr := start(t, nil)
	for letter, code := range redirectCodes {
		resp, err := noRedirect(5*time.Second).Post("http://"+addr+"/parity-redirect-"+letter+"/v1/traces", "application/x-protobuf", strings.NewReader("x"))
		if err != nil {
			t.Fatal(err)
		}
		_ = resp.Body.Close()
		want := "http://" + addr + "/redirected/v1/traces?token=" + redirectToken
		if resp.StatusCode != code || resp.Header.Get("Location") != want {
			t.Fatalf("%s: %d %q, want %d %q", letter, resp.StatusCode, resp.Header.Get("Location"), code, want)
		}
	}
}

func TestDefaultHTTPEchoErrors(t *testing.T) {
	addr := start(t, nil)
	req, _ := http.NewRequest(http.MethodPost, "http://"+addr+"/v1/traces", strings.NewReader("x"))
	req.Header.Set(echoErrorHeader, "1")
	req.Header.Set("X-Parity-Auth", "Bearer tok-2")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusBadRequest || !strings.Contains(string(b), bodyMarker) || !strings.Contains(string(b), "tok-2") {
		t.Fatalf("got %d %s", resp.StatusCode, b)
	}

	req, _ = http.NewRequest(http.MethodPost, "http://"+addr+"/v1/traces", strings.NewReader("x"))
	req.Header.Set(echoErrorHeader, "token")
	req.Header.Set("X-Parity-Auth", "Bearer tok-3")
	resp, err = (&http.Client{Transport: &http.Transport{DisableKeepAlives: true}}).Do(req)
	if err != nil {
		t.Fatal(err)
	}
	b, _ = io.ReadAll(resp.Body)
	_ = resp.Body.Close()
	if resp.Status != "400 Rejected invalid token tok-3" || string(b) != `{"note":"`+bodyMarker+`"}` {
		t.Fatalf("got %q %s", resp.Status, b)
	}
}

func TestRedirectPath(t *testing.T) {
	tests := []struct {
		path string
		code int
		rest string
		ok   bool
	}{
		{"/parity-redirect-a/v1/traces", 301, "/v1/traces", true},
		{"/parity-redirect-d/x", 308, "/x", true},
		{"/parity-redirect-e/v1/traces", 0, "", false},
		{"/parity-redirect-c", 0, "", false},
		{"/parity-redirect-cc/v1", 0, "", false},
		{"/v1/traces", 0, "", false},
	}
	for _, tt := range tests {
		code, rest, ok := redirectPath(tt.path)
		if code != tt.code || ok != tt.ok || (ok && rest != tt.rest) {
			t.Errorf("%s: (%d, %q, %v), want (%d, %q, %v)", tt.path, code, rest, ok, tt.code, tt.rest, tt.ok)
		}
	}
}

func TestServeRequests(t *testing.T) {
	resetRequests()
	get := func() string {
		w := httptest.NewRecorder()
		serveRequests(w, httptest.NewRequest(http.MethodGet, "/requests", nil))
		return w.Body.String()
	}
	if got := get(); got != "[]\n" {
		t.Fatalf("empty log: %q", got)
	}
	add(rec{Proto: "http", Path: "/v1/logs"})
	var got []rec
	if err := json.Unmarshal([]byte(get()), &got); err != nil || len(got) != 1 || got[0].Path != "/v1/logs" {
		t.Fatalf("log %+v (%v)", got, err)
	}
	serveRequests(httptest.NewRecorder(), httptest.NewRequest(http.MethodDelete, "/requests", nil))
	if got := get(); got != "[]\n" {
		t.Fatalf("after DELETE: %q", got)
	}
}

func TestCertsAndTLSListener(t *testing.T) {
	dir := t.TempDir()
	cert := certs(dir, []string{"127.0.0.1", "fakeotlp"}, true)
	for _, name := range []string{"ca.pem", "cert.pem", "key.pem"} {
		if fi, err := os.Stat(filepath.Join(dir, name)); err != nil || fi.Size() == 0 {
			t.Fatalf("%s: %v", name, err)
		}
	}
	caPEM, _ := os.ReadFile(filepath.Join(dir, "ca.pem"))
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caPEM) {
		t.Fatal("ca.pem holds no certificate")
	}
	resetRequests()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	srv := newServer("", handler(newGRPCServer(), true, nil), true)
	srv.TLSConfig = &tls.Config{Certificates: []tls.Certificate{cert}}
	go func() { _ = srv.ServeTLS(ln, "", "") }()
	t.Cleanup(func() { _ = srv.Close() })
	client := &http.Client{Timeout: 5 * time.Second, Transport: &http.Transport{
		TLSClientConfig: &tls.Config{RootCAs: pool}, ForceAttemptHTTP2: true}}
	resp, err := client.Post("https://"+ln.Addr().String()+"/v1/traces", "application/x-protobuf", strings.NewReader("x"))
	if err != nil {
		t.Fatal(err)
	}
	_ = resp.Body.Close()
	if resp.StatusCode != http.StatusOK || resp.ProtoMajor != 2 {
		t.Fatalf("got %d over HTTP/%d, want 200 over HTTP/2", resp.StatusCode, resp.ProtoMajor)
	}
	if r := recorded(); len(r) != 1 || !r[0].TLS {
		t.Fatalf("recorded %+v, want one TLS request", r)
	}
}

func TestEnvOr(t *testing.T) {
	t.Setenv("FAKEOTLP_TEST_VAR", "")
	if got := envOr("FAKEOTLP_TEST_VAR", "d"); got != "d" {
		t.Fatalf("unset: %q", got)
	}
	t.Setenv("FAKEOTLP_TEST_VAR", "v")
	if got := envOr("FAKEOTLP_TEST_VAR", "d"); got != "v" {
		t.Fatalf("set: %q", got)
	}
}
