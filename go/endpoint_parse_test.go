package otelhelper

import (
	"errors"
	"strings"
	"testing"
)

// Task A17: the D1 edge-case rulings dated 2026-10-29 (RFC 3986 split, ports,
// host characters, IDN, constant rendering, '@' in the path).

func TestRFCSplit(t *testing.T) {
	tests := []struct {
		in   string
		ok   bool
		want rfcEndpoint
	}{
		{"http://h:4317", true, rfcEndpoint{scheme: "http", host: "h", port: "4317"}},
		{"HTTPS://u:p@w@h:04318/a%2Fb?q=1#f", true, rfcEndpoint{scheme: "https", userinfo: true, host: "h", port: "4318", path: "/a%2Fb", hasQuery: true, query: "q=1", hasFragment: true}},
		{"http://h:4317/?", true, rfcEndpoint{scheme: "http", host: "h", port: "4317", path: "/", hasQuery: true}},
		{"http://h#", true, rfcEndpoint{scheme: "http", host: "h", hasFragment: true}},
		{"http://h:", true, rfcEndpoint{scheme: "http", host: "h"}},
		{"http://[::1]:24318/x", true, rfcEndpoint{scheme: "http", host: "[::1]", port: "24318", path: "/x"}},
		{"http://[::1]", true, rfcEndpoint{scheme: "http", host: "[::1]"}},
		{"http://", true, rfcEndpoint{scheme: "http"}},
		{"http://[::1", false, rfcEndpoint{}},
		{"http://[::1]x", false, rfcEndpoint{}},
		{"http://h:0", false, rfcEndpoint{}},
		{"http://h:000", false, rfcEndpoint{}},
		{"http://h:65536", false, rfcEndpoint{}},
		{"http://h:123456", false, rfcEndpoint{}},
		{"http://h:1:2", false, rfcEndpoint{}},
		{"http://h:abc", false, rfcEndpoint{}},
		{"h:4317", false, rfcEndpoint{}},
		{"1http://h", false, rfcEndpoint{}},
	}
	for _, tt := range tests {
		got, ok := rfcSplit(tt.in)
		if ok != tt.ok || (ok && got != tt.want) {
			t.Errorf("rfcSplit(%q) = %+v, %v; want %+v, %v", tt.in, got, ok, tt.want, tt.ok)
		}
	}
}

func TestCheckHost(t *testing.T) {
	tests := []struct {
		in, want   string
		zoneID, ok bool
	}{
		{"collector", "collector", false, true},
		{"Collector.Example", "collector.example", false, true},
		{"[2001:DB8::1]", "[2001:db8::1]", false, true},
		{"1.2.3.4", "1.2.3.4", false, true},
		{"10.0.0.255", "10.0.0.255", false, true},
		{"host.1a", "host.1a", false, true}, // last label not a number
		{"127.1", "", false, false},
		{"0x7f.0.0.1", "", false, false},
		{"h.0x", "", false, false},
		{"2130706433", "", false, false},
		{"1.2.3.256", "", false, false},
		{"1.2.3.4..", "1.2.3.4..", false, true}, // not "ends in a number" (WHATWG): a reg-name
		{"01.2.3.4", "", false, false},
		{"1.2.3.4.", "1.2.3.4.", false, true},
		{"10.0.0.1.", "10.0.0.1.", false, true},
		{"1.2.3.4..", "1.2.3.4..", false, true},                                            // not "ends in a number" (WHATWG): a reg-name
		{"\uff11\uff12\uff17.\uff11", "", false, false},                                    // fullwidth 127.1
		{"\uff12\uff11\uff13\uff10\uff17\uff10\uff16\uff14\uff13\uff13", "", false, false}, // fullwidth 2130706433
		{"bücher.de.", "xn--bcher-kva.de.", false, true},
		{"bü..de", "", false, false},
		{strings.Repeat("a", 62) + "." + strings.Repeat("b", 62) + "." + strings.Repeat("c", 62) + "." + strings.Repeat("d", 62) + ".bü", "", false, false},
		{"a{b", "", false, false},
		{`"h"`, "", false, false},
		{"h{", "", false, false},
		{"straße.de", "xn--strae-oqa.de", false, true},
		{"a\u200db.example", "", false, false},
		{"[2001:db8::1]", "[2001:db8::1]", false, true},
		{"bücher.de", "xn--bcher-kva.de", false, true},
		{"my_host.bücher.de", "my_host.xn--bcher-kva.de", false, true}, // no STD3 rules (WHATWG)
		{"�.example", "", false, false},
		{"", "", false, false},
		{"coll ector", "", false, false},
		{"%63ollector", "", false, false},
		{"[fe80::1%eth0]", "", true, false},
		{"[fe80::1%25eth0]", "", true, false},
		{"[1.2.3.4]", "", false, false},
		{"[nothost]", "", false, false},
		{"a<b", "", false, false},
		{"a|b", "", false, false},
	}
	for _, tt := range tests {
		got, zone, ok := checkHost(tt.in)
		if got != tt.want || zone != tt.zoneID || ok != tt.ok {
			t.Errorf("checkHost(%q) = %q, %v, %v; want %q, %v, %v", tt.in, got, zone, ok, tt.want, tt.zoneID, tt.ok)
		}
	}
}

