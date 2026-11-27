package otelsql

import (
	"errors"
	"net/url"
	"os"
	"strings"
	"sync"
)

// envPGDatabase is the libpq default database variable, read for the drivers
// mapped to postgresql when the DSN names no database.
const envPGDatabase = "PGDATABASE"

// dbFamily groups driver names by DSN syntax (the systemAttributes mapping).
type dbFamily int

const (
	familyOther dbFamily = iota
	familyPostgreSQL
	familyMySQL
	familySQLServer
	familySQLite
)

func familyOf(driverName string) dbFamily {
	switch strings.ToLower(driverName) {
	case "pgx", "pgx/v5", "postgres", "postgresql":
		return familyPostgreSQL
	case "mysql":
		return familyMySQL
	case "sqlserver", "mssql":
		return familySQLServer
	case "sqlite", "sqlite3":
		return familySQLite
	}
	return familyOther
}

// namespaceWarned dedupes the unknown-namespace line per driver name per
// process (tests clear it).
var namespaceWarned sync.Map

// resolveNamespace is the db.namespace of a pool (design D16): option > DSN >
// PGDATABASE (postgresql drivers, unless the DSN names a service) > "" with
// one stderr line per driver name per process, naming the driver only.
// SQLite resolves to "" without a line unless the option is given. A name
// taken from the DSN or PGDATABASE that does not look like a database name
// (see validNamespace) is treated as unknown: a parse that went wrong must
// not put a credential or a host into an attribute.
func resolveNamespace(driverName, dsn, option string) string {
	if option != "" {
		return option // the application's value, used as given
	}
	family := familyOf(driverName)
	if family == familySQLite {
		return ""
	}
	ns, service := namespaceFromDSN(family, dsn)
	if ns == "" && family == familyPostgreSQL && !service {
		ns = os.Getenv(envPGDatabase)
	}
	if ns != "" && validNamespace(ns) {
		return ns
	}
	if _, seen := namespaceWarned.LoadOrStore(driverName, true); !seen {
		// The driver name only: the DSN may carry credentials.
		diagLogger.Warn("otelhelper: db.namespace unknown for driver " + driverName + "; pass otelsql.WithDBNamespace")
	}
	return ""
}

// validNamespace rejects a name with a character no database name of the
// parsed syntaxes needs but credentials, hosts and option lists carry.
func validNamespace(ns string) bool {
	return !strings.ContainsAny(ns, "@:,=(){} \t\n\r\v\f")
}

// namespaceFromDSN parses the database name out of dsn with the standard
// library only; "" when the syntax is unknown, malformed or names no
// database. service reports a PostgreSQL service= parameter (the service
// file then decides the database, so PGDATABASE is not read). It never logs
// or returns any other part of the DSN.
func namespaceFromDSN(family dbFamily, dsn string) (ns string, service bool) {
	switch family {
	case familyPostgreSQL:
		if hasPrefixFold(dsn, "postgres://") || hasPrefixFold(dsn, "postgresql://") {
			return postgresURLDatabase(dsn)
		}
		kv, ok := parseKeywords(dsn)
		if !ok {
			return "", false
		}
		_, service = kv["service"]
		if v := kv["dbname"]; v != "" {
			return v, service
		}
		return kv["database"], service
	case familyMySQL:
		return mysqlDatabase(dsn), false
	case familySQLServer:
		return sqlServerDatabase(dsn), false
	}
	return "", false
}

func hasPrefixFold(s, prefix string) bool {
	return len(s) >= len(prefix) && strings.EqualFold(s[:len(prefix)], prefix)
}

// postgresURLDatabase reads a libpq URL as pgx does: net/url parses it
// (any parse error = unknown), the database is the path, and ?dbname= (or
// pgx's ?database=) wins, also when present but empty (pgx then has no
// database either). Userinfo, query and fragment are split by the URL
// parser, so a password in any of them is never read as the database.
func postgresURLDatabase(dsn string) (string, bool) {
	u, err := url.Parse(dsn)
	if err != nil {
		if u, err = parseSocketHostURL(dsn); err != nil {
			return "", false
		}
	}
	q := u.Query()
	service := q.Has("service")
	for _, key := range []string{"dbname", "database"} {
		if q.Has(key) {
			return q.Get(key), service
		}
	}
	return strings.TrimPrefix(u.Path, "/"), service
}

// parseSocketHostURL handles libpq's percent-encoded Unix socket host
// (postgres://%2Fvar%2Frun%2Fpostgresql/db), which net/url rejects but pgx
// accepts: the host is replaced by a placeholder and the URL parsed again,
// for its path and query only. Any other parse error stays an error.
func parseSocketHostURL(dsn string) (*url.URL, error) {
	scheme := strings.Index(dsn, "://") + 3
	rest := dsn[scheme:]
	end := strings.IndexAny(rest, "/?#")
	if end < 0 {
		end = len(rest)
	}
	at := strings.LastIndexByte(rest[:end], '@') + 1
	if !hasPrefixFold(rest[at:end], "%2F") {
		return nil, errNotSocketHost
	}
	return url.Parse(dsn[:scheme] + rest[:at] + "socket" + rest[end:])
}

var errNotSocketHost = errors.New("not a socket host")

const kwSpace = " \t\n\r\v\f"

