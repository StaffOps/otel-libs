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
// Test-only: no auth. Never run it outside a test network.
package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"io"
	"log"
	"math/big"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"google.golang.org/grpc"
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

func handler(g *grpc.Server, isTLS bool) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		addr, port, _ := net.SplitHostPort(r.Context().Value(http.LocalAddrContextKey).(net.Addr).String())
		if r.ProtoMajor == 2 && strings.HasPrefix(r.Header.Get("Content-Type"), "application/grpc") {
			add(rec{Addr: addr, Proto: "grpc", TLS: isTLS, Host: r.Host, Port: port, Path: r.URL.Path, RawPath: r.URL.EscapedPath(),
				Encoding: r.Header.Get("Grpc-Encoding"), Timeout: r.Header.Get("Grpc-Timeout"), Headers: parityHeaders(r.Header)})
			g.ServeHTTP(w, r)
			return
		}
		b, _ := io.ReadAll(http.MaxBytesReader(w, r.Body, maxBody))
		add(rec{Addr: addr, Proto: "http", TLS: isTLS, Host: r.Host, Port: port, Path: r.URL.Path, RawPath: r.URL.EscapedPath(), Bytes: len(b),
			Encoding: r.Header.Get("Content-Encoding"), Headers: parityHeaders(r.Header)})
		w.Header().Set("Content-Type", "application/x-protobuf")
		w.WriteHeader(http.StatusOK) // empty body == empty Export*ServiceResponse
	})
}

// maxBody caps a recorded HTTP payload; OTLP test batches are a few KB.
const maxBody = 8 << 20

func main() {
	out := envOr("OUT", "/tmp")
	g := grpc.NewServer(grpc.UnknownServiceHandler(func(_ any, s grpc.ServerStream) error {
		var m emptypb.Empty // unknown fields retained; we only need to drain the request
		if err := s.RecvMsg(&m); err != nil {
			return err
		}
		return s.SendMsg(&emptypb.Empty{}) // zero bytes == valid Export*ServiceResponse
	}))
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
		p := new(http.Protocols)
		p.SetHTTP1(true)
		if isTLS {
			p.SetHTTP2(true)
		} else {
			p.SetUnencryptedHTTP2(true)
		}
		s := &http.Server{Addr: addr, Handler: handler(g, isTLS), Protocols: p}
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
	http.HandleFunc("/requests", func(w http.ResponseWriter, r *http.Request) {
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
	})
	log.Printf("fakeotlp ready uid=%d", os.Getuid())
	log.Fatal(http.ListenAndServe(envOr("ADMIN", "127.0.0.2:8888"), nil))
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}
