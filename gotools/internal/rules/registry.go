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

		// Database performance. From the manual review of 41 services, where
		// round trips were the largest single category of finding — see
		// docs/CODE-REVIEW-FINDINGS.md §2.
		RepoBatchInLoop,
		RepoRawRows,
		RepoSelectStar,
		NoStoredProcedure,
		RepoMultiRoundTrip,
		RepoTransactionScope,
		RepoSQLNow,

		// Clients, context and deadlines
		ClientSingleton,
		CtxPropagation,
		ExternalCallTimeout,

		// Logging
		RepoNoLogging,
		NoSensitiveLogging,
		LogLevelHygiene,

		// DTOs
		RequestDTO,
		RequestValidateDepth,
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
		ConfigNoHardcode,
		SwaggerVisible,

		// Idiomatic Go, advisory except for the package-declaration check
		GoIdiom,

		// The reviewers' standing checklist, which appears near-verbatim in 15
		// of the 41 service sheets.
		NoFmtPrint,
		CtxNaming,
		PreferSwitch,
		MagicLiteral,
		HandlerSingleRepoCall,

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
