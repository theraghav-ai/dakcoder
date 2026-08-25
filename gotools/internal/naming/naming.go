// Package naming holds the identifier conventions shared by the rules engine
// and the scaffolders.
//
// It exists because the two halves of gotools must agree. The linter decides
// whether `db:"ppo_number"` is the right tag for a field called PPONumber; the
// scaffolder writes that tag. If each carried its own snake_case function, the
// first divergence would be the scaffolder emitting code its own linter
// rejects — which is the least forgivable bug a tool like this can have.
//
// One implementation, one test suite, both callers.
package naming

import (
	"strings"
	"unicode"
)

// initialisms are rendered fully upper-case in Go identifiers.
//
// The first block is the conventional Go set (the same list golint carried and
// staticcheck still uses). The second is IT 2.0 domain vocabulary: the spike
// produced `PpoNumber` for a pension PPO number, which is a real identifier in
// every DOP service, so the correction has to be built in rather than left to
// the model to get right.
var initialisms = map[string]bool{
	// Conventional Go.
	"ACL": true, "API": true, "ASCII": true, "CPU": true, "CSS": true,
	"DNS": true, "EOF": true, "GUID": true, "HTML": true, "HTTP": true,
	"HTTPS": true, "ID": true, "IP": true, "JSON": true, "LHS": true,
	"QPS": true, "RAM": true, "RHS": true, "RPC": true, "SLA": true,
	"SMTP": true, "SQL": true, "SSH": true, "TCP": true, "TLS": true,
	"TTL": true, "UDP": true, "UI": true, "UID": true, "UUID": true,
	"URI": true, "URL": true, "UTF8": true, "VM": true, "XML": true,
	"XMPP": true, "XSRF": true, "XSS": true,

	// India Post / IT 2.0 domain.
	"PPO": true, "HOA": true, "DOP": true, "OTP": true, "PIN": true,
	"SMS": true, "PAN": true, "IFSC": true, "GST": true, "KYC": true,
	"MICR": true, "NEFT": true, "RTGS": true, "UPI": true, "DOB": true,
	"CSI": true, "PLI": true, "RPLI": true, "GDS": true, "NPS": true,
}

// IsInitialism reports whether an upper-cased word is a known initialism.
func IsInitialism(word string) bool { return initialisms[strings.ToUpper(word)] }

// Words splits an identifier into its component words, accepting PascalCase,
// camelCase, snake_case, kebab-case and any mixture of them.
//
// Runs of capitals are treated as one word up to the last capital that starts a
// new lower-cased word, which is what keeps `PPONumber` as ["PPO","Number"]
// rather than ["P","P","O","Number"] or ["PPONumber"].
func Words(s string) []string {
	var (
		out  []string
		cur  []rune
		runs = []rune(s)
	)
	flush := func() {
		if len(cur) > 0 {
			out = append(out, string(cur))
			cur = cur[:0]
		}
	}
	for i, r := range runs {
		switch {
		case r == '_' || r == '-' || r == ' ' || r == '.' || r == '/':
			flush()
			continue
		case unicode.IsDigit(r):
			// A digit continues the current word (Address2, UTF8) unless the
			// word is empty, in which case it starts one.
			cur = append(cur, r)
			continue
		case unicode.IsUpper(r):
			prevLower := i > 0 && (unicode.IsLower(runs[i-1]) || unicode.IsDigit(runs[i-1]))
			nextLower := i+1 < len(runs) && unicode.IsLower(runs[i+1])
			prevUpper := i > 0 && unicode.IsUpper(runs[i-1])
			// Boundary either after a lower-case run (userID -> user|ID) or at
			// the last capital of a run that begins a word (PPONumber -> PPO|Number).
			if prevLower || (prevUpper && nextLower) {
				flush()
			}
			cur = append(cur, r)
		default:
			cur = append(cur, r)
		}
	}
	flush()
	return splitInitialismRuns(out)
}

// splitInitialismRuns breaks an all-capitals word that is a concatenation of
// known initialisms — "HTTPURL" into "HTTP" and "URL".
//
// The scanner above cannot do this on its own: it splits a run of capitals only
// where a lower-case letter follows, so a word made entirely of initialisms
// arrives here whole. Without this pass, Snake("HTTPURL") is "httpurl" and the
// db tag no longer maps back to the field name, which is a silent scan failure
// rather than a compile error.
//
// A word is only split when it decomposes *completely* into known initialisms;
// a partial match ("IDS" as "ID" plus a stray "S") leaves the word alone,
// because guessing is worse than leaving a plausible name intact.
func splitInitialismRuns(words []string) []string {
	var out []string
	for _, w := range words {
		if len(w) <= 3 || w != strings.ToUpper(w) || !isAlpha(w) {
			out = append(out, w)
			continue
		}
		if parts := decompose(w); parts != nil {
			out = append(out, parts...)
			continue
		}
		out = append(out, w)
	}
	return out
}

// decompose returns the initialisms an all-capitals word is composed of, or nil
// when it is not fully decomposable. A single whole-word initialism is not a
// decomposition — there is nothing to split.
func decompose(w string) []string {
	if len(w) > 32 {
		return nil // not a realistic identifier; do not pay for the search
	}
	var walk func(s string) []string
	walk = func(s string) []string {
		if s == "" {
			return []string{}
		}
		// Longest-first, then backtrack, so "UIDAPI" is UID+API rather than
		// stalling on UI.
		for n := len(s); n >= 2; n-- {
			head := s[:n]
			if !initialisms[head] {
				continue
			}
			if rest := walk(s[n:]); rest != nil {
				return append([]string{head}, rest...)
			}
		}
		return nil
	}
	parts := walk(w)
	if len(parts) < 2 {
		return nil
	}
	return parts
}

