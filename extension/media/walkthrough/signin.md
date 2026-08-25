## Sign in with GitLab

dakcoder runs **as you**. Your GitLab identity is what the gateway meters quota
against and what the audit log records, so there is no shared token to leak and
no ambiguity about who asked for a change.

Sign-in opens your browser once. The refresh token is stored in the operating
system's credential store — Windows Credential Manager, macOS Keychain, or the
Linux Secret Service — and never in a settings file.
