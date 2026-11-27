// Command fakeotlp is a minimal OTLP receiver for the parity fixture's
// "export" cases (specs/config-contract-v0.3/design.md D2).
//
// Every address in PLAIN / TLS accepts BOTH OTLP/gRPC and OTLP/HTTP (dispatch
// on content-type); plaintext listeners use h2c, TLS listeners use a CA
// generated at startup and written to $OUT/ca.pem. Defaults bind only the
// parity case addresses (127.0.0.2, 127.0.0.3:4317 TLS, [::1]:24318), so
// unrelated exporters dialing localhost:4317 are refused. Each request is
// recorded (destination IP, port, protocol, TLS, path and raw path, content/grpc encoding,
// grpc-timeout, x-parity-* headers). ADMIN serves GET /requests (JSON) and
// DELETE /requests (reset). Payloads are drained, never decoded.
//
// An OTLP/HTTP request whose path starts with /parity-redirect-<letter>/ (a=301,
// b=302, c=307, d=308) is answered with that status and Location http://<host>/redirected/<rest>?token=
// parity-redirect-token (contract task A31: clients must not follow it).
// An OTLP/gRPC request carrying the header x-parity-grpc-redirect=<3xx code>
// is answered with that HTTP/2 :status and the same kind of Location, no gRPC
// body or trailers (contract task A51: the helper line for a gRPC 3xx).
//
// A request carrying the header x-parity-echo-error is answered with an error
// that echoes its x-parity-* headers, as a collector or proxy echoing the
// request would (contract task A29: SDK error text must not carry a response
// body or a configured header value):
//   - any value but "token": OTLP/HTTP 400 with the headers and the body-only
//     marker "parity-body-marker" in a JSON body; OTLP/gRPC INVALID_ARGUMENT
//     with the headers in the status message;
//   - "token": only the last whitespace-separated part of each other x-parity-*
//     value (the credential of "Bearer <token>") — in the HTTP reason phrase
//     (status line "400 Rejected invalid token <t>", body = the marker only) and
//     in the gRPC status message.
//
// Hang mode (--hang or HANG=true; soak task K-hang, specs/soak-simulation
// SR15 "Failure modes (r6.2)", design K-D14): every OTLP request — gRPC or
// HTTP, on every listener — completes the TCP and HTTP/2 (h2c) or HTTP/1.1
// handshake, is recorded and has its body read, and then gets no answer: no
// gRPC response headers, status or trailers, no HTTP status line. The call is
// held until the client gives up (deadline, timeout or closed connection).
// At most --hang-max (HANG_MAX, default 256) calls are held; a new call beyond
// the cap closes the oldest one (HTTP/2 stream reset, HTTP/1.1 connection
// closed), so a client that never gives up cannot exhaust memory. Redirect
// paths and the echo/redirect headers are not honored in hang mode. ADMIN
// also serves GET /held: {"held": <calls held now>, "evicted": <closed by the cap>}.
//
// Test-only: no auth. Never run it outside a test network.
package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"flag"
	"fmt"
	"io"
	"log"
	"math/big"
	"net"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	// Registers the gzip decompressor: the helpers export gzip by default and
	// an unregistered codec fails the call (logged as an upload error).
	_ "google.golang.org/grpc/encoding/gzip"
	"google.golang.org/protobuf/types/known/emptypb"
)

type rec struct {
	Addr     string            `json:"addr"` // local (destination) IP the client dialed
	Proto    string            `json:"proto"`
	TLS      bool              `json:"tls"`
	Host     string            `json:"host"`
	Port     string            `json:"port"`
	Path     string            `json:"path"`
	// RawPath is the path as sent, escapes kept (net/url EscapedPath: the
	// request's RawPath when it differs from Path's default encoding), so a
	// case can assert that "/a%2Fb" was not decoded to "/a/b".
	RawPath  string            `json:"raw_path"`
	Bytes    int               `json:"bytes"`
	Encoding string            `json:"encoding"`
	Timeout  string            `json:"timeout,omitempty"` // raw grpc-timeout header (gRPC only)
	Headers  map[string]string `json:"headers,omitempty"`
}