func isAlpha(s string) bool {
	for _, r := range s {
		if !unicode.IsLetter(r) {
			return false
		}
	}
	return true
}

// Pascal renders an identifier in PascalCase with initialisms upper-cased:
// "ppo_number" and "PpoNumber" both become "PPONumber".
func Pascal(s string) string {
	var b strings.Builder
	for _, w := range Words(s) {
		b.WriteString(capitalise(w))
	}
	return b.String()
}

// Camel renders an identifier in camelCase, with a leading initialism kept
// lower-case in full: "PPONumber" becomes "ppoNumber", not "pPONumber".
//
// This matters because camel-case identifiers become repository parameter
// names, and `pPONumber` is the kind of thing a reviewer notices instead of
// reviewing the query.
func Camel(s string) string {
	words := Words(s)
	if len(words) == 0 {
		return ""
	}
	var b strings.Builder
	b.WriteString(strings.ToLower(words[0]))
	for _, w := range words[1:] {
		b.WriteString(capitalise(w))
	}
	return escapeKeyword(b.String())
}

// capitalise upper-cases a whole word when it is an initialism, and otherwise
// title-cases it.
func capitalise(w string) string {
	if w == "" {
		return ""
	}
	if initialisms[strings.ToUpper(w)] {
		return strings.ToUpper(w)
	}
	r := []rune(strings.ToLower(w))
	r[0] = unicode.ToUpper(r[0])
	return string(r)
}

// Snake renders an identifier in lower_snake_case, keeping initialisms whole:
// "PPONumber" becomes "ppo_number", never "p_p_o_number".
func Snake(s string) string {
	words := Words(s)
	for i, w := range words {
		words[i] = strings.ToLower(w)
	}
	return strings.Join(words, "_")
}

// Kebab renders an identifier in lower-kebab-case, for route segments.
func Kebab(s string) string {
	return strings.ReplaceAll(Snake(s), "_", "-")
}

// Title renders an identifier as space-separated words with each word
// capitalised — the form route names take in Routes(): "Get Pension By ID".
func Title(s string) string {
	words := Words(s)
	for i, w := range words {
		words[i] = capitalise(w)
	}
	return strings.Join(words, " ")
}

// irregularPlurals covers the cases the suffix rules get wrong. Kept
// deliberately short: the spec carries an explicit Plural field and the
// scaffold wizard shows the inferred value for editing, so an exhaustive
// inflection library would be weight for no benefit.
var irregularPlurals = map[string]string{
	"person": "people", "child": "children", "man": "men", "woman": "women",
	"datum": "data", "index": "indexes", "status": "statuses",
	"foot": "feet", "tooth": "teeth", "mouse": "mice", "criterion": "criteria",
}

// uncountable words are their own plural.
var uncountable = map[string]bool{
	"data": true, "info": true, "equipment": true, "staff": true,
	"news": true, "series": true, "species": true,
}

// Plural inflects the last word of an identifier, preserving the case style of
// the input: "Pension" -> "Pensions", "ppo_entry" -> "ppo_entries".
func Plural(s string) string {
	words := Words(s)
	if len(words) == 0 {
		return ""
	}
	last := words[len(words)-1]
	words[len(words)-1] = restoreCase(last, pluraliseWord(strings.ToLower(last)))
	return rejoinLike(s, words)
}

func pluraliseWord(w string) string {
	if w == "" || uncountable[w] {
		return w
	}
	if p, ok := irregularPlurals[w]; ok {
		return p
	}
	switch {
	case strings.HasSuffix(w, "s"), strings.HasSuffix(w, "x"), strings.HasSuffix(w, "z"),
		strings.HasSuffix(w, "ch"), strings.HasSuffix(w, "sh"):
		return w + "es"
	case len(w) > 1 && strings.HasSuffix(w, "y") && !isVowel(w[len(w)-2]):
		return w[:len(w)-1] + "ies"
	default:
		return w + "s"
	}
}

func isVowel(b byte) bool {
	switch b {
	case 'a', 'e', 'i', 'o', 'u':
		return true
	}
	return false
}

// restoreCase re-applies the casing of the original word to its inflected form.
func restoreCase(original, inflected string) string {
	switch {
	case original == "":
		return inflected
	case original == strings.ToUpper(original) && len(original) > 1:
		return strings.ToUpper(inflected)
	case unicode.IsUpper(rune(original[0])):
		r := []rune(inflected)
		r[0] = unicode.ToUpper(r[0])
		return string(r)
	default:
		return inflected
	}
}

// rejoinLike reassembles words using the separator style of the original.
func rejoinLike(original string, words []string) string {
	switch {
	case strings.Contains(original, "_"):
		return strings.Join(words, "_")
	case strings.Contains(original, "-"):
		return strings.Join(words, "-")
	default:
		return strings.Join(words, "")
	}
}

// goKeywords may not be used as identifiers. A field legitimately called
// "type" or "range" in the source system has to become "typeVal" in Go.
var goKeywords = map[string]bool{
	"break": true, "case": true, "chan": true, "const": true, "continue": true,
	"default": true, "defer": true, "else": true, "fallthrough": true, "for": true,
	"func": true, "go": true, "goto": true, "if": true, "import": true,
	"interface": true, "map": true, "package": true, "range": true, "return": true,
	"select": true, "struct": true, "switch": true, "type": true, "var": true,
}

// IsGoKeyword reports whether a string is a reserved Go keyword.
func IsGoKeyword(s string) bool { return goKeywords[s] }

// escapeKeyword makes a camel-case identifier safe to use as a parameter name.
func escapeKeyword(s string) string {
	if goKeywords[s] {
		return s + "Val"
	}
	return s
}
