package otelhelper

// Fixture schema, expectation matching and well-formedness checks for the
// parity runner (parity_test.go). Semantics mirror python/tests/test_parity.py;
// the format is documented in parity/README.md.

import (
	"fmt"
	"os"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// snapshotSchema lists, per kind, the expect keys a case may use. An expect
// key outside the schema is a fixture bug and fails the case hard (it would
// otherwise "fail as expected" and satisfy an xfail).
var commonExpectKeys = []string{"error", "warning", "warning_count", "log", "diagnostics", "helper_log", "console"}

// caseKeys are the only top-level keys a case may have; requiredCaseKeys must
// all be present (same set as python/tests/test_parity.py).
var (
	caseKeys         = []string{"id", "kind", "requirement", "note", "env", "code", "input", "expect", "status"}
	requiredCaseKeys = []string{"id", "kind", "env", "code", "expect", "status"}
	statusOverride   = regexp.MustCompile(`^python@sdk>=[\d.]+$`)
)

var snapshotSchema = map[string][]string{
	"resolve":        {"resource", "metric_exporters", "metric_export_interval_ms", "log_level"},
	"export":         {"received"},
	"sampler":        {"sampled", "attributes"},
	"propagation":    {"headers"},
	"http_server":    {"server_spans", "requests_counted", "handler_child_spans"},
	"grpc_server":    {"server_spans", "rpcs_counted", "call_ok"},
	"logging":        {},
	"metrics_scrape": {"scrape", "scrape_status"},
	"fork":           {"instance_ids", "instance_ids_distinct"},
}

var inputSchema = map[string][]string{
	"sampler":     {"scenario"},
	"http_server": {"requests", "traceparent", "child_span"},
	"grpc_server": {"method"},
}

var (
	receivedSignals = map[string]bool{"traces": true, "metrics": true, "logs": true}
	recordFields    = map[string]bool{"proto": true, "tls": true, "port": true, "path": true, "encoding": true, "headers": true, "timeout_ms": true, "absent": true}
	matcherKeys     = map[string]bool{"prefix": true, "regex": true, "absent": true, "not_contains": true, "all": true, "lte": true, "gte": true}
	validStatus     = regexp.MustCompile(`^(pass|xfail:[A-Za-z0-9.-]+|skip:[A-Za-z0-9._-]+)$`)
)

func contains(list []string, s string) bool {
	for _, e := range list {
		if e == s {
			return true
		}
	}
	return false
}

// validateCase returns fixture-format errors (not behavior mismatches). It
// type-checks every shape the runner later asserts on, so a malformed case
// fails on its own instead of panicking the parent test.
func validateCase(c parityCase) []string {
	var errs []string
	for _, k := range requiredCaseKeys {
		if _, ok := c.raw[k]; !ok {
			errs = append(errs, fmt.Sprintf("missing key %q", k))
		}
	}
	for k := range c.raw {
		if !contains(caseKeys, k) {
			errs = append(errs, fmt.Sprintf("unknown top-level key %q", k))
		}
	}
	allowed, ok := snapshotSchema[c.Kind]
	if !ok {
		return append(errs, fmt.Sprintf("unknown kind %q", c.Kind))
	}
	for k := range c.Expect {
		if !contains(allowed, k) && !contains(commonExpectKeys, k) {
			errs = append(errs, fmt.Sprintf("expect key %q is not in the %s snapshot schema", k, c.Kind))
		}
	}
	for k, v := range c.Input {
		if !contains(inputSchema[c.Kind], k) {
			errs = append(errs, fmt.Sprintf("input key %q is not valid for kind %s", k, c.Kind))
			continue
		}
		switch k {
		case "requests":
			list, ok := v.([]any)
			for _, e := range list {
				if _, isStr := e.(string); !isStr {
					ok = false
				}
			}
			if !ok {
				errs = append(errs, "input.requests must be a list of strings")
			}
		case "method", "scenario", "traceparent":
			if _, ok := v.(string); !ok {
				errs = append(errs, fmt.Sprintf("input.%s must be a string", k))
			}
		case "child_span":
			if _, ok := v.(bool); !ok {
				errs = append(errs, "input.child_span must be a boolean")
			}
		}
	}
	if w, ok := c.Expect["warning_count"]; ok {
		m, isMap := w.(map[string]any)
		for _, n := range m {
			if _, isNum := n.(float64); !isNum {
				isMap = false
			}
		}
		if !isMap {
			errs = append(errs, "warning_count must map substrings to numbers")
		}
	}
	if w, ok := c.Expect["warning"]; ok {
		if _, isStr := w.(string); !isStr {
			list, isList := w.([]any)
			for _, e := range list {
				if _, s := e.(string); !s {
					isList = false
				}
			}
			if !isList {
				errs = append(errs, "warning must be a string or list of strings")
			}
		}
	}
	if r, ok := c.Expect["received"]; ok {
		rec, isMap := r.(map[string]any)
		if !isMap {
			errs = append(errs, "expect.received must be an object")
		}
		for sig, w := range rec {
			if !receivedSignals[sig] {
				errs = append(errs, fmt.Sprintf("received signal %q unknown", sig))
			}
			wm, isMap := w.(map[string]any)
			if !isMap {
				errs = append(errs, fmt.Sprintf("received.%s must be an object", sig))
				continue
			}
			for f := range wm {
				if !recordFields[f] {
					errs = append(errs, fmt.Sprintf("received.%s field %q unknown", sig, f))
				}
			}
		}
	}
	errs = append(errs, checkMatcherDicts("expect", anyMapOf(c.Expect))...)
	for _, lang := range []string{"go", "python", "dotnet", "nodejs"} {
		if !validStatus.MatchString(c.Status[lang]) {
			errs = append(errs, fmt.Sprintf("status.%s %q invalid", lang, c.Status[lang]))
		}
	}
	for k, v := range c.Status {
		if contains([]string{"go", "python", "dotnet", "nodejs"}, k) {
			continue
		}
		if !statusOverride.MatchString(k) || !validStatus.MatchString(v) {
			errs = append(errs, fmt.Sprintf("status override %q: %q invalid", k, v))
		}
	}
	walkRegexes(c.Expect, func(re string) {
		if _, err := regexp.Compile(re); err != nil {
			errs = append(errs, fmt.Sprintf("regex %q does not compile: %v", re, err))
		}
	})
	return errs
}

func anyMapOf(m map[string]any) any { return m }

// checkMatcherDicts rejects objects mixing matcher ops with other keys (a typo
// like "contians" next to "regex" would otherwise be ignored).
func checkMatcherDicts(path string, v any) []string {
	var errs []string
	switch x := v.(type) {
	case map[string]any:
		if isMatcher(x) {
			for k := range x {
				if !matcherKeys[k] {
					errs = append(errs, fmt.Sprintf("%s: %q is not a matcher op (allowed: prefix, regex, absent, not_contains, all, lte, gte)", path, k))
				}
			}
			if subs, ok := x["all"]; ok {
				list, isList := subs.([]any)
				if !isList {
					errs = append(errs, path+".all must be a list")
				}
				for i, e := range list {
					errs = append(errs, checkMatcherDicts(fmt.Sprintf("%s.all[%d]", path, i), e)...)
				}
			}
			for _, op := range []string{"lte", "gte"} {
				if a, ok := x[op]; ok {
					if _, isNum := a.(float64); !isNum {
						errs = append(errs, fmt.Sprintf("%s.%s must be a number", path, op))
					}
				}
			}
			return errs
		}
		for k, e := range x {
			errs = append(errs, checkMatcherDicts(path+"."+k, e)...)
		}
	case []any:
		for i, e := range x {
			errs = append(errs, checkMatcherDicts(fmt.Sprintf("%s[%d]", path, i), e)...)
		}
	}
	return errs
}

func walkRegexes(v any, fn func(string)) {
	switch x := v.(type) {
	case map[string]any:
		for k, e := range x {
			if s, ok := e.(string); ok && k == "regex" {
				fn(s)
			} else {
				walkRegexes(e, fn)
			}
		}
	case []any:
		for _, e := range x {
			walkRegexes(e, fn)
		}
	}
}

// TestParityNoTaskSkipsAtRelease fails a release build (PARITY_RELEASE=1)
// while any case is still skipped pending a task ("skip:A…").
func TestParityNoTaskSkipsAtRelease(t *testing.T) {
	if os.Getenv("PARITY_RELEASE") == "" {
		t.Skip("set PARITY_RELEASE=1 for the release gate")
	}
	for _, c := range loadParityCases(t) {
		for lang, st := range c.Status {
			if strings.HasPrefix(st, "skip:A") {
				t.Errorf("%s: status.%s %q still pending a task", c.ID, lang, st)
			}
		}
	}
}

func TestParityFixtureWellFormed(t *testing.T) {
	seen := map[string]bool{}
	for _, c := range loadParityCases(t) {
		if seen[c.ID] {
			t.Errorf("duplicate case id %q", c.ID)
		}
		seen[c.ID] = true
		for _, e := range validateCase(c) {
			t.Errorf("%s: %s", c.ID, e)
		}
	}
}

func TestParityMatcher(t *testing.T) {
	snap := map[string]any{"a": "unknown_service:x", "n": float64(3), "log": "x OTEL_FOO y", "diagnostics": "OTEL_FOO\nOTEL_FOO"}
	ok := []map[string]any{
		{"a": map[string]any{"prefix": "unknown_service"}, "n": float64(3), "warning": "OTEL_FOO"},
		{"b": map[string]any{"absent": true}},
		{"a": map[string]any{"all": []any{map[string]any{"regex": "x$"}, map[string]any{"not_contains": "go"}}}},
		{"warning_count": map[string]any{"OTEL_FOO": float64(2)}},
	}
	for i, e := range ok {
		if m := matchExpect(e, snap); len(m) > 0 {
			t.Errorf("ok[%d]: %v", i, m)
		}
	}
	bad := []map[string]any{
		{"a": map[string]any{"not_contains": "unknown"}},
		{"n": float64(4)},
		{"a": map[string]any{"regex": "("}}, // compile error → mismatch, not panic
		{"warning_count": map[string]any{"OTEL_FOO": float64(1)}},
	}
	for i, e := range bad {
		if m := matchExpect(e, snap); len(m) == 0 {
			t.Errorf("bad[%d] matched", i)
		}
	}
	if m := matchExpect(map[string]any{}, map[string]any{"error": "boom"}); len(m) != 1 {
		t.Errorf("unexpected error not reported: %v", m)
	}
	okStatus := map[string]string{"go": "pass", "python": "pass", "dotnet": "xfail:phase-c", "nodejs": "skip:phase-b"}
	raw := map[string]any{"id": 1, "kind": 1, "env": 1, "code": 1, "expect": 1, "status": 1}
	mk := func(kind string, expect map[string]any) parityCase {
		return parityCase{ID: "x", Kind: kind, Expect: expect, Status: okStatus, raw: raw}
	}
	if errs := validateCase(mk("bogus", map[string]any{})); len(errs) == 0 {
		t.Error("unknown kind accepted")
	}
	if errs := validateCase(mk("resolve", map[string]any{"resorce": float64(1)})); len(errs) != 1 {
		t.Errorf("misspelled expect key: %v", errs)
	}
	if errs := validateCase(mk("resolve", map[string]any{"log": map[string]any{"regex": "x", "contians": "y"}})); len(errs) != 1 {
		t.Errorf("misspelled matcher op: %v", errs)
	}
	if errs := validateCase(mk("export", map[string]any{"received": "nope"})); len(errs) == 0 {
		t.Error("non-object received accepted")
	}
	if errs := validateCase(mk("resolve", map[string]any{"warning_count": map[string]any{"x": "1"}})); len(errs) == 0 {
		t.Error("non-numeric warning_count accepted")
	}
	badOverride := mk("resolve", map[string]any{})
	badOverride.Status = map[string]string{"go": "pass", "python": "pass", "dotnet": "xfail:phase-c", "nodejs": "skip:phase-b", "python@sdk>=x": "pass"}
	if errs := validateCase(badOverride); len(errs) != 1 {
		t.Errorf("bad override key: %v", errs)
	}
	noKeys := mk("resolve", map[string]any{})
	noKeys.raw = map[string]any{"id": 1, "kind": 1, "expect": 1, "status": 1, "stauts": 1}
	if errs := validateCase(noKeys); len(errs) != 3 {
		t.Errorf("missing env/code + unknown top-level key: %v", errs)
	}
	if m := matchExpect(map[string]any{"t": map[string]any{"lte": float64(500), "gte": float64(1)}}, map[string]any{"t": float64(499)}); len(m) > 0 {
		t.Errorf("lte/gte: %v", m)
	}
	if m := matchExpect(map[string]any{"t": map[string]any{"lte": float64(500)}}, map[string]any{"t": float64(9999)}); len(m) == 0 {
		t.Error("lte did not reject")
	}
}

// ── matching ──────────────────────────────────────────────────────────────

func isMatcher(m map[string]any) bool {
	for k := range m {
		if matcherKeys[k] {
			return true
		}
	}
	return false
}

func matchExpect(expect map[string]any, snap map[string]any) []string {
	var out []string
	if _, ok := expect["error"]; !ok {
		if e, has := snap["error"]; has {
			out = append(out, fmt.Sprintf("unexpected error: %v", e))
		}
	}
	for _, k := range sortedKeys(expect) {
		want := expect[k]
		switch k {
		case "error":
			got, has := snap["error"]
			if s, ok := want.(string); ok {
				if !has || !strings.Contains(fmt.Sprint(got), s) {
					out = append(out, fmt.Sprintf("error: want substring %q, got %v", s, got))
				}
				continue
			}
			out = append(out, matchValue("error", want, got, has)...)
		case "warning":
			log := fmt.Sprint(snap["log"])
			for _, s := range asStrings(want) {
				if !strings.Contains(log, s) {
					out = append(out, fmt.Sprintf("warning: no diagnostic contains %q (log: %q)", s, truncate(log)))
				}
			}
		case "warning_count":
			diag := fmt.Sprint(snap["diagnostics"])
			for _, sub := range sortedKeys(want.(map[string]any)) {
				n := 0
				for _, line := range strings.Split(diag, "\n") {
					if strings.Contains(line, sub) {
						n++
					}
				}
				if float64(n) != want.(map[string]any)[sub] {
					out = append(out, fmt.Sprintf("warning_count[%q]: want %v, got %d", sub, want.(map[string]any)[sub], n))
				}
			}
		case "received":
			out = append(out, matchReceived(want.(map[string]any), snap["received"])...)
		default:
			got, has := snap[k]
			out = append(out, matchValue(k, want, got, has)...)
		}
	}
	return out
}

func matchReceived(want map[string]any, got any) []string {
	recv, _ := got.(map[string]any)
	var out []string
	for _, sig := range sortedKeys(want) {
		w := want[sig].(map[string]any)
		recs, _ := recv[sig].([]any)
		if a, ok := w["absent"]; ok && a == true {
			if len(recs) > 0 {
				out = append(out, fmt.Sprintf("received.%s: want none, got %v", sig, recs))
			}
			continue
		}
		if len(recs) == 0 {
			out = append(out, fmt.Sprintf("received.%s: nothing received (all: %v)", sig, recv))
			continue
		}
		for i, r := range recs {
			rm := r.(map[string]any)
			for _, f := range sortedKeys(w) {
				fv, has := rm[f]
				out = append(out, matchValue(fmt.Sprintf("received.%s[%d].%s", sig, i, f), w[f], fv, has)...)
			}
		}
	}
	return out
}

func matchValue(path string, want, got any, has bool) []string {
	m, ok := want.(map[string]any)
	if !ok {
		if !has {
			return []string{fmt.Sprintf("%s: want %v, got <absent>", path, want)}
		}
		if !reflect.DeepEqual(normalize(want), normalize(got)) {
			return []string{fmt.Sprintf("%s: want %v, got %v", path, want, got)}
		}
		return nil
	}
	var out []string
	if !isMatcher(m) {
		gm, _ := got.(map[string]any)
		for _, k := range sortedKeys(m) {
			v, h := gm[k]
			out = append(out, matchValue(path+"."+k, m[k], v, h)...)
		}
		return out
	}
	s := fmt.Sprint(got)
	for _, op := range sortedKeys(m) {
		arg := m[op]
		switch op {
		case "absent":
			if arg == true && has {
				out = append(out, fmt.Sprintf("%s: want absent, got %v", path, got))
			}
			if arg == false && !has {
				out = append(out, fmt.Sprintf("%s: want present", path))
			}
		case "prefix":
			if !has || !strings.HasPrefix(s, fmt.Sprint(arg)) {
				out = append(out, fmt.Sprintf("%s: want prefix %q, got %v", path, arg, got))
			}
		case "regex":
			re, err := regexp.Compile(fmt.Sprint(arg))
			if err != nil {
				out = append(out, fmt.Sprintf("%s: bad regex %q: %v", path, arg, err))
			} else if !has || !re.MatchString(s) {
				out = append(out, fmt.Sprintf("%s: want /%s/, got %q", path, arg, truncate(s)))
			}
		case "not_contains":
			if has && strings.Contains(s, fmt.Sprint(arg)) {
				out = append(out, fmt.Sprintf("%s: must not contain %q", path, arg))
			}
		case "lte", "gte":
			n, isNum := got.(float64)
			lim, _ := arg.(float64)
			if !has || !isNum || (op == "lte" && n > lim) || (op == "gte" && n < lim) {
				out = append(out, fmt.Sprintf("%s: want %s %v, got %v", path, op, arg, got))
			}
		case "all":
			subs, _ := arg.([]any)
			for _, sub := range subs {
				out = append(out, matchValue(path, sub, got, has)...)
			}
		}
	}
	return out
}

func normalize(v any) any {
	switch x := v.(type) {
	case int:
		return float64(x)
	case int64:
		return float64(x)
	case []any:
		out := make([]any, len(x))
		for i := range x {
			out[i] = normalize(x[i])
		}
		return out
	}
	return v
}

func asStrings(v any) []string {
	if s, ok := v.(string); ok {
		return []string{s}
	}
	var out []string
	for _, e := range v.([]any) {
		out = append(out, fmt.Sprint(e))
	}
	return out
}

func sortedKeys(m map[string]any) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func truncate(s string) string {
	if len(s) > 300 {
		return s[:300] + "…"
	}
	return s
}
