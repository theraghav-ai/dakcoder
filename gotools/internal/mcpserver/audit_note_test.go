package mcpserver

import (
	"strings"
	"testing"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/libversion"
)

// The warning has to fire on an *incomplete* report, not only on a dead one.
//
// It was gated on `!res.Reachable` — "did anything answer" — so one successful
// lookup withheld it from a report thirteen lookups short. The field run that
// found this got four blank version columns, no explanation, and asked the
// developer the same question three times.
func TestThePartialReportWarnsEvenThoughTheRegistryAnswered(t *testing.T) {
	note := versionNote(&libversion.Result{
		Reachable:  true,
		Unresolved: []string{libversion.ModulePrefix + "n-api-db", libversion.ModulePrefix + "n-api-log"},
	})

	if !strings.Contains(note, "2 lookup(s) did not answer") {
		t.Fatalf("an incomplete report did not say so: %q", note)
	}
	if !strings.Contains(note, "n-api-db") || strings.Contains(note, libversion.ModulePrefix) {
		t.Fatalf("the modules are not named, or not trimmed: %q", note)
	}
	if !strings.Contains(note, "missing rather than empty") {
		t.Fatalf("a blank column still reads as 'none published': %q", note)
	}
}

func TestAnUnreachableRegistryStillSaysSo(t *testing.T) {
	note := versionNote(&libversion.Result{Reachable: false})
	if !strings.Contains(note, "not reachable") {
		t.Fatalf("%q", note)
	}
}

// The note is read by the acting phase of a migration, whose whole job that
// turn is to change these versions. "Tell the user and let them decide" is
// right for a review and reads as "stop" there.
func TestTheNoteNamesTheMoveThatWorks(t *testing.T) {
	note := versionNote(&libversion.Result{Reachable: true})
	for _, want := range []string{"go_mod", "`version` omitted", "separate release lines"} {
		if !strings.Contains(note, want) {
			t.Fatalf("the note does not say %q: %q", want, note)
		}
	}
	if !strings.Contains(note, "regression hunt") {
		t.Fatal("the review-time rule was dropped rather than qualified")
	}
}

func TestACompleteReportGetsTheBareNote(t *testing.T) {
	note := versionNote(&libversion.Result{Reachable: true})
	if strings.Contains(note, "did not answer") || strings.Contains(note, "not reachable") {
		t.Fatalf("a complete report was warned about: %q", note)
	}
}
