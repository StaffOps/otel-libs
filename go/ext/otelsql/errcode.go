package otelsql

import (
	"errors"
	"reflect"
	"strconv"
)

// maxErrorNodes bounds the error-tree walk of mysqlNumber, so a huge
// errors.Join tree costs a bounded amount of work on a failed query.
const maxErrorNodes = 64

// responseStatusCode is the database response status code carried by err
// (design D16 Values): a PostgreSQL SQLSTATE read through a SQLState() method
// (pgx *pgconn.PgError, lib/pq *pq.Error), else a MySQL server error number
// read by reflection from a MySQLError struct's Number field
// (go-sql-driver *mysql.MySQLError; the module imports no driver). Message
// text never reaches the result.
func responseStatusCode(err error) (string, bool) {
	if err == nil {
		return "", false
	}
	var s interface{ SQLState() string }
	if errors.As(err, &s) {
		if code := safeSQLState(s); validSQLState(code) {
			return code, true
		}
	}
	if n, ok := mysqlNumber(err); ok {
		return strconv.FormatUint(n, 10), true
	}
	return "", false
}

// safeSQLState calls SQLState, treating a panic (a typed nil receiver) as no
// code: a broken driver error must not crash the query path.
func safeSQLState(s interface{ SQLState() string }) (code string) {
	defer func() {
		if recover() != nil {
			code = ""
		}
	}()
	return s.SQLState()
}

// validSQLState accepts ^[0-9A-Z]{5}$ with at least one digit: every
// PostgreSQL 17 SQLSTATE has one, and the digit rule rejects socket codes
// such as EPIPE that some drivers report in the same field.
func validSQLState(code string) bool {
	if len(code) != 5 {
		return false
	}
	digit := false
	for i := 0; i < len(code); i++ {
		c := code[i]
		switch {
		case '0' <= c && c <= '9':
			digit = true
		case 'A' <= c && c <= 'Z':
		default:
			return false
		}
	}
	return digit
}

// mysqlNumber walks err's Unwrap tree (single and multi) for a struct, or a
// pointer to one, named MySQLError with an unsigned integer Number field, and accepts
// a server error number: 1–65535 outside the client range 2000–2999 (CR_*
// codes, such as 2003 connection refused or 2013 server lost, are client
// errors, not server responses).
func mysqlNumber(err error) (uint64, bool) {
	queue := []error{err}
	for visited := 0; len(queue) > 0 && visited < maxErrorNodes; visited++ {
		e := queue[0]
		queue = queue[1:]
		if n, ok := mysqlNumberOf(e); ok {
			return n, true
		}
		switch u := e.(type) {
		case interface{ Unwrap() error }:
			if next := u.Unwrap(); next != nil {
				queue = append(queue, next)
			}
		case interface{ Unwrap() []error }:
			for _, next := range u.Unwrap() {
				if next != nil {
					queue = append(queue, next)
				}
			}
		}
	}
	return 0, false
}

// mysqlNumberOf reads e's Number field when e is a MySQLError struct or a
// non-nil pointer to one, and applies the accepted range.
func mysqlNumberOf(e error) (uint64, bool) {
	v := reflect.ValueOf(e)
	if v.Kind() == reflect.Pointer {
		if v.IsNil() {
			return 0, false
		}
		v = v.Elem()
	}
	if v.Kind() != reflect.Struct || v.Type().Name() != "MySQLError" {
		return 0, false
	}
	f := v.FieldByName("Number")
	switch f.Kind() {
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
	default:
		return 0, false // go-sql-driver's Number is uint16
	}
	n := f.Uint()
	if n < 1 || n > 65535 || (n >= 2000 && n <= 2999) {
		return 0, false
	}
	return n, true
}
