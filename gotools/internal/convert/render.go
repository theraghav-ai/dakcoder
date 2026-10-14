package convert

import (
	"fmt"
	"strings"
)

// Render is the report the model and the developer read: what was converted,
// and for every method that was not, why -- each reason a small job.
func Render(r *Result) string {
	var b strings.Builder
	verb := "converted"
	if r.DryRun {
		verb = "would convert"
	}
	fmt.Fprintf(&b, "%s %d handler method(s) by rule; %d left for an edit; %d repository method(s)/helper(s) now take context.Context.\n",
		verb, r.Converted, r.Left, r.Helpers)
	if len(r.Written) > 0 {
		fmt.Fprintf(&b, "Written: %s\n", strings.Join(r.Written, ", "))
	}
	for _, f := range r.Files {
		if f.Error != "" {
			fmt.Fprintf(&b, "\n%s: not converted -- %s\n", f.File, f.Error)
			continue
		}
		if f.Converted == 0 && f.Left == 0 {
			continue
		}
		fmt.Fprintf(&b, "\n%s: %d converted, %d left\n", f.File, f.Converted, f.Left)
		for _, m := range f.Methods {
			if m.Status != "left" {
				continue
			}
			fmt.Fprintf(&b, "  left  %s (lines %d-%d): %s\n", m.Name, m.Start, m.End, strings.Join(m.Reasons, "; "))
		}
		var created []string
		for _, m := range f.Methods {
			if m.Status == "converted" && m.Created {
				created = append(created, m.Name)
			}
		}
		if len(created) > 0 {
			fmt.Fprintf(&b, "  answers 201 -- register with http.StatusCreated: %s\n", strings.Join(created, ", "))
		}
	}
	if r.Left > 0 {
		b.WriteString("\nEach left method is unchanged. Convert it by hand: its reasons name the statement that did not fit a rule. unit_check confirms it.\n")
	}
	return b.String()
}
