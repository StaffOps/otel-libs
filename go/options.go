package otelhelper

import "strings"

// Options holds configuration for the OTel helper.
type Options struct {
	ServiceName          string
	Environment          DeploymentEnvironment
	OtelEndpoint         string
	DebugLevel           bool
	ExtraInstrumentation string
	ExportTimeoutMs      int
	SampleRatio          float64
	ResourceAttributes   map[string]string
}

// Option is a functional option for Setup.
type Option func(*Options)

func WithServiceName(name string) Option          { return func(o *Options) { o.ServiceName = name } }
func WithEnvironment(env DeploymentEnvironment) Option { return func(o *Options) { o.Environment = env } }
func WithDebug() Option                           { return func(o *Options) { o.DebugLevel = true } }
func WithEndpoint(endpoint string) Option         { return func(o *Options) { o.OtelEndpoint = endpoint } }
func WithSampleRatio(ratio float64) Option        { return func(o *Options) { o.SampleRatio = ratio } }
func WithExportTimeout(ms int) Option             { return func(o *Options) { o.ExportTimeoutMs = ms } }
func WithExtraInstrumentation(instr string) Option {
	return func(o *Options) { o.ExtraInstrumentation = instr }
}
func WithResourceAttributes(attrs map[string]string) Option {
	return func(o *Options) { o.ResourceAttributes = attrs }
}

// HasInstrumentation checks if a named instrumentation is enabled.
func (o *Options) HasInstrumentation(name string) bool {
	if o.DebugLevel {
		return true
	}
	for _, part := range strings.Split(o.ExtraInstrumentation, ",") {
		if strings.EqualFold(strings.TrimSpace(part), name) {
			return true
		}
	}
	return false
}

func newOptions(opts ...Option) *Options {
	o := &Options{
		ServiceName:          "my-service",
		Environment:          LOCAL,
		ExtraInstrumentation: "SQL",
		ExportTimeoutMs:      10_000,
		SampleRatio:          1.0,
		ResourceAttributes:   make(map[string]string),
	}
	for _, opt := range opts {
		opt(o)
	}
	o.resolveFromEnv()
	return o
}
