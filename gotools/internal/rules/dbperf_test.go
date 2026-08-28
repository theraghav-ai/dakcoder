package rules

import (
	"strings"
	"testing"
)

// repoFile wraps a repository method body in the imports every fixture needs,
// so each table entry is only the code under test.
func repoFile(body string) string {
	return `package postgres

import (
	"context"

	sq "github.com/Masterminds/squirrel"
	"github.com/jackc/pgx/v5"
	dblib "gitlab.cept.gov.in/it-2.0-common/n-api-db"
	config "gitlab.cept.gov.in/it-2.0-common/api-config"

	"pisapi/core/domain"
)

var _ = sq.Eq{}
var _ = pgx.ErrNoRows
var _ = context.Background

type UserRepository struct {
	db  *dblib.DB
	cfg *config.Config
}

` + body + `
`
}

func TestRepoBatchInLoop(t *testing.T) {
	tests := []struct {
		name string
		body string
		want bool
	}{
		{
			name: "query inside a range body is the N+1",
			want: true,
			body: `func (r *UserRepository) Insert(ctx context.Context, ids []int64) error {
	for _, id := range ids {
		q := dblib.Psql.Insert("users").Columns("id").Values(id)
		if _, err := dblib.Insert(ctx, r.db, q); err != nil {
			return err
		}
	}
	return nil
}`,
		},
		{
			name: "query inside a classic for body",
			want: true,
			body: `func (r *UserRepository) Insert(ctx context.Context, ids []int64) error {
	for i := 0; i < len(ids); i++ {
		q := dblib.Psql.Insert("users").Columns("id").Values(ids[i])
		if _, err := dblib.Insert(ctx, r.db, q); err != nil {
			return err
		}
	}
	return nil
}`,
		},
		{
			// The exclusion that makes the rule trustworthy. The query is
			// syntactically a child of the RangeStmt but runs exactly once, so
			// counting it would be a false positive on the one rule whose
			// remedy is a rewrite.
			name: "query in a range header runs once, not per element",
			want: false,
			body: `func (r *UserRepository) List(ctx context.Context) error {
	q := dblib.Psql.Select("id").From("users")
	rows, err := dblib.SelectRows(ctx, r.db, q, pgx.RowToStructByName[domain.User])
	if err != nil {
		return err
	}
	for range rows {
		_ = 1
	}
	return nil
}`,
		},
		{
			name: "queueing into a batch inside a loop is the fix, not the defect",
			want: false,
			body: `func (r *UserRepository) Insert(ctx context.Context, ids []int64) error {
	batch := &pgx.Batch{}
	for _, id := range ids {
		q := dblib.Psql.Insert("users").Columns("id").Values(id)
		if err := dblib.QueueExecRow(batch, q); err != nil {
			return err
		}
	}
	results := r.db.SendBatch(ctx, batch)
	if results != nil {
		defer results.Close()
	}
	return nil
}`,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := lint(t, RepoBatchInLoop.ID, map[string]string{
				"repo/postgres/user.go": repoFile(tc.body),
			})
			if (len(got) > 0) != tc.want {
				t.Errorf("violations = %d, want fired=%v\n%v", len(got), tc.want, got)
			}
		})
	}
}

func TestRepoMultiRoundTrip(t *testing.T) {
	call := `	q%d := dblib.Psql.Select("id").From("users")
	if _, err := dblib.SelectOne(ctx, r.db, q%d, pgx.RowToStructByName[domain.User]); err != nil {
		return err
	}
`
	body := func(n int) string {
		var b strings.Builder
		b.WriteString("func (r *UserRepository) Do(ctx context.Context) error {\n")
		for i := range n {
			b.WriteString(strings.ReplaceAll(call, "%d", string(rune('a'+i))))
		}
		b.WriteString("\treturn nil\n}")
		return b.String()
	}

	tests := []struct {
		calls int
		want  string // substring of the expected message, "" for silence
	}{
		{1, ""},
		{2, "a batch may be possible"},
		{3, "batch them where feasible"},
		{5, "batch them where feasible"},
	}
	for _, tc := range tests {
		got := lint(t, RepoMultiRoundTrip.ID, map[string]string{
			"repo/postgres/user.go": repoFile(body(tc.calls)),
		})
		switch {
		case tc.want == "" && len(got) > 0:
			t.Errorf("%d call(s): fired unexpectedly: %v", tc.calls, got)
		case tc.want == "":
		case len(got) == 0:
			t.Errorf("%d call(s): expected %q, got nothing", tc.calls, tc.want)
		case !strings.Contains(got[0].Message, tc.want):
			t.Errorf("%d call(s): message = %q, want it to contain %q", tc.calls, got[0].Message, tc.want)
		}
	}

	// Neither tier ever blocks: batching is not mandatory, because a batch is
	// not always available.
	got := lint(t, RepoMultiRoundTrip.ID, map[string]string{
		"repo/postgres/user.go": repoFile(body(5)),
	})
	for _, v := range got {
		if v.Severity != SeverityWarning {
			t.Errorf("severity = %s, want warning — batching is never mandatory", v.Severity)
		}
	}
}