// parityHeaders returns the request headers whose name starts with
// "x-parity" (lower-cased), so fixture cases can assert header propagation
// without the receiver storing arbitrary — possibly sensitive — headers.
func parityHeaders(h http.Header) map[string]string {
	var out map[string]string
	for k, v := range h {
		if lk := strings.ToLower(k); strings.HasPrefix(lk, "x-parity") {
			if out == nil {
				out = map[string]string{}
			}
			out[lk] = strings.Join(v, ",")
		}
	}
	return out
}

// echoErrorHeader asks the receiver to answer with an error echoing the
// request's x-parity-* headers.
const echoErrorHeader = "x-parity-echo-error"

// grpcRedirectHeader asks for an HTTP/2 3xx answer to a gRPC request (task A51).
const grpcRedirectHeader = "x-parity-grpc-redirect"

// echoText renders the x-parity-* headers as JSON (sorted keys).
func echoText(h map[string]string) string {
	b, _ := json.Marshal(h)
	return string(b)
}

// echoTokens is the last whitespace-separated part of every x-parity-* value
// except the trigger, space-separated (sorted by header name).
func echoTokens(h map[string]string) string {
	keys := make([]string, 0, len(h))
	for k := range h {
		if k != echoErrorHeader {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	var out []string
	for _, k := range keys {
		if f := strings.Fields(h[k]); len(f) > 0 {
			out = append(out, f[len(f)-1])
		}
	}
	return strings.Join(out, " ")
}

// bodyMarker is written only in the HTTP error body: a case asserts it never
// reaches the logs (the body is dropped, not only masked).
const bodyMarker = "parity-body-marker"

var (
	mu   sync.Mutex
	reqs []rec
)

func add(r rec) { mu.Lock(); reqs = append(reqs, r); mu.Unlock() }

// certs generates a CA and a leaf for hosts, writes $OUT/ca.pem and, when
// writeLeaf is set (CERTS_ONLY mode, for the e2e Collector), also
// $OUT/cert.pem and $OUT/key.pem (0644: test-only material read by another uid).
func certs(out string, hosts []string, writeLeaf bool) tls.Certificate {
	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		log.Fatalf("generate CA key: %v", err)
	}
	ca := &x509.Certificate{SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "fakeotlp-test-ca"}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(24 * time.Hour), IsCA: true, KeyUsage: x509.KeyUsageCertSign, BasicConstraintsValid: true}
	caDER, err := x509.CreateCertificate(rand.Reader, ca, ca, &caKey.PublicKey, caKey)
	if err != nil {
		log.Fatalf("create CA: %v", err)
	}
	caCert, err := x509.ParseCertificate(caDER)
	if err != nil {
		log.Fatalf("parse CA: %v", err)
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		log.Fatalf("generate leaf key: %v", err)
	}
	leaf := &x509.Certificate{SerialNumber: big.NewInt(2), Subject: pkix.Name{CommonName: "fakeotlp"}, NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(24 * time.Hour), KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	for _, h := range hosts {
		if ip := net.ParseIP(h); ip != nil {
			leaf.IPAddresses = append(leaf.IPAddresses, ip)
		} else {
			leaf.DNSNames = append(leaf.DNSNames, h)
		}
	}
	der, err := x509.CreateCertificate(rand.Reader, leaf, caCert, &key.PublicKey, caKey)
	if err != nil {
		log.Fatalf("create leaf certificate: %v", err)
	}
	if err := os.WriteFile(out+"/ca.pem", pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER}), 0o644); err != nil {
		log.Fatalf("write CA: %v", err) // a silently empty CA turns every TLS case into a false failure
	}
	if writeLeaf {
		keyDER, err := x509.MarshalECPrivateKey(key)
		if err != nil {
			log.Fatalf("marshal leaf key: %v", err)
		}
		for name, blk := range map[string]*pem.Block{
			"cert.pem": {Type: "CERTIFICATE", Bytes: der},
			"key.pem":  {Type: "EC PRIVATE KEY", Bytes: keyDER},
		} {
			if err := os.WriteFile(out+"/"+name, pem.EncodeToMemory(blk), 0o644); err != nil {
				log.Fatalf("write %s: %v", name, err)
			}
		}
	}
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key}
}