func TestRedactURLRulings(t *testing.T) {
	tests := map[string]string{
		"http://:4317":                        unparseableURL, // host-less: never rendered partly
		"collector:abc":                       unparseableURL,
		"http://h:70000":                      unparseableURL,
		"http://h:0":                          unparseableURL,
		`http://user:pw\x@h:4317`:             unparseableURL,
		"//h:4317":                            unparseableURL,
		"":                                    unparseableURL,
		"http://coll ector:4317":              unparseableURL,
		"http://h:04318/x":                    "http://h:4318/x",
		"http://bücher.de":                    "http://xn--bcher-kva.de",
		"http://user/parity-secret@collector": "http://",
		"http://admin:1234/rest@collector?x":  "http://",
		"user:123/s@c":                        schemelessAtRendering,
		"ftp://user/s@x":                      "ftp://",
		"http://h:4318/%zz":                   "http://h:4318/%zz", // no protocol: no escape check
		"http://h:4317/p?token=a@b":           "http://",
		"unix:/tmp/otlp.sock":                 "unix:",
		"Collector:/v1/traces":                "collector:",
		"http://h:4317/a%40b":                 "http://",
		"unix:/a@b/c?q":                       "unix:",
		"x:@":                                 unparseableURL,
		"ftp://u:p@h:21/x":                    "ftp://h:21/x",
		"[::1]:4317/otlp":                     "[::1]:4317/otlp",
	}
	for in, want := range tests {
		if got := redactURL(in); got != want {
			t.Errorf("redactURL(%q) = %q, want %q", in, got, want)
		}
	}
	if got := redactEndpoint("http://h:4318/%zz", ProtocolHTTP); got != unparseableURL {
		t.Errorf("redactEndpoint(http/protobuf, %%zz) = %q", got)
	}
	if got := redactEndpoint("http://h:4317/%zz", ProtocolGRPC); got != "http://h:4317/%zz" {
		t.Errorf("redactEndpoint(grpc, %%zz) = %q", got)
	}
}