func TestRepoTransactionScope(t *testing.T) {
	tests := []struct {
		name string
		body string
		want string
	}{
		{
			name: "a transaction around one statement buys nothing",
			want: "buys nothing",
			body: `func (r *UserRepository) Do(ctx context.Context) error {
	tx, err := r.db.Begin(ctx)
	if err != nil {
		return err
	}
	q := dblib.Psql.Insert("users").Columns("id").Values(1)
	_, err = dblib.Insert(ctx, r.db, q)
	return err
}`,
		},
		{
			// The nuance the reviewers' shorthand loses: a transaction holding
			// several writes together is doing its job. The rule may suggest
			// batching inside it; it must never say "drop it".
			name: "a transaction over several statements is not told to drop it",
			want: "round trips remain",
			body: `func (r *UserRepository) Do(ctx context.Context) error {
	tx, err := r.db.Begin(ctx)
	if err != nil {
		return err
	}
	_ = tx
	qa := dblib.Psql.Insert("users").Columns("id").Values(1)
	if _, err := dblib.Insert(ctx, r.db, qa); err != nil {
		return err
	}
	qb := dblib.Psql.Update("users").Set("id", 2)
	if _, err := dblib.Update(ctx, r.db, qb); err != nil {
		return err
	}
	return nil
}`,
		},
		{
			name: "no transaction, nothing to say",
			want: "",
			body: `func (r *UserRepository) Do(ctx context.Context) error {
	q := dblib.Psql.Insert("users").Columns("id").Values(1)
	_, err := dblib.Insert(ctx, r.db, q)
	return err
}`,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := lint(t, RepoTransactionScope.ID, map[string]string{
				"repo/postgres/user.go": repoFile(tc.body),
			})
			switch {
			case tc.want == "":
				if len(got) > 0 {
					t.Errorf("fired unexpectedly: %v", got)
				}
			case len(got) == 0:
				t.Fatalf("expected %q, got nothing", tc.want)
			case !strings.Contains(got[0].Message, tc.want):
				t.Errorf("message = %q, want it to contain %q", got[0].Message, tc.want)
			}
		})
	}
}

func TestRepoRawRows(t *testing.T) {
	fired := func(body string) bool {
		return len(lint(t, RepoRawRows.ID, map[string]string{
			"repo/postgres/user.go": repoFile(body),
		})) > 0
	}

	if !fired(`func (r *UserRepository) List(ctx context.Context) error {
	rows, err := r.db.Query(ctx, "select id from users")
	if err != nil {
		return err
	}
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return err
		}
	}
	return nil
}`) {
		t.Error("rows.Next()/rows.Scan() should be reported: both bind by position")
	}

	if !fired(`func (r *UserRepository) One(ctx context.Context) error {
	var id int64
	return r.db.QueryRow(ctx, "select id from users").Scan(&id)
}`) {
		t.Error("QueryRow().Scan() should be reported")
	}

	if fired(`func (r *UserRepository) List(ctx context.Context) ([]domain.User, error) {
	q := dblib.Psql.Select("id").From("users")
	return dblib.SelectRows(ctx, r.db, q, pgx.RowToStructByName[domain.User])
}`) {
		t.Error("the dblib by-name path is the remedy and must not fire")
	}
}

func TestRepoSelectStarAndStoredProcedure(t *testing.T) {
	got := lint(t, RepoSelectStar.ID, map[string]string{
		"repo/postgres/user.go": repoFile(`func (r *UserRepository) List(ctx context.Context) error {
	q := dblib.Psql.Select("*").From("users")
	_ = q
	return nil
}`),
	})
	if len(got) == 0 {
		t.Error(`Select("*") should be reported`)
	}

	got = lint(t, NoStoredProcedure.ID, map[string]string{
		"repo/postgres/user.go": repoFile(`func (r *UserRepository) Run(ctx context.Context) error {
	_, err := dblib.Exec(ctx, r.db, "CALL pao.recalculate_balances(1)")
	return err
}`),
	})
	if len(got) == 0 {
		t.Error("a CALL statement should be reported")
	}
}

// TestRepoContractRejectsDetachedTimeout covers the false negative found while
// reading the legacy corpus: a timeout that satisfies both of repo-contract's
// original conditions and still severs the request.
func TestRepoContractRejectsDetachedTimeout(t *testing.T) {
	got := lint(t, RepoContract.ID, map[string]string{
		"repo/postgres/user.go": repoFile(`func (r *UserRepository) Get(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(context.Background(), r.cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	q := dblib.Psql.Select("id").From("users")
	_, err := dblib.SelectOne(ctx, r.db, q, pgx.RowToStructByName[domain.User])
	return err
}`),
	})
	found := false
	for _, v := range got {
		if strings.Contains(v.Message, "rather than the request context") {
			found = true
		}
	}
	if !found {
		t.Errorf("a timeout parented on context.Background() must be reported; got %v", got)
	}

	// And the correct form stays silent.
	got = lint(t, RepoContract.ID, map[string]string{
		"repo/postgres/user.go": repoFile(`func (r *UserRepository) Get(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, r.cfg.GetDuration("db.QueryTimeoutMed"))
	defer cancel()
	q := dblib.Psql.Select("id").From("users")
	_, err := dblib.SelectOne(ctx, r.db, q, pgx.RowToStructByName[domain.User])
	return err
}`),
	})
	if len(got) > 0 {
		t.Errorf("deriving the timeout from ctx is correct; got %v", got)
	}
}