// parseKeywords reads a libpq keyword/value string (host=x dbname='my db'):
// whitespace around '=', single-quoted values with backslash escapes,
// unquoted values ending at whitespace; the last occurrence of a key wins.
// ok is false for a malformed string. An unquoted value may hold '=' (a
// base64 password); an empty "dbname=" then reads the next keyword as its
// value, which validNamespace rejects (no PGDATABASE fallback, one line).
func parseKeywords(dsn string) (kv map[string]string, ok bool) {
	kv = map[string]string{}
	s := dsn
	for {
		s = strings.TrimLeft(s, kwSpace)
		if s == "" {
			return kv, true
		}
		eq := strings.IndexByte(s, '=')
		if eq <= 0 {
			return nil, false
		}
		k := strings.TrimRight(s[:eq], kwSpace)
		if strings.ContainsAny(k, kwSpace) {
			return nil, false
		}
		s = strings.TrimLeft(s[eq+1:], kwSpace)
		var v strings.Builder
		if strings.HasPrefix(s, "'") {
			s = s[1:]
			closed := false
			for len(s) > 0 {
				c := s[0]
				s = s[1:]
				if c == '\\' && len(s) > 0 {
					v.WriteByte(s[0])
					s = s[1:]
					continue
				}
				if c == '\'' {
					closed = true
					break
				}
				v.WriteByte(c)
			}
			if !closed {
				return nil, false
			}
		} else {
			for len(s) > 0 && !strings.ContainsRune(kwSpace, rune(s[0])) {
				c := s[0]
				s = s[1:]
				if c == '\\' && len(s) > 0 {
					c = s[0]
					s = s[1:]
				}
				v.WriteByte(c)
			}
		}
		kv[k] = v.String()
	}
}

// mysqlDatabase reads a go-sql-driver DSN
// [user[:pass]@][net[(addr)]]/dbname[?params]: the name runs from the last
// '/' (a password may contain '/') to the first '?' after it, path-unescaped
// as the driver does. A candidate holding '@', '(' or ')' is not a database
// name but the rest of a DSN without one (u:a/b@tcp(h)); a DSN with neither
// '@' nor '(' (u:a/b) has no database the helper can tell from a password.
func mysqlDatabase(dsn string) string {
	slash := strings.LastIndexByte(dsn, '/')
	if slash < 0 {
		return ""
	}
	db := dsn[slash+1:]
	if i := strings.IndexByte(db, '?'); i >= 0 {
		db = db[:i]
	}
	// A DSN starting with '/' (no user, default address) has nothing before
	// the slash that could hold a secret.
	if strings.ContainsAny(db, "@()") || (slash > 0 && !strings.ContainsAny(dsn, "@(")) {
		return "" // the rest of a DSN without a database, or no DSN shape at all
	}
	if unescaped, err := url.PathUnescape(db); err == nil {
		return unescaped
	}
	return ""
}

// sqlServerDatabase reads a go-mssqldb DSN: the URL form's ?database=, or
// database= / initial catalog= in the ADO and ODBC (odbc: prefix) forms.
// The instance name of the URL path is not part of the namespace.
func sqlServerDatabase(dsn string) string {
	if hasPrefixFold(dsn, "sqlserver://") {
		u, err := url.Parse(dsn) // as go-mssqldb parses it
		if err != nil {
			return ""
		}
		return u.Query().Get("database")
	}
	if hasPrefixFold(dsn, "odbc:") {
		dsn = dsn[len("odbc:"):]
	}
	pairs, ok := parseConnString(dsn)
	if !ok {
		return ""
	}
	value := ""
	for _, p := range pairs {
		switch strings.ToLower(p[0]) {
		case "database", "initial catalog":
			value = p[1]
		}
	}
	return value
}

// parseConnString splits an ADO / ODBC connection string into key/value
// pairs on ';', honoring values in {braces} ("}}" is a literal '}') and in
// "double" or 'single' quotes (a doubled quote is literal). ok is false for
// an unterminated brace or quote, or text after a closing one, so a value
// holding ';' can never be read as further keys.
func parseConnString(s string) (pairs [][2]string, ok bool) {
	for len(s) > 0 {
		s = strings.TrimLeft(s, kwSpace+";")
		if s == "" {
			break
		}
		eq := strings.IndexByte(s, '=')
		semi := strings.IndexByte(s, ';')
		if eq < 0 || (semi >= 0 && semi < eq) {
			return nil, false // a key without a value
		}
		key := strings.TrimSpace(s[:eq])
		s = strings.TrimLeft(s[eq+1:], kwSpace)
		var v strings.Builder
		if len(s) > 0 && (s[0] == '{' || s[0] == '"' || s[0] == '\'') {
			open, closer := s[0], s[0]
			if open == '{' {
				closer = '}'
			}
			s = s[1:]
			closed := false
			for len(s) > 0 {
				c := s[0]
				s = s[1:]
				if c == closer {
					if len(s) > 0 && s[0] == closer {
						v.WriteByte(c)
						s = s[1:]
						continue
					}
					closed = true
					break
				}
				v.WriteByte(c)
			}
			if !closed {
				return nil, false
			}
			s = strings.TrimLeft(s, kwSpace)
			if len(s) > 0 && s[0] != ';' {
				return nil, false
			}
		} else {
			end := strings.IndexByte(s, ';')
			if end < 0 {
				end = len(s)
			}
			v.WriteString(strings.TrimSpace(s[:end]))
			s = s[end:]
		}
		pairs = append(pairs, [2]string{key, v.String()})
	}
	return pairs, true
}