// handler serves OTLP/gRPC (HTTP/2 + application/grpc) through g and OTLP/HTTP
// itself; with a non-nil hang it holds every request unanswered instead.
func handler(g *grpc.Server, isTLS bool, hang *hanger) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		addr, port, _ := net.SplitHostPort(r.Context().Value(http.LocalAddrContextKey).(net.Addr).String())
		if r.ProtoMajor == 2 && strings.HasPrefix(r.Header.Get("Content-Type"), "application/grpc") {
			add(rec{Addr: addr, Proto: "grpc", TLS: isTLS, Host: r.Host, Port: port, Path: r.URL.Path, RawPath: r.URL.EscapedPath(),
				Encoding: r.Header.Get("Grpc-Encoding"), Timeout: r.Header.Get("Grpc-Timeout"), Headers: parityHeaders(r.Header)})
			if hang != nil {
				_, _ = io.Copy(io.Discard, http.MaxBytesReader(w, r.Body, maxBody))
				hang.hold(r.Context())
			}
			if code, err := strconv.Atoi(r.Header.Get(grpcRedirectHeader)); err == nil && code >= 300 && code <= 399 {
				w.Header().Set("Location", "http://"+r.Host+"/redirected"+r.URL.Path+"?token="+redirectToken)
				w.WriteHeader(code)
				return
			}
			g.ServeHTTP(w, r)
			return
		}
		if hang != nil {
			n, _ := io.Copy(io.Discard, http.MaxBytesReader(w, r.Body, maxBody))
			add(rec{Addr: addr, Proto: "http", TLS: isTLS, Host: r.Host, Port: port, Path: r.URL.Path, RawPath: r.URL.EscapedPath(), Bytes: int(n),
				Encoding: r.Header.Get("Content-Encoding"), Headers: parityHeaders(r.Header)})
			hang.hold(r.Context())
		}
		b, _ := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBody))
		add(rec{Addr: addr, Proto: "http", TLS: isTLS, Host: r.Host, Port: port, Path: r.URL.Path, RawPath: r.URL.EscapedPath(), Bytes: len(b),
			Encoding: r.Header.Get("Content-Encoding"), Headers: parityHeaders(r.Header)})
		if code, rest, ok := redirectPath(r.URL.Path); ok {
			// The client must not follow it (contract task A31): the target path is
			// recorded like any request, so a followed redirect is visible.
			w.Header().Set("Location", "http://"+r.Host+"/redirected"+rest+"?token="+redirectToken)
			w.WriteHeader(code)
			return
		}
		if mode := r.Header.Get(echoErrorHeader); mode != "" {
			ph := parityHeaders(r.Header)
			if mode == "token" {
				// A custom reason phrase needs the raw connection (HTTP/1.1 only).
				if hj, ok := w.(http.Hijacker); ok && r.ProtoMajor == 1 {
					if conn, bw, err := hj.Hijack(); err == nil {
						body := `{"note":"` + bodyMarker + `"}`
						reason := strings.NewReplacer("\r", " ", "\n", " ").Replace("Rejected invalid token " + echoTokens(ph))
						_, _ = fmt.Fprintf(bw, "HTTP/1.1 400 %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s",
							reason, len(body), body)
						_ = bw.Flush()
						_ = conn.Close()
						return
					}
				}
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusBadRequest)
			_, _ = w.Write([]byte(`{"error":"echo","note":"` + bodyMarker + `","request_headers":` + echoText(ph) + "}"))
			return
		}
		w.Header().Set("Content-Type", "application/x-protobuf")
		w.WriteHeader(http.StatusOK) // empty body == empty Export*ServiceResponse
	})
}

// redirectToken is the credential-like marker of the redirect Location: a case
// asserts it is never logged.
const redirectToken = "parity-redirect-token"

// redirectCodes maps the letter of "/parity-redirect-<letter>/" to its status;
// letters, not digits, so the request path never holds the status a case
// asserts in the client's diagnostic (contract task A31).
var redirectCodes = map[string]int{"a": 301, "b": 302, "c": 307, "d": 308}

