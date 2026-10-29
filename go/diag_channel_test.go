package otelhelper

import (
	"go/ast"
	"go/parser"
	"go/token"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// diagAllowed lists the only exceptions to the diagnostics-channel rule, keyed
// "<file>:<enclosing func>|<what>" (func is "Recv.Name" for methods, "<decl>"
// at package level), with the allowed count. Everything else is a violation.
var diagAllowed = map[string]int{
	// Runtime error from the Prometheus /metrics server goroutine after Setup
	// returned — an SDK-pipeline error, not a configuration diagnostic.
	"metrics.go:configureMetrics|otel.Handle": 1,
	// The helper slog handler's fallback is the application's own log output
	// (stdout JSON), not helper diagnostics.
	"logging.go:newStdoutJSONHandler|os.Stdout": 1,
	"logging.go:NewLogger|slog.New":             1,
	// The OTEL_RESOURCE_ATTRIBUTES echo filter (resource.go) is an OTel error
	// handler: it forwards SDK pipeline errors to the previous/current handler,
	// and to log.Print (the SDK default handler's own output) when that one
	// would route them back to the filter.
	"resource.go:resourceEnvEchoFilter.Handle|otel.GetErrorHandler": 1,
	"resource.go:resourceEnvEchoFilter.Handle|log.Print":            1,
	"resource.go:quietResourceEnvEcho|otel.GetErrorHandler":         2,
}

// stdStreamFiles may reference os.Stdout/os.Stderr and build slog loggers:
// only diag.go, which owns the diagnostics channel. (console.go needs no
// exemption: the stdout exporters default to os.Stdout themselves.)
var stdStreamFiles = map[string]bool{"diag.go": true}

// deniedImports would bypass the channel (raw fds, stderr crash output, a
// second logr sink writing to the std streams).
var deniedImports = map[string]bool{"syscall": true, "runtime/debug": true, "github.com/go-logr/stdr": true}

// slogEmitters are log/slog package functions that write through (or
// replace) the default logger; referencing them at all (call or method
// value) is a violation.
var slogEmitters = map[string]bool{
	"Default": true, "SetDefault": true, "SetLogLoggerLevel": true, "Debug": true, "Info": true, "Warn": true,
	"Error": true, "Log": true, "LogAttrs": true, "DebugContext": true, "InfoContext": true, "WarnContext": true,
	"ErrorContext": true,
}

// TestDiagnosticsChannel enforces design D6 / CLAUDE.md "Helper diagnostics"
// over every non-test file of the helper package and of go/ext/*: helper
// diagnostics go only through diagLogger (stderr). Forbidden: the slog
// default-logger functions, slog.New outside diag.go, the log package,
// fmt.Print* (and Fprint* to a std stream, caught as an os.Stdout/os.Stderr
// reference), print/println, any os.Stdout / os.Stderr reference outside
// diag.go, os.NewFile, os.Open*/os.Create of a /dev/std*, /dev/fd or
// /proc/self/fd path, otel.GetErrorHandler, otel.Handle beyond diagAllowed,
// the syscall / runtime/debug / go-logr/stdr imports and dot-imports.
func TestDiagnosticsChannel(t *testing.T) {
	files, err := filepath.Glob("*.go")
	if err != nil {
		t.Fatal(err)
	}
	ext, _ := filepath.Glob(filepath.Join("ext", "*", "*.go"))
	files = append(files, ext...)
	fset := token.NewFileSet()
	seen := map[string]int{}
	var violations []string
	for _, name := range files {
		if strings.HasSuffix(name, "_test.go") {
			continue
		}
		f, err := parser.ParseFile(fset, name, nil, 0)
		if err != nil {
			t.Fatal(err)
		}
		imports := map[string]string{} // local name -> import path
		for _, imp := range f.Imports {
			p, _ := strconv.Unquote(imp.Path.Value)
			if deniedImports[p] {
				violations = append(violations, fset.Position(imp.Pos()).String()+": import "+p)
			}
			if imp.Name != nil && imp.Name.Name == "." {
				violations = append(violations, fset.Position(imp.Pos()).String()+": dot-import "+p+" hides call sites from this check")
			}
			local := filepath.Base(p)
			if imp.Name != nil {
				local = imp.Name.Name
			}
			imports[local] = p
		}
		base := filepath.Base(name)
		for _, decl := range f.Decls {
			scope := "<decl>"
			if fn, ok := decl.(*ast.FuncDecl); ok {
				scope = fn.Name.Name
				if fn.Recv != nil && len(fn.Recv.List) > 0 {
					scope = recvName(fn.Recv.List[0].Type) + "." + scope
				}
			}
			report := func(n ast.Node, what string) {
				key := name + ":" + scope + "|" + what
				if filepath.Dir(name) == "." {
					key = base + ":" + scope + "|" + what
				}
				seen[key]++
				if _, ok := diagAllowed[key]; !ok {
					violations = append(violations, fset.Position(n.Pos()).String()+": "+what+" (helper diagnostics go to diagLogger)")
				}
			}
			ast.Inspect(decl, func(n ast.Node) bool {
				switch x := n.(type) {
				case *ast.CallExpr:
					if id, ok := x.Fun.(*ast.Ident); ok && (id.Name == "print" || id.Name == "println") {
						report(x, "builtin "+id.Name)
					}
					if sel, ok := x.Fun.(*ast.SelectorExpr); ok {
						if pkg, ok := sel.X.(*ast.Ident); ok && imports[pkg.Name] == "os" &&
							(sel.Sel.Name == "OpenFile" || sel.Sel.Name == "Create" || sel.Sel.Name == "Open") && len(x.Args) > 0 {
							if lit, ok := x.Args[0].(*ast.BasicLit); ok && lit.Kind == token.STRING {
								if path, _ := strconv.Unquote(lit.Value); stdDevicePath(path) {
									report(x, "os."+sel.Sel.Name+"("+path+")")
								}
							}
						}
					}
				case *ast.SelectorExpr:
					pkg, ok := x.X.(*ast.Ident)
					if !ok {
						return true
					}
					sel := x.Sel.Name
					switch path := imports[pkg.Name]; {
					case path == "log/slog" && slogEmitters[sel]:
						report(x, "slog."+sel)
					case path == "log/slog" && sel == "New" && !stdStreamFiles[base]:
						report(x, "slog.New")
					case path == "log":
						report(x, "log."+sel)
					case path == "fmt" && strings.HasPrefix(sel, "Print"):
						report(x, "fmt."+sel)
					case path == "os" && sel == "NewFile":
						report(x, "os.NewFile")
					case path == "os" && (sel == "Stdout" || sel == "Stderr") && !stdStreamFiles[base]:
						report(x, "os."+sel)
					case path == "go.opentelemetry.io/otel" && (sel == "Handle" || sel == "GetErrorHandler"):
						report(x, "otel."+sel)
					}
				}
				return true
			})
		}
	}
	for key, n := range diagAllowed {
		if seen[key] != n {
			violations = append(violations, "allow-list is stale: "+key+" seen "+strconv.Itoa(seen[key])+"x, allowed "+strconv.Itoa(n))
		}
	}
	sort.Strings(violations)
	for _, v := range violations {
		t.Error(v)
	}
}

func recvName(e ast.Expr) string {
	switch x := e.(type) {
	case *ast.StarExpr:
		return recvName(x.X)
	case *ast.Ident:
		return x.Name
	case *ast.IndexExpr:
		return recvName(x.X)
	}
	return "?"
}

// stdDevicePath reports paths that alias the process's std streams.
func stdDevicePath(p string) bool {
	return strings.HasPrefix(p, "/dev/std") || strings.HasPrefix(p, "/dev/fd/") || strings.HasPrefix(p, "/proc/self/fd/")
}
