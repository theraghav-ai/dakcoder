package rules

import (
	"fmt"
	"time"

	"gitlab.cept.gov.in/it-2.0/dakcoder/gotools/internal/workspace"
)

// Analyze loads a workspace and runs the applicable rules against it.
//
// This is the single entry point used by both the CLI and the MCP server, so
// the two can never drift in what they consider a violation — a linter that
// disagrees with itself depending on how it was invoked is worse than no
// linter.
func Analyze(root string, opts RunOptions) (*Result, error) {
	start := time.Now()

	cfg := opts.Config
	if cfg.isZero() {
		loaded, err := LoadConfig(root)
		if err != nil {
			return nil, err
		}
		cfg = loaded
	}
	opts.Config = cfg

	loadOpts := []workspace.Option{}
	if needsGenerated(opts) {
		loadOpts = append(loadOpts, workspace.WithGenerated())
	}
	if needsTests(opts) {
		loadOpts = append(loadOpts, workspace.WithTests())
	}

	ws, err := workspace.Load(root, loadOpts...)
	if err != nil {
		return nil, fmt.Errorf("load workspace: %w", err)
	}

	res, err := Default().Run(ws, opts)
	if err != nil {
		return nil, err
	}
	res.DurationMS = time.Since(start).Milliseconds()
	return res, nil
}

// needsGenerated reports whether the selected rules require generated files to
// be loaded. Only validator-generated does, and loading them for every run
// would produce findings the agent must not act on.
func needsGenerated(opts RunOptions) bool {
	if len(opts.Only) == 0 {
		return true // validator-generated is in the default set
	}
	for _, id := range opts.Only {
		if id == ValidatorStale.ID {
			return true
		}
	}
	return false
}

// needsTests reports whether test files must be loaded. dep-allowlist inspects
// them (against the test allow-list) so that a genuinely disallowed test
// dependency is still caught.
func needsTests(opts RunOptions) bool {
	if len(opts.Only) == 0 {
		return true
	}
	for _, id := range opts.Only {
		if id == DepAllowlist.ID {
			return true
		}
	}
	return false
}