// redirectPath recognizes "/parity-redirect-<letter>/<rest>" (contract task
// A31): such an OTLP/HTTP request is answered with that status and a Location
// on the same receiver ("/redirected/<rest>?token=…").
func redirectPath(path string) (code int, rest string, ok bool) {
	const prefix = "/parity-redirect-"
	if !strings.HasPrefix(path, prefix) || len(path) < len(prefix)+2 || path[len(prefix)+1] != '/' {
		return 0, "", false
	}
	code, ok = redirectCodes[path[len(prefix):len(prefix)+1]]
	return code, path[len(prefix)+1:], ok
}

// maxBody caps a recorded HTTP payload; OTLP test batches are a few KB.
const maxBody = 8 << 20

// newGRPCServer answers every method: the request is drained, never decoded,
// and the reply is an empty message (a valid Export*ServiceResponse), unless
// the x-parity-echo-error header asks for an echoing error.
func newGRPCServer() *grpc.Server {
	return grpc.NewServer(grpc.UnknownServiceHandler(func(_ any, s grpc.ServerStream) error {
		var m emptypb.Empty // unknown fields retained; we only need to drain the request
		if err := s.RecvMsg(&m); err != nil {
			return err
		}
		if md, ok := metadata.FromIncomingContext(s.Context()); ok && len(md.Get(echoErrorHeader)) > 0 {
			echoed := map[string]string{}
			for k, v := range md {
				if strings.HasPrefix(k, "x-parity") {
					echoed[k] = strings.Join(v, ",")
				}
			}
			if md.Get(echoErrorHeader)[0] == "token" {
				return status.Error(codes.InvalidArgument, "invalid token "+echoTokens(echoed))
			}
			return status.Error(codes.InvalidArgument, "echo: request headers "+echoText(echoed))
		}
		return s.SendMsg(&emptypb.Empty{}) // zero bytes == valid Export*ServiceResponse
	}))
}

// newServer is one listener: HTTP/1.1 plus HTTP/2 (h2c when plaintext).
func newServer(addr string, h http.Handler, isTLS bool) *http.Server {
	p := new(http.Protocols)
	p.SetHTTP1(true)
	if isTLS {
		p.SetHTTP2(true)
	} else {
		p.SetUnencryptedHTTP2(true)
	}
	return &http.Server{Addr: addr, Handler: h, Protocols: p}
}

// defaultHangMax caps the calls held in hang mode.
const defaultHangMax = 256

