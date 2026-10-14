package convert

import (
	"fmt"
	"sort"
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

// RenderService is the service conversion's report.
func RenderService(r *ServiceResult) string {
	var b strings.Builder
	if r.Error != "" {
		fmt.Fprintf(&b, "service wiring not converted: %s\n", r.Error)
		return b.String()
	}
	total, pending := 0, 0
	for _, h := range r.Handlers {
		total += h.Routes
		pending += h.Pending
	}
	verb := "registered"
	if r.DryRun {
		verb = "would register"
	}
	fmt.Fprintf(&b, "%s %d route(s) on %d handler(s) through Routes(); %d route(s) wait on a method still on gin.\n",
		verb, total, len(r.Handlers), pending)
	for _, h := range r.Handlers {
		fmt.Fprintf(&b, "  %s (%s): prefix %q, %d route(s)", h.Type, h.File, h.Prefix, h.Routes)
		if h.Pending > 0 {
			fmt.Fprintf(&b, ", %d pending", h.Pending)
		}
		b.WriteString("\n")
	}
	if len(r.Wired) > 0 {
		fmt.Fprintf(&b, "bootstrap registers as controllers: %s\n", strings.Join(r.Wired, ", "))
	}
	if len(r.Retired) > 0 {
		fmt.Fprintf(&b, "retired: %s (main.go no longer invokes it)\n", strings.Join(r.Retired, ", "))
	}
	if len(r.Tests) > 0 {
		fmt.Fprintf(&b, "test harness on the template router: %s\n", strings.Join(r.Tests, ", "))
	}
	if len(r.Pending) > 0 {
		b.WriteString("\nRoutes written as TODO comments in Routes(), because their method is still on gin:\n")
		for _, p := range r.Pending {
			fmt.Fprintf(&b, "  %s\n", p)
		}
	}
	if len(r.Dropped) > 0 {
		b.WriteString("\nDropped (the template server provides them):\n")
		for _, d := range r.Dropped {
			fmt.Fprintf(&b, "  %s\n", d)
		}
	}
	for _, n := range r.Notes {
		fmt.Fprintf(&b, "note: %s\n", n)
	}
	if len(r.Written) > 0 {
		fmt.Fprintf(&b, "files: %s\n", strings.Join(r.Written, ", "))
	}
	return b.String()
}

// RenderDTOs is the validation step's report.
func RenderDTOs(r *DTOResult) string {
	var b strings.Builder
	verb := "validated"
	if r.DryRun {
		verb = "would validate"
	}
	if r.Moved > 0 {
		fmt.Fprintf(&b, "moved %d type(s) into handler/request/request.go.\n", r.Moved)
	}
	fmt.Fprintf(&b, "%s %d request type(s): %d by govalid, %d by the legacy runtime validator (custom tags); %d already had Validate().\n",
		verb, len(r.Generated)+len(r.Runtime), len(r.Generated), len(r.Runtime), len(r.Already))
	if len(r.Runtime) > 0 {
		names := make([]string, 0, len(r.Runtime))
		for n := range r.Runtime {
			names = append(names, n)
		}
		sort.Strings(names)
		b.WriteString("Runtime-validated, because govalid does not implement these tags:\n")
		for _, n := range names {
			fmt.Fprintf(&b, "  %s: %s\n", n, strings.Join(r.Runtime[n], ", "))
		}
	}
	for _, n := range r.Notes {
		fmt.Fprintf(&b, "note: %s\n", n)
	}
	if len(r.Written) > 0 {
		fmt.Fprintf(&b, "files: %d written\n", len(r.Written))
	}
	return b.String()
}