func TestEndpointPortRulings(t *testing.T) {
	for in, want := range map[string]string{
		"http://h:" + strings.Repeat("0", 5000) + "4318": "4318", "http://h:" + strings.Repeat("9", 5000): "",
		"http://h:04318": "4318", "h:04318": "4318", "http://h:0": "", "h:4318\x01": "", `h:4318\x`: "",
	} {
		if got := endpointPort(in); got != want {
			t.Errorf("endpointPort(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestResolveEndpointA17Rulings(t *testing.T) {
	env := func(raw, protocol string) endpointInput {
		return endpointInput{raw: raw, variable: EnvCollectorEndpoint, signal: "traces", protocol: protocol}
	}
	invalid := []struct{ raw, protocol, msg string }{
		{"http://h:70000", ProtocolGRPC, "invalid endpoint"},
		{"http://h:0", ProtocolGRPC, "invalid endpoint"},
		{"http://:4317", ProtocolGRPC, "invalid endpoint"},
		{"http:///collector:4317", ProtocolGRPC, "invalid endpoint"},
		{"//collector:4317", ProtocolGRPC, "invalid endpoint"},
		{`https://h:4318/x\y`, ProtocolHTTP, "backslash"},
		{`http://user:secret\pw@collector:4317`, ProtocolGRPC, "backslash"},
		{"unix:/x\x7f", ProtocolGRPC, "control character"},
		{"http://%63ollector:4317", ProtocolGRPC, "invalid endpoint"},
		{"http://[fe80::1%eth0]:4317", ProtocolGRPC, "zone ids"},
		{"http://�.example:4317", ProtocolGRPC, "invalid endpoint"},
		{"http://h:4318/%zz", ProtocolHTTP, "percent-escape"},
		{"http://127.1:4317", ProtocolGRPC, "invalid endpoint"},
		{"http://1.2.3.256:4317", ProtocolGRPC, "invalid endpoint"},
		{"http://a{b:4317", ProtocolGRPC, "invalid endpoint"},
		{"http://h:" + strings.Repeat("9", 5000), ProtocolGRPC, "invalid endpoint"},
	}
	for _, tt := range invalid {
		res, err := resolveEndpoint(env(tt.raw, tt.protocol))
		if !errors.Is(err, errEndpointInvalid) || len(res.warnings) != 1 {
			t.Errorf("%q: err=%v warnings=%+v, want one invalid warning", tt.raw, err, res.warnings)
			continue
		}
		if w := res.warnings[0]; !strings.Contains(w.msg, tt.msg) || w.value != unparseableURL {
			t.Errorf("%q: warning %+v, want %q and the constant", tt.raw, w, tt.msg)
		}
	}
	in := env("http://h:70000", ProtocolGRPC)
	in.fromCode, in.variable = true, "WithEndpoint"
	if _, err := resolveEndpoint(in); err == nil || !strings.Contains(err.Error(), "WithEndpoint") || !strings.Contains(err.Error(), unparseableURL) {
		t.Errorf("code port out of range: err = %v", err)
	}

	ok := []struct{ name, raw, protocol, wantURL, wantWarn string }{
		{"leading-zero port numeric", "http://h:04318", ProtocolHTTP, "http://h:4318/v1/traces", ""},
		{"IDN punycoded", "http://bücher.de", ProtocolGRPC, "http://xn--bcher-kva.de:4317", "explicit port"},
		{"invalid path escape irrelevant for gRPC", "http://h:4317/%zz", ProtocolGRPC, "http://h:4317", ""},
		{"empty fragment removed", "http://h:4317#", ProtocolGRPC, "http://h:4317", "userinfo/query"},
		{"empty query removed", "http://h:4317/?", ProtocolGRPC, "http://h:4317", "userinfo/query"},
		{"@ in path warned", "http://user:4318/secret@collector", ProtocolHTTP, "http://user:4318/secret@collector/v1/traces", "misplaced credentials"},
		{"schemeless IDN", "bücher.de:4317", ProtocolGRPC, "https://xn--bcher-kva.de:4317", ""},
		{"host lowercased", "http://COLLECTOR:4317", ProtocolGRPC, "http://collector:4317", ""},
		{"%2F sent decoded by the exporter: warned", "http://h:4318/a%2Fb", ProtocolHTTP, "http://h:4318/a%2Fb/v1/traces", "sends decoded"},
		{"%20 unchanged on the wire: no warning", "http://h:4318/a%20b", ProtocolHTTP, "http://h:4318/a%20b/v1/traces", ""},
		{"%40 in path warned", "http://h:4318/secret%40c", ProtocolHTTP, "http://h:4318/secret%40c/v1/traces", "misplaced credentials"},
	}
	for _, tt := range ok {
		res, err := resolveEndpoint(env(tt.raw, tt.protocol))
		if err != nil || res.explicitURL != tt.wantURL {
			t.Errorf("%s: url=%q err=%v, want %q", tt.name, res.explicitURL, err, tt.wantURL)
			continue
		}
		var joined []string
		for _, w := range res.warnings {
			joined = append(joined, w.msg+" "+w.value+" "+w.using)
		}
		got := strings.Join(joined, "|")
		if (tt.wantWarn == "") != (got == "") || !strings.Contains(got, tt.wantWarn) {
			t.Errorf("%s: warnings %q, want %q", tt.name, got, tt.wantWarn)
		}
		if strings.Contains(got, "secret") {
			t.Errorf("%s: warning echoes the path: %q", tt.name, got)
		}
	}
	// Per-signal: the path is kept verbatim (no /v1/<signal>), still warned.
	per := env("http://h:4318/a%2Fb", ProtocolHTTP)
	per.perSignal = true
	if res, err := resolveEndpoint(per); err != nil || res.explicitURL != "http://h:4318/a%2Fb" || len(res.warnings) != 1 || !strings.Contains(res.warnings[0].msg, "sends decoded") {
		t.Errorf("per-signal %%2F: url=%q err=%v warnings=%+v", res.explicitURL, err, res.warnings)
	}
	// '@' in the path or query: every rendering is "<scheme>://" only (R8, A17 final round).
	res, err := resolveEndpoint(env("http://u:pw@h:4318/p?x=secret@c", ProtocolHTTP))
	if err != nil || len(res.warnings) != 2 || res.warnings[0].using != "http://" || res.warnings[0].value != "http://" || res.warnings[1].value != "" {
		t.Errorf("@ in query: err=%v warnings=%+v", err, res.warnings)
	}
	res, err = resolveEndpoint(env("http://admin:1234/rest@collector?x", ProtocolGRPC))
	for _, w := range res.warnings {
		if strings.Contains(w.value+w.using, "admin") || strings.Contains(w.value+w.using, "1234") {
			t.Errorf("numeric password prefix echoed: %+v (err=%v)", w, err)
		}
	}
	res, _ = resolveEndpoint(env("user:123/s@c", ProtocolHTTP))
	for _, w := range res.warnings {
		if w.value != "" && w.value != schemelessAtRendering || strings.Contains(w.using, "123") {
			t.Errorf("schemeless @ rendering: %+v", w)
		}
	}
	res, _ = resolveEndpoint(env("ftp://user/s@x", ProtocolGRPC))
	if len(res.warnings) != 1 || res.warnings[0].value != "ftp://" {
		t.Errorf("unsupported scheme @ rendering: %+v", res.warnings)
	}
}

func TestWarnOnceNameOnly(t *testing.T) {
	buf := captureDiag(t)
	o := &Options{}
	o.warnOnce(endpointWarning{msg: "endpoint path contains '@' — misplaced credentials?", variable: "OTEL_EXPORTER_OTLP_ENDPOINT"})
	out := buf.String()
	if !strings.Contains(out, "variable=OTEL_EXPORTER_OTLP_ENDPOINT") || strings.Contains(out, "value=") || strings.Contains(out, "using=") {
		t.Errorf("name-only warning = %q", out)
	}
}