// parseOptions reads the hang mode from the command line, falling back to the
// HANG / HANG_MAX environment (the other options are environment only).
func parseOptions(args []string, getenv func(string) string) (hang bool, hangMax int, err error) {
	fs := flag.NewFlagSet("fakeotlp", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	if v := getenv("HANG"); v != "" {
		if hang, err = strconv.ParseBool(v); err != nil {
			return false, 0, fmt.Errorf("HANG=%q is not a boolean", v)
		}
	}
	hangMax = defaultHangMax
	if v := getenv("HANG_MAX"); v != "" {
		if hangMax, err = strconv.Atoi(v); err != nil {
			return false, 0, fmt.Errorf("HANG_MAX=%q is not a whole number", v)
		}
	}
	fs.BoolVar(&hang, "hang", hang, "hold every OTLP request unanswered until the client gives up")
	fs.IntVar(&hangMax, "hang-max", hangMax, "most calls held at once in hang mode; the oldest is closed beyond it")
	if err := fs.Parse(args); err != nil {
		return false, 0, err
	}
	if fs.NArg() > 0 {
		return false, 0, fmt.Errorf("unexpected argument %q", fs.Arg(0))
	}
	if hangMax < 1 {
		return false, 0, fmt.Errorf("the hang cap must be at least 1 (got %d)", hangMax)
	}
	return hang, hangMax, nil
}

func main() {
	hangOn, hangMax, err := parseOptions(os.Args[1:], os.Getenv)
	if err != nil {
		log.Fatalf("fakeotlp: %v", err)
	}
	var hang *hanger
	if hangOn {
		hang = newHanger(hangMax)
		log.Printf("fakeotlp: hang mode, at most %d held calls", hangMax)
	}
	out := envOr("OUT", "/tmp")
	g := newGRPCServer()
	// fakeotlp / fakeotlp-tls: DNS names the Node.js runner maps to 127.0.0.2 /
	// 127.0.0.3 (Node 26 refuses an IP literal as TLS ServerName; design D-JS14).
	hosts := strings.Split(envOr("TLS_HOSTS", "localhost,127.0.0.1,127.0.0.2,127.0.0.3,::1,fakeotlp,fakeotlp-tls"), ",")
	if os.Getenv("CERTS_ONLY") != "" {
		certs(out, hosts, true)
		log.Printf("fakeotlp: wrote CA, cert and key to %s", out)
		return
	}
	cert := certs(out, hosts, false)
	serve := func(addr string, isTLS bool) {
		s := newServer(addr, handler(g, isTLS, hang), isTLS)
		var err error
		if isTLS {
			s.TLSConfig = &tls.Config{Certificates: []tls.Certificate{cert}}
			err = s.ListenAndServeTLS("", "")
		} else {
			err = s.ListenAndServe()
		}
		log.Fatalf("listen %s: %v", addr, err)
	}
	for _, a := range strings.Split(envOr("PLAIN", "127.0.0.2:4317,127.0.0.2:4318,127.0.0.2:80,127.0.0.2:24318,[::1]:24318"), ",") {
		go serve(a, false)
	}
	for _, a := range strings.Split(envOr("TLS", "127.0.0.2:14317,127.0.0.2:14318,127.0.0.2:443,127.0.0.3:4317"), ",") {
		go serve(a, true)
	}
	http.HandleFunc("/requests", serveRequests)
	http.HandleFunc("/held", func(w http.ResponseWriter, _ *http.Request) {
		held, evicted := hang.stats()
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]int{"held": held, "evicted": evicted})
	})
	log.Printf("fakeotlp ready uid=%d", os.Getuid())
	log.Fatal(http.ListenAndServe(envOr("ADMIN", "127.0.0.2:8888"), nil))
}

// serveRequests is the admin GET (JSON list) and DELETE (reset) of the log.
func serveRequests(w http.ResponseWriter, r *http.Request) {
	mu.Lock()
	defer mu.Unlock()
	if r.Method == http.MethodDelete {
		reqs = nil
		return
	}
	w.Header().Set("Content-Type", "application/json")
	if reqs == nil {
		_, _ = w.Write([]byte("[]\n"))
		return
	}
	_ = json.NewEncoder(w).Encode(reqs)
}

// hanger holds hang-mode calls, oldest first, at most max at a time.
type hanger struct {
	max     int
	mu      sync.Mutex
	held    []chan struct{} // closed to evict the call
	evicted int
}

func newHanger(limit int) *hanger { return &hanger{max: limit} }

// hold blocks until ctx ends (the client gave up) or the cap evicts the call,
// then aborts the handler: http.ErrAbortHandler resets the HTTP/2 stream or
// closes the HTTP/1.1 connection, so no status is ever sent (a handler that
// returned would send an implicit 200, a successful export).
func (h *hanger) hold(ctx context.Context) {
	evict := make(chan struct{})
	h.mu.Lock()
	h.held = append(h.held, evict)
	var oldest chan struct{}
	if len(h.held) > h.max {
		oldest, h.held = h.held[0], h.held[1:]
		h.evicted++
	}
	h.mu.Unlock()
	if oldest != nil {
		close(oldest)
		log.Printf("fakeotlp: hang cap %d reached; closed the oldest held call", h.max)
	}
	select {
	case <-ctx.Done():
		h.release(evict)
	case <-evict:
	}
	panic(http.ErrAbortHandler)
}

func (h *hanger) release(evict chan struct{}) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for i, c := range h.held {
		if c == evict {
			h.held = append(h.held[:i], h.held[i+1:]...)
			return
		}
	}
}

// stats returns the calls held now and those closed by the cap (0, 0 outside
// hang mode).
func (h *hanger) stats() (held, evicted int) {
	if h == nil {
		return 0, 0
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.held), h.evicted
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}
