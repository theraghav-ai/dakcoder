## Check your Go toolchain

dakcoder compiles on **your** machine. That is what makes it fast and what keeps
your repository local — and it also means your Go setup has to be right.

Doctor checks the whole chain: the Go version, `GOPRIVATE` for
`gitlab.cept.gov.in`, your git credential for the internal module registry,
`gopls`, `govalid`, and whether a corporate proxy is intercepting loopback.

Every failure comes with a fix you can apply from the report.
