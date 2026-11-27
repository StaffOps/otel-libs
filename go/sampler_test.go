package otelhelper

// Sampler contract (R6, design D3, ADR-002), mirroring
// python/tests/test_sampler_precedence.py. Precedence: code ratio (any value) >
// OTEL_TRACES_SAMPLER/_ARG > OTEL_HELPER_SAMPLE_RATIO (deprecated alias,
// parentbased_traceidratio) > parentbased_always_on. Asserted on sampling
// decisions, not only on sampler types.

import (
	"context"
	"strings"
	"testing"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

var (
	samplerTraceID = trace.TraceID{0x0a, 0xf7, 0x65, 0x19, 0x16, 0xcd, 0x43, 0xdd, 0x84, 0x48, 0xeb, 0x21, 0x1c, 0x80, 0x31, 0x9c}
	samplerSpanID  = trace.SpanID{0xb7, 0xad, 0x6b, 0x71, 0x69, 0x20, 0x33, 0x31}
)

// samplerProvider resolves options from code + env and builds the sampler the
// way configureTracing does.
func samplerProvider(t *testing.T, opts ...Option) (*sdktrace.TracerProvider, *Options) {
	t.Helper()
	o := newOptions(append([]Option{WithServiceName("svc")}, opts...)...)
	tp := sdktrace.NewTracerProvider(sdktrace.WithSampler(buildSampler(o)))
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	return tp, o
}

func remoteCtx(sampled bool) context.Context {
	var flags trace.TraceFlags
	if sampled {
		flags = trace.FlagsSampled
	}
	sc := trace.NewSpanContext(trace.SpanContextConfig{
		TraceID: samplerTraceID, SpanID: samplerSpanID, TraceFlags: flags, Remote: true,
	})
	return trace.ContextWithRemoteSpanContext(context.Background(), sc)
}

func isSampled(tp *sdktrace.TracerProvider, parent context.Context) bool {
	if parent == nil {
		parent = context.Background()
	}
	_, span := tp.Tracer("t").Start(parent, "s")
	defer span.End()
	return span.SpanContext().IsSampled()
}

// samplerWarnings returns the sampler diagnostics only (config warns about
// unrelated unset variables).
func samplerWarnings(buf *syncBuffer) []string {
	var out []string
	for _, line := range strings.Split(buf.String(), "\n") {
		// The OTEL_HELPER_SAMPLE_RATIO deprecation names OTEL_TRACES_SAMPLER as
		// its replacement; it is asserted in deprecation_test.go, not here.
		if strings.Contains(line, "level=WARN") && strings.Contains(line, "OTEL_TRACES_SAMPLER") && !strings.Contains(line, "is deprecated") {
			out = append(out, line)
		}
	}
	return out
}

func clearSamplerEnv(t *testing.T) {
	t.Helper()
	for _, k := range []string{EnvTracesSampler, EnvTracesSamplerArg, EnvSampleRatio} {
		t.Setenv(k, "")
	}
}

// --- precedence ---

func TestSampler_HelperVarOnly_AppliesParentBasedRatio(t *testing.T) {
	clearSamplerEnv(t)
	t.Setenv(EnvSampleRatio, "0.25")
	tp, o := samplerProvider(t)
	if o.SampleRatio != 0.25 {
		t.Fatalf("SampleRatio = %v, want 0.25", o.SampleRatio)
	}
	if d := buildSampler(o).Description(); !strings.Contains(d, "root:TraceIDRatioBased{0.25}") {
		t.Errorf("sampler = %s, want ParentBased(TraceIDRatioBased{0.25})", d)
	}
	if !isSampled(tp, remoteCtx(true)) {
		t.Error("alias sampler did not follow a sampled remote parent")
	}
}

func TestSampler_StandardVarOnly_Wins(t *testing.T) {
	clearSamplerEnv(t)
	t.Setenv(EnvTracesSampler, "always_off")
	tp, _ := samplerProvider(t)
	if isSampled(tp, nil) || isSampled(tp, remoteCtx(true)) {
		t.Error("always_off sampled a span")
	}
}

func TestSampler_StandardVarBeatsHelperVar(t *testing.T) {
	clearSamplerEnv(t)
	t.Setenv(EnvTracesSampler, "parentbased_always_on")
	t.Setenv(EnvSampleRatio, "0.25")
	_, o := samplerProvider(t)
	if o.SampleRatio != 1.0 {
		t.Errorf("SampleRatio = %v, want 1.0 (alias ignored)", o.SampleRatio)
	}
	if d := buildSampler(o).Description(); !strings.Contains(d, "root:AlwaysOnSampler") {
		t.Errorf("sampler = %s, want ParentBased(AlwaysOn)", d)
	}
}

func TestSampler_WhitespaceStandardVar_CountsAsUnset(t *testing.T) {
	clearSamplerEnv(t)
	t.Setenv(EnvTracesSampler, "  ")
	t.Setenv(EnvSampleRatio, "0.25")
	_, o := samplerProvider(t)
	if o.SampleRatio != 0.25 {
		t.Errorf("SampleRatio = %v, want 0.25 (blank OTEL_TRACES_SAMPLER is unset)", o.SampleRatio)
	}
}

func TestSampler_CodeRatioBeatsStandardVar(t *testing.T) {
	clearSamplerEnv(t)
	t.Setenv(EnvTracesSampler, "always_off")
	_, o := samplerProvider(t, WithSampleRatio(0.5))
	if d := buildSampler(o).Description(); !strings.Contains(d, "root:TraceIDRatioBased{0.5}") {
		t.Errorf("sampler = %s, want ParentBased(TraceIDRatioBased{0.5})", d)
	}
}

// --- default: parentbased_always_on ---

func TestSampler_Default_IsParentBasedAlwaysOn(t *testing.T) {
	clearSamplerEnv(t)
	tp, _ := samplerProvider(t)
	if !isSampled(tp, nil) {
		t.Error("root span not sampled")
	}
	if !isSampled(tp, remoteCtx(true)) {
		t.Error("child of a sampled remote parent not sampled")
	}
	if isSampled(tp, remoteCtx(false)) {
		t.Error("child of an unsampled remote parent sampled (sampler is not parent-based)")
	}
	// Local parents: the child inherits the parent's decision.
	ctx, parent := tp.Tracer("t").Start(remoteCtx(false), "parent")
	if isSampled(tp, ctx) {
		t.Error("child of an unsampled local parent sampled")
	}
	parent.End()
	ctx, parent = tp.Tracer("t").Start(context.Background(), "parent")
	if !isSampled(tp, ctx) {
		t.Error("child of a sampled local root not sampled")
	}
	parent.End()
}

// --- ratio alias / code ratio are parent-based ---

func TestSampler_RatioZero_StillFollowsParent(t *testing.T) {
	for name, setup := range map[string]func(t *testing.T) []Option{
		"alias": func(t *testing.T) []Option { t.Setenv(EnvSampleRatio, "0"); return nil },
		"code":  func(*testing.T) []Option { return []Option{WithSampleRatio(0)} },
	} {
		t.Run(name, func(t *testing.T) {
			clearSamplerEnv(t)
			tp, _ := samplerProvider(t, setup(t)...)
			if !isSampled(tp, remoteCtx(true)) {
				t.Error("ratio 0 dropped the child of a sampled remote parent")
			}
			if isSampled(tp, nil) {
				t.Error("ratio 0 sampled a root span")
			}
		})
	}
}

func TestSampler_AliasOne_FollowsUnsampledParent(t *testing.T) {
	clearSamplerEnv(t)
	t.Setenv(EnvSampleRatio, "1")
	tp, _ := samplerProvider(t)
	if isSampled(tp, remoteCtx(false)) {
		t.Error("child of an unsampled remote parent sampled")
	}
}

// --- OTEL_TRACES_SAMPLER names ---

func TestSampler_KnownNames(t *testing.T) {
	cases := []struct {
		value                 string
		root, remoteUnsampled bool
	}{
		{"always_on", true, true},
		{"always_off", false, false},
		{"parentbased_always_on", true, false},
		{"parentbased_always_off", false, false},
		{"traceidratio", true, true},
		{"parentbased_traceidratio", true, false},
		{"ALWAYS_ON", true, true},
		{" parentbased_always_off ", false, false},
		{"\tParentBased_TraceIDRatio\n", true, false},
	}
	for _, c := range cases {
		t.Run(strings.TrimSpace(c.value), func(t *testing.T) {
			clearSamplerEnv(t)
			buf := captureDiag(t)
			t.Setenv(EnvTracesSampler, c.value)
			tp, _ := samplerProvider(t)
			if got := isSampled(tp, nil); got != c.root {
				t.Errorf("root sampled = %v, want %v", got, c.root)
			}
			if got := isSampled(tp, remoteCtx(false)); got != c.remoteUnsampled {
				t.Errorf("child of unsampled remote parent sampled = %v, want %v", got, c.remoteUnsampled)
			}
			if w := samplerWarnings(buf); len(w) != 0 {
				t.Errorf("unexpected sampler warnings: %v", w)
			}
		})
	}
}

func TestSampler_TraceIDRatio_UsesArg(t *testing.T) {
	clearSamplerEnv(t)
	t.Setenv(EnvTracesSampler, "traceidratio")
	t.Setenv(EnvTracesSamplerArg, " 0.3 ")
	_, o := samplerProvider(t)
	if d := buildSampler(o).Description(); d != "TraceIDRatioBased{0.3}" {
		t.Errorf("sampler = %s, want TraceIDRatioBased{0.3}", d)
	}
}

func TestSampler_ParentBasedTraceIDRatio_UsesArg(t *testing.T) {
	clearSamplerEnv(t)
	t.Setenv(EnvTracesSampler, "parentbased_traceidratio")
	t.Setenv(EnvTracesSamplerArg, "0")
	tp, o := samplerProvider(t)
	if d := buildSampler(o).Description(); !strings.Contains(d, "root:TraceIDRatioBased{0}") {
		t.Errorf("sampler = %s, want ParentBased(TraceIDRatioBased{0})", d)
	}
	if !isSampled(tp, remoteCtx(true)) || isSampled(tp, nil) {
		t.Error("parentbased_traceidratio=0 must follow a sampled parent and drop roots")
	}
}

func TestSampler_RatioWithoutArg_DefaultsToOneSilently(t *testing.T) {
	clearSamplerEnv(t)
	buf := captureDiag(t)
	t.Setenv(EnvTracesSampler, "traceidratio")
	_, o := samplerProvider(t)
	if d := buildSampler(o).Description(); d != "TraceIDRatioBased{1}" {
		t.Errorf("sampler = %s, want TraceIDRatioBased{1}", d)
	}
	if w := samplerWarnings(buf); len(w) != 0 {
		t.Errorf("unexpected warnings: %v", w)
	}
}

func TestSampler_InvalidArg_WarnsAndUsesOne(t *testing.T) {
	for _, arg := range []string{"abc", "2", "-0.1", "+0.5", "nan", "NaN", "inf", "0x1p-2", "1e-1", "0_5", ".5", "\uff10.5"} {
		t.Run(arg, func(t *testing.T) {
			clearSamplerEnv(t)
			buf := captureDiag(t)
			t.Setenv(EnvTracesSampler, "parentbased_traceidratio")
			t.Setenv(EnvTracesSamplerArg, arg)
			_, o := samplerProvider(t)
			if d := buildSampler(o).Description(); !strings.Contains(d, "root:TraceIDRatioBased{1}") {
				t.Errorf("sampler = %s, want ParentBased(TraceIDRatioBased{1})", d)
			}
			// buildSampler ran twice (provider + description): one warning each.
			w := samplerWarnings(buf)
			if len(w) != 2 || !strings.Contains(w[0], "variable="+EnvTracesSamplerArg) {
				t.Errorf("warnings = %v, want one naming %s per build", w, EnvTracesSamplerArg)
			}
		})
	}
}

func TestSampler_UnknownValue_WarnsAndUsesDefault(t *testing.T) {
	clearSamplerEnv(t)
	buf := captureDiag(t)
	t.Setenv(EnvTracesSampler, "bogus")
	t.Setenv(EnvSampleRatio, "0") // ignored: OTEL_TRACES_SAMPLER is set
	tp, o := samplerProvider(t)
	if o.SampleRatio != 1.0 {
		t.Errorf("SampleRatio = %v, want 1.0 (alias ignored)", o.SampleRatio)
	}
	if !isSampled(tp, nil) || isSampled(tp, remoteCtx(false)) {
		t.Error("unknown sampler must fall back to parentbased_always_on")
	}
	w := samplerWarnings(buf)
	if len(w) != 1 || !strings.Contains(w[0], "variable="+EnvTracesSampler) || !strings.Contains(w[0], "value=bogus") {
		t.Errorf("warnings = %v, want one naming %s and the value", w, EnvTracesSampler)
	}
}

func TestSampler_UnsupportedSpecNames_Warn(t *testing.T) {
	for _, name := range []string{"jaeger_remote", "parentbased_jaeger_remote", "XRay"} {
		t.Run(name, func(t *testing.T) {
			clearSamplerEnv(t)
			buf := captureDiag(t)
			t.Setenv(EnvTracesSampler, name)
			tp, _ := samplerProvider(t)
			if !isSampled(tp, nil) || isSampled(tp, remoteCtx(false)) {
				t.Error("unsupported sampler must fall back to parentbased_always_on")
			}
			w := samplerWarnings(buf)
			if len(w) != 1 || !strings.Contains(w[0], "unsupported by this helper") {
				t.Errorf("warnings = %v, want one saying \"unsupported by this helper\"", w)
			}
		})
	}
}

func TestSampler_AliasGrammar(t *testing.T) {
	cases := []struct {
		raw  string
		want float64
		warn bool
	}{
		{"0.25", 0.25, false},
		{" 1.0 ", 1.0, false},
		{"0", 0, false},
		{"2", 1.0, true},
		{"-0.1", 1.0, true},
		{"nan", 1.0, true},
		{"0x1p-2", 1.0, true},
		{"1e-1", 1.0, true},
		{"0_5", 1.0, true},
	}
	for _, c := range cases {
		t.Run(c.raw, func(t *testing.T) {
			clearSamplerEnv(t)
			buf := captureDiag(t)
			t.Setenv(EnvSampleRatio, c.raw)
			o := newOptions(WithServiceName("svc"))
			if o.SampleRatio != c.want {
				t.Errorf("SampleRatio = %v, want %v", o.SampleRatio, c.want)
			}
			// Only the invalid-value warning; the deprecation always fires.
			warned := strings.Contains(buf.String(), "sample ratio is not a number")
			if warned != c.warn {
				t.Errorf("warned = %v, want %v (log %q)", warned, c.warn, buf.String())
			}
		})
	}
}

// D1 A13 ruling: a code ratio wins whatever its value — 1.0 (the helper
// default) included — over OTEL_TRACES_SAMPLER and the deprecated alias.
func TestSampler_CodeRatioOneBeatsEnv(t *testing.T) {
	for name, env := range map[string][2]string{
		"standard var": {EnvTracesSampler, "always_off"},
		"alias":        {EnvSampleRatio, "0"},
	} {
		t.Run(name, func(t *testing.T) {
			clearSamplerEnv(t)
			t.Setenv(env[0], env[1])
			tp, o := samplerProvider(t, WithSampleRatio(1.0))
			if o.SampleRatio != 1.0 || o.sampleRatioFromEnv {
				t.Errorf("SampleRatio = %v (fromEnv=%v), want the code 1.0", o.SampleRatio, o.sampleRatioFromEnv)
			}
			if d := buildSampler(o).Description(); !strings.Contains(d, "root:TraceIDRatioBased{1}") {
				t.Errorf("sampler = %s, want ParentBased(TraceIDRatioBased{1})", d)
			}
			if !isSampled(tp, nil) {
				t.Error("root span not sampled with the code ratio 1.0")
			}
		})
	}
}
