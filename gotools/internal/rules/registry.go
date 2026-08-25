package rules

// Default is the shipped rule set.
//
// Ordering here is irrelevant — NewRegistry sorts by ID — but the grouping is
// kept for readability, and every rule in the compliance set must be satisfied
// by the reference `user` resource. That invariant is asserted in
// TestReferenceTemplateIsClean, and it is the check that keeps the rules honest:
// if a rule fires on the template, the rule is wrong, not the template.
func Default() *Registry {
	return NewRegistry(
		// Layer boundaries
		LayerSQLBoundary,
		LayerDTOBoundary,

		// Handler contract
		HandlerSignature,
		HandlerBase,
		RoutesInHandler,

		// Repository contract
		RepoContract,
		RepoRowMapper,
		RepoNoRows,

		// DTOs
		RequestDTO,
		ResponseDTO,
		ResponseStatus,

		// Domain
		DomainTags,

		// Cross-cutting
		FXRegistration,
		ErrorHandling,
		DepAllowlist,
		FileSize,
		ValidatorStale,

		// Configuration
		SecretsInConfig,
		ConfigKeyExists,
		SwaggerVisible,

		// Idiomatic Go, advisory except for the package-declaration check
		GoIdiom,

		// Legacy detection (legacy_audit only)
		LegacyLibGeneration,
		LegacyRoutesFile,
		LegacyGinHandler,
		LegacyManualValidation,
		LegacySwaggo,
		LegacyResponseHelper,
		LegacyHandmadeHealth,
		LegacyGoWork,
		LegacyCommittedArtifacts,
	)
}
