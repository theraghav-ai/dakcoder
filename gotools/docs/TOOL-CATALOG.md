# Tool catalogue — `gotools`

> **Generated.** Do not edit. Run `make tool-catalog` and commit the result.
> Regenerating is how this file stays true; editing it is how it stops being.

The sidecar's half of contract **C1** (plan.md §7). The gateway routes against
these schemas, the extension renders approvals from them, and the model is sent
them verbatim — so they are re-sent on every turn, which is why the size limits
below are limits and not preferences.

C1 limits: at most **6 parameters** per tool, description at most **200 characters**, written as an instruction to the model rather than documentation for a human.

| Tool | Mutates | Params | Description |
|---|---|---|---|
| [`db_roundtrip_audit`](#db_roundtrip_audit) |  | 1 | Per repository method: database calls, whether any is in a loop, batched, in a transaction, plus a verdict. Worst first. Call before optimising by eye. |
| [`fx_wire`](#fx_wire) | ✓ | 4 | Register a repository or handler in bootstrap/bootstrapper.go with the correct annotation. Never hand-edit it: an unannotated handler serves no routes. |
| [`legacy_audit`](#legacy_audit) |  | 3 | Detect pre-template (api-*) patterns in an existing service: routes.go, gin handlers, manual validation, swaggo docs, handleSuccess helpers. Use when planning a migration, not during ordinary edits. |
| [`lib_version_check`](#lib_version_check) |  | 1 | CEPT library drift: which are behind, which are superseded by the n-api-* generation. Reports only — never edit go.mod on it; tell the user. Call when asked about versions or migration. |
| [`list_rules`](#list_rules) |  | 1 | List the rule ids, severities and citations. Call this before explaining a violation so you quote the real rule id and its source. |
| [`project_scaffold`](#project_scaffold) | ✓ | 4 | Create a new n-api-template service in an empty directory, seeded with one working resource. Greenfield only; to add to an existing service use resource_scaffold. |
| [`repo_map`](#repo_map) |  | 3 | Module path, library generation, package tree with exported symbols, and the FX composition root. Call once to orient; pass `package` for one package in full. |
| [`resource_scaffold`](#resource_scaffold) | ✓ | 3 | Write a whole CRUD resource — domain, DDL, repository, DTOs, handler, FX registration — from a field spec. Use this instead of writing the files yourself. |
| [`rules_lint`](#rules_lint) |  | 3 | Check Go against the n-api-template contract: layer boundaries, handler signature, repository contract, DTO envelopes, FX wiring. Run after each edit batch, passing `paths` with the files you changed. |
| [`temporal_audit`](#temporal_audit) |  | 1 | Inline work that may belong off the request path: uploads, SMS, email, reports, outbound calls. Candidates only, no recommendation. Call when asked about async or Temporal. |
| [`validation_audit`](#validation_audit) |  | 1 | Every request field, its validate tag, and what the tag leaves unbounded. Call when writing or reviewing request DTOs: `required` alone means only 'not empty', so a 10MB string passes. |

A tool marked **Mutates** writes to the workspace and passes through the
approval gate (Part A §7.2). Each of them takes `dry_run`, which returns the
full result without touching the working tree — that is what the gate uses to
show a diff before anything is written.

---

## db_roundtrip_audit

Per repository method: database calls, whether any is in a loop, batched, in a transaction, plus a verdict. Worst first. Call before optimising by eye.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `root` | string |  | workspace root; omit to use the server's default |

<details><summary>Input schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "root": {
      "description": "workspace root; omit to use the server's default",
      "type": "string"
    }
  },
  "type": "object"
}
```

</details>

<details><summary>Output schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "methods": {
      "items": {
        "additionalProperties": false,
        "properties": {
          "batched": {
            "type": "boolean"
          },
          "in_loop": {
            "type": "boolean"
          },
          "line": {
            "type": "integer"
          },
          "method": {
            "type": "string"
          },
          "path": {
            "type": "string"
          },
          "score": {
            "type": "integer"
          },
          "statements": {
            "type": "integer"
          },
          "transaction": {
            "type": "boolean"
          },
          "verdict": {
            "type": "string"
          }
        },
        "required": [
          "path",
          "line",
          "method",
          "statements",
          "in_loop",
          "batched",
          "transaction",
          "verdict",
          "score"
        ],
        "type": "object"
      },
      "type": [
        "null",
        "array"
      ]
    },
    "summary": {
      "type": "string"
    }
  },
  "required": [
    "methods",
    "summary"
  ],
  "type": "object"
}
```

</details>

---

## fx_wire

Register a repository or handler in bootstrap/bootstrapper.go with the correct annotation. Never hand-edit it: an unannotated handler serves no routes.

**Mutates the workspace.** Approval-gated.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `ctor` | string | yes | the constructor's bare name, e.g. NewPensionHandler |
| `kind` | string | yes | repo for a repository constructor, handler for a handler constructor |
| `dry_run` | boolean |  | true to return the patched file without writing it |
| `root` | string |  | workspace root; omit to use the server's default |

<details><summary>Input schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "ctor": {
      "description": "the constructor's bare name, e.g. NewPensionHandler",
      "type": "string"
    },
    "dry_run": {
      "description": "true to return the patched file without writing it",
      "type": "boolean"
    },
    "kind": {
      "description": "repo for a repository constructor, handler for a handler constructor",
      "type": "string"
    },
    "root": {
      "description": "workspace root; omit to use the server's default",
      "type": "string"
    }
  },
  "required": [
    "kind",
    "ctor"
  ],
  "type": "object"
}
```

</details>

<details><summary>Output schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "added": {
      "items": {
        "type": "string"
      },
      "type": [
        "null",
        "array"
      ]
    },
    "already_registered": {
      "description": "constructors that were already wired; this is success, not a failure",
      "items": {
        "type": "string"
      },
      "type": [
        "null",
        "array"
      ]
    },
    "changed": {
      "type": "boolean"
    },
    "content": {
      "description": "the patched file; present only on a dry run",
      "type": "string"
    },
    "ok": {
      "type": "boolean"
    },
    "path": {
      "type": "string"
    },
    "written": {
      "type": "boolean"
    }
  },
  "required": [
    "ok",
    "path",
    "changed",
    "written"
  ],
  "type": "object"
}
```

</details>

---

## legacy_audit

Detect pre-template (api-*) patterns in an existing service: routes.go, gin handlers, manual validation, swaggo docs, handleSuccess helpers. Use when planning a migration, not during ordinary edits.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `only` | null or array |  | subset of rule ids to run; omit to run all |
| `paths` | null or array |  | workspace-relative files you just changed. Pass these so the check is scoped to your edits. Omit ONLY for a full audit the user explicitly asked for |
| `root` | string |  | workspace root; omit to use the server's default |

<details><summary>Input schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "only": {
      "description": "subset of rule ids to run; omit to run all",
      "items": {
        "type": "string"
      },
      "type": [
        "null",
        "array"
      ]
    },
    "paths": {
      "description": "workspace-relative files you just changed. Pass these so the check is scoped to your edits. Omit ONLY for a full audit the user explicitly asked for",
      "items": {
        "type": "string"
      },
      "type": [
        "null",
        "array"
      ]
    },
    "root": {
      "description": "workspace root; omit to use the server's default",
      "type": "string"
    }
  },
  "type": "object"
}
```

</details>

<details><summary>Output schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "count": {
      "description": "number of blocking violations",
      "type": "integer"
    },
    "duration_ms": {
      "type": "integer"
    },
    "files_scanned": {
      "type": "integer"
    },
    "ok": {
      "description": "true when there are no blocking violations in scope",
      "type": "boolean"
    },
    "out_of_scope_count": {
      "description": "pre-existing violations in files you did not touch; these do not block and you should not fix them unless asked",
      "type": "integer"
    },
    "violations": {
      "description": "blocking violations, each with a fix and a citation",
      "items": {
        "additionalProperties": false,
        "properties": {
          "citation": {
            "type": "string"
          },
          "col": {
            "type": "integer"
          },
          "fix": {
            "type": "string"
          },
          "line": {
            "type": "integer"
          },
          "message": {
            "type": "string"
          },
          "path": {
            "type": "string"
          },
          "rule": {
            "type": "string"
          },
          "severity": {
            "type": "string"
          }
        },
        "required": [
          "rule",
          "severity",
          "path",
          "line",
          "message"
        ],
        "type": "object"
      },
      "type": [
        "null",
        "array"
      ]
    },
    "warnings": {
      "description": "non-blocking advice",
      "items": {
        "additionalProperties": false,
        "properties": {
          "citation": {
            "type": "string"
          },
          "col": {
            "type": "integer"
          },
          "fix": {
            "type": "string"
          },
          "line": {
            "type": "integer"
          },
          "message": {
            "type": "string"
          },
          "path": {
            "type": "string"
          },
          "rule": {
            "type": "string"
          },
          "severity": {
            "type": "string"
          }
        },
        "required": [
          "rule",
          "severity",
          "path",
          "line",
          "message"
        ],
        "type": "object"
      },
      "type": [
        "null",
        "array"
      ]
    }
  },
  "required": [
    "ok",
    "count",
    "violations",
    "out_of_scope_count",
    "files_scanned",
    "duration_ms"
  ],
  "type": "object"
}
```

</details>

---

## lib_version_check

CEPT library drift: which are behind, which are superseded by the n-api-* generation. Reports only — never edit go.mod on it; tell the user. Call when asked about versions or migration.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `root` | string |  | workspace root; omit to use the server's default |

<details><summary>Input schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "root": {
      "description": "workspace root; omit to use the server's default",
      "type": "string"
    }
  },
  "type": "object"
}
```

</details>

<details><summary>Output schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "note": {
      "type": "string"
    },
    "result": {
      "additionalProperties": false,
      "properties": {
        "module": {
          "type": "string"
        },
        "registry_error": {
          "type": "string"
        },
        "registry_reachable": {
          "type": "boolean"
        },
        "reports": {
          "items": {
            "additionalProperties": false,
            "properties": {
              "behind": {
                "type": "integer"
              },
              "current": {
                "type": "string"
              },
              "latest": {
                "type": "string"
              },
              "module": {
                "type": "string"
              },
              "note": {
                "type": "string"
              },
              "status": {
                "type": "string"
              },
              "superseded_by": {
                "type": "string"
              }
            },
            "required": [
              "module",
              "current",
              "status"
            ],
            "type": "object"
          },
          "type": [
            "null",
            "array"
          ]
        }
      },
      "required": [
        "module",
        "reports",
        "registry_reachable"
      ],
      "type": [
        "null",
        "object"
      ]
    },
    "summary": {
      "type": "string"
    }
  },
  "required": [
    "result",
    "summary",
    "note"
  ],
  "type": "object"
}
```

</details>

---

## list_rules

List the rule ids, severities and citations. Call this before explaining a violation so you quote the real rule id and its source.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `legacy` | boolean |  | true to list legacy-detection rules instead of compliance rules |

<details><summary>Input schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "legacy": {
      "description": "true to list legacy-detection rules instead of compliance rules",
      "type": "boolean"
    }
  },
  "type": "object"
}
```

</details>

<details><summary>Output schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "rules": {
      "items": {
        "additionalProperties": false,
        "properties": {
          "citation": {
            "type": "string"
          },
          "id": {
            "type": "string"
          },
          "severity": {
            "type": "string"
          },
          "summary": {
            "type": "string"
          }
        },
        "required": [
          "id",
          "severity",
          "summary"
        ],
        "type": "object"
      },
      "type": [
        "null",
        "array"
      ]
    }
  },
  "required": [
    "rules"
  ],
  "type": "object"
}
```

</details>

---

## project_scaffold

Create a new n-api-template service in an empty directory, seeded with one working resource. Greenfield only; to add to an existing service use resource_scaffold.

**Mutates the workspace.** Approval-gated.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `project` | object | yes | the service to create |
| `resource` | object | yes | one working resource to seed the service with |
| `dry_run` | boolean |  | true to return the files without writing them |
| `root` | string |  | target directory; omit to use the server's default |

<details><summary>Input schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "dry_run": {
      "description": "true to return the files without writing them",
      "type": "boolean"
    },
    "project": {
      "additionalProperties": false,
      "description": "the service to create",
      "properties": {
        "addr": {
          "description": "listen address, e.g. :8080",
          "type": "string"
        },
        "app_name": {
          "description": "short service name; derived from the module when omitted",
          "type": "string"
        },
        "description": {
          "description": "one-line description for the swagger document",
          "type": "string"
        },
        "go_version": {
          "description": "go directive, e.g. 1.25.0",
          "type": "string"
        },
        "module": {
          "description": "go module path, e.g. gitlab.cept.gov.in/it-2.0/pension-api",
          "type": "string"
        },
        "title": {
          "description": "human title for the swagger document",
          "type": "string"
        }
      },
      "required": [
        "module"
      ],
      "type": "object"
    },
    "resource": {
      "additionalProperties": false,
      "description": "one working resource to seed the service with",
      "properties": {
        "fields": {
          "description": "the resource's own columns; do NOT include id, created_at or updated_at",
          "items": {
            "additionalProperties": false,
            "properties": {
              "db": {
                "description": "db column; inferred as snake_case when omitted",
                "type": "string"
              },
              "go": {
                "description": "Go field name in PascalCase, e.g. PPONumber",
                "type": "string"
              },
              "json": {
                "description": "json tag; inferred as snake_case when omitted",
                "type": "string"
              },
              "sql": {
                "description": "postgres column type; inferred from the Go type when omitted",
                "type": "string"
              },
              "type": {
                "description": "one of string, int, int64, float64, bool, time.Time",
                "type": "string"
              },
              "validate": {
                "description": "go-playground validate tag, e.g. required or oneof=active closed",
                "type": "string"
              }
            },
            "required": [
              "go",
              "type"
            ],
            "type": "object"
          },
          "type": [
            "null",
            "array"
          ]
        },
        "list_filters": {
          "description": "optional query-string filters on the list route; each must name a declared field",
          "items": {
            "additionalProperties": false,
            "properties": {
              "form": {
                "description": "query-string parameter name; inferred as snake_case when omitted",
                "type": "string"
              },
              "go": {
                "description": "the declared field to filter on",
                "type": "string"
              }
            },
            "required": [
              "go"
            ],
            "type": "object"
          },
          "type": [
            "null",
            "array"
          ]
        },
        "name": {
          "description": "singular resource name in PascalCase, e.g. Pension",
          "type": "string"
        },
        "operations": {
          "description": "subset of create, list, get, update, delete; omit for all five",
          "items": {
            "type": "string"
          },
          "type": [
            "null",
            "array"
          ]
        },
        "paginate": {
          "description": "true to accept skip/limit on the list route",
          "type": "boolean"
        },
        "plural": {
          "description": "plural form; inferred from name when omitted",
          "type": "string"
        },
        "route_base": {
          "description": "route base under /v1, e.g. /pensions; inferred when omitted",
          "type": "string"
        },
        "table": {
          "description": "postgres table name; inferred from the plural when omitted",
          "type": "string"
        }
      },
      "required": [
        "name",
        "fields"
      ],
      "type": "object"
    },
    "root": {
      "description": "target directory; omit to use the server's default",
      "type": "string"
    }
  },
  "required": [
    "project",
    "resource"
  ],
  "type": "object"
}
```

</details>

<details><summary>Output schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "files": {
      "items": {
        "additionalProperties": false,
        "properties": {
          "action": {
            "description": "create or modify",
            "type": "string"
          },
          "bytes": {
            "type": "integer"
          },
          "content": {
            "description": "the file's full content; present only on a dry run",
            "type": "string"
          },
          "path": {
            "type": "string"
          }
        },
        "required": [
          "path",
          "action",
          "bytes"
        ],
        "type": "object"
      },
      "type": [
        "null",
        "array"
      ]
    },
    "module": {
      "type": "string"
    },
    "notes": {
      "description": "steps the scaffolder deliberately left for a human; relay these",
      "items": {
        "type": "string"
      },
      "type": [
        "null",
        "array"
      ]
    },
    "ok": {
      "type": "boolean"
    },
    "written": {
      "description": "false on a dry run, when nothing was written",
      "type": "boolean"
    }
  },
  "required": [
    "ok",
    "written",
    "files"
  ],
  "type": "object"
}
```

</details>

---

## repo_map

Module path, library generation, package tree with exported symbols, and the FX composition root. Call once to orient; pass `package` for one package in full.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `max_tokens` | integer |  | size cap; omit for the default of 4000 |
| `package` | string |  | a package directory such as repo/postgres, for that package in full detail |
| `root` | string |  | workspace root; omit to use the server's default |

<details><summary>Input schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "max_tokens": {
      "description": "size cap; omit for the default of 4000",
      "type": "integer"
    },
    "package": {
      "description": "a package directory such as repo/postgres, for that package in full detail",
      "type": "string"
    },
    "root": {
      "description": "workspace root; omit to use the server's default",
      "type": "string"
    }
  },
  "type": "object"
}
```

</details>

<details><summary>Output schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "duration_ms": {
      "type": "integer"
    },
    "elided": {
      "additionalProperties": false,
      "properties": {
        "dropped": {
          "description": "packages omitted entirely",
          "type": "integer"
        },
        "hint": {
          "type": "string"
        },
        "summarised": {
          "description": "packages whose symbol lists were dropped",
          "type": "integer"
        },
        "truncated": {
          "description": "packages whose symbol lists were shortened; see more_types and more_funcs",
          "type": "integer"
        }
      },
      "required": [
        "hint"
      ],
      "type": [
        "null",
        "object"
      ]
    },
    "est_tokens": {
      "type": "integer"
    },
    "files": {
      "type": "integer"
    },
    "fx": {
      "additionalProperties": false,
      "properties": {
        "handlers": {
          "items": {
            "type": "string"
          },
          "type": [
            "null",
            "array"
          ]
        },
        "misregistered": {
          "description": "handlers registered without their group tag; they start but serve no routes",
          "items": {
            "type": "string"
          },
          "type": [
            "null",
            "array"
          ]
        },
        "repos": {
          "items": {
            "type": "string"
          },
          "type": [
            "null",
            "array"
          ]
        },
        "unwired": {
          "description": "constructors declared but absent from bootstrap/; these fail at startup",
          "items": {
            "type": "string"
          },
          "type": [
            "null",
            "array"
          ]
        }
      },
      "type": [
        "null",
        "object"
      ]
    },
    "generation": {
      "description": "n-api for the current template generation, api for the legacy one",
      "type": "string"
    },
    "go_version": {
      "type": "string"
    },
    "module": {
      "type": "string"
    },
    "packages": {
      "items": {
        "additionalProperties": false,
        "properties": {
          "dir": {
            "type": "string"
          },
          "files": {
            "type": "integer"
          },
          "funcs": {
            "description": "exported functions; methods appear as (*Type).Method",
            "items": {
              "type": "string"
            },
            "type": [
              "null",
              "array"
            ]
          },
          "layer": {
            "type": "string"
          },
          "more_funcs": {
            "type": "integer"
          },
          "more_types": {
            "type": "integer"
          },
          "name": {
            "type": "string"
          },
          "summarised": {
            "type": "boolean"
          },
          "types": {
            "items": {
              "type": "string"
            },
            "type": [
              "null",
              "array"
            ]
          }
        },
        "required": [
          "dir",
          "name",
          "files"
        ],
        "type": "object"
      },
      "type": [
        "null",
        "array"
      ]
    },
    "requires": {
      "description": "direct dependencies as path@version",
      "items": {
        "type": "string"
      },
      "type": [
        "null",
        "array"
      ]
    }
  },
  "required": [
    "packages",
    "files",
    "est_tokens",
    "duration_ms"
  ],
  "type": "object"
}
```

</details>

---

## resource_scaffold

Write a whole CRUD resource — domain, DDL, repository, DTOs, handler, FX registration — from a field spec. Use this instead of writing the files yourself.

**Mutates the workspace.** Approval-gated.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `spec` | object | yes | the resource to scaffold |
| `dry_run` | boolean |  | true to return the files without writing them |
| `root` | string |  | workspace root; omit to use the server's default |

<details><summary>Input schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "dry_run": {
      "description": "true to return the files without writing them",
      "type": "boolean"
    },
    "root": {
      "description": "workspace root; omit to use the server's default",
      "type": "string"
    },
    "spec": {
      "additionalProperties": false,
      "description": "the resource to scaffold",
      "properties": {
        "fields": {
          "description": "the resource's own columns; do NOT include id, created_at or updated_at",
          "items": {
            "additionalProperties": false,
            "properties": {
              "db": {
                "description": "db column; inferred as snake_case when omitted",
                "type": "string"
              },
              "go": {
                "description": "Go field name in PascalCase, e.g. PPONumber",
                "type": "string"
              },
              "json": {
                "description": "json tag; inferred as snake_case when omitted",
                "type": "string"
              },
              "sql": {
                "description": "postgres column type; inferred from the Go type when omitted",
                "type": "string"
              },
              "type": {
                "description": "one of string, int, int64, float64, bool, time.Time",
                "type": "string"
              },
              "validate": {
                "description": "go-playground validate tag, e.g. required or oneof=active closed",
                "type": "string"
              }
            },
            "required": [
              "go",
              "type"
            ],
            "type": "object"
          },
          "type": [
            "null",
            "array"
          ]
        },
        "list_filters": {
          "description": "optional query-string filters on the list route; each must name a declared field",
          "items": {
            "additionalProperties": false,
            "properties": {
              "form": {
                "description": "query-string parameter name; inferred as snake_case when omitted",
                "type": "string"
              },
              "go": {
                "description": "the declared field to filter on",
                "type": "string"
              }
            },
            "required": [
              "go"
            ],
            "type": "object"
          },
          "type": [
            "null",
            "array"
          ]
        },
        "name": {
          "description": "singular resource name in PascalCase, e.g. Pension",
          "type": "string"
        },
        "operations": {
          "description": "subset of create, list, get, update, delete; omit for all five",
          "items": {
            "type": "string"
          },
          "type": [
            "null",
            "array"
          ]
        },
        "paginate": {
          "description": "true to accept skip/limit on the list route",
          "type": "boolean"
        },
        "plural": {
          "description": "plural form; inferred from name when omitted",
          "type": "string"
        },
        "route_base": {
          "description": "route base under /v1, e.g. /pensions; inferred when omitted",
          "type": "string"
        },
        "table": {
          "description": "postgres table name; inferred from the plural when omitted",
          "type": "string"
        }
      },
      "required": [
        "name",
        "fields"
      ],
      "type": "object"
    }
  },
  "required": [
    "spec"
  ],
  "type": "object"
}
```

</details>

<details><summary>Output schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "files": {
      "items": {
        "additionalProperties": false,
        "properties": {
          "action": {
            "description": "create or modify",
            "type": "string"
          },
          "bytes": {
            "type": "integer"
          },
          "content": {
            "description": "the file's full content; present only on a dry run",
            "type": "string"
          },
          "path": {
            "type": "string"
          }
        },
        "required": [
          "path",
          "action",
          "bytes"
        ],
        "type": "object"
      },
      "type": [
        "null",
        "array"
      ]
    },
    "module": {
      "type": "string"
    },
    "notes": {
      "description": "steps the scaffolder deliberately left for a human; relay these",
      "items": {
        "type": "string"
      },
      "type": [
        "null",
        "array"
      ]
    },
    "ok": {
      "type": "boolean"
    },
    "written": {
      "description": "false on a dry run, when nothing was written",
      "type": "boolean"
    }
  },
  "required": [
    "ok",
    "written",
    "files"
  ],
  "type": "object"
}
```

</details>

---

## rules_lint

Check Go against the n-api-template contract: layer boundaries, handler signature, repository contract, DTO envelopes, FX wiring. Run after each edit batch, passing `paths` with the files you changed.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `only` | null or array |  | subset of rule ids to run; omit to run all |
| `paths` | null or array |  | workspace-relative files you just changed. Pass these so the check is scoped to your edits. Omit ONLY for a full audit the user explicitly asked for |
| `root` | string |  | workspace root; omit to use the server's default |

<details><summary>Input schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "only": {
      "description": "subset of rule ids to run; omit to run all",
      "items": {
        "type": "string"
      },
      "type": [
        "null",
        "array"
      ]
    },
    "paths": {
      "description": "workspace-relative files you just changed. Pass these so the check is scoped to your edits. Omit ONLY for a full audit the user explicitly asked for",
      "items": {
        "type": "string"
      },
      "type": [
        "null",
        "array"
      ]
    },
    "root": {
      "description": "workspace root; omit to use the server's default",
      "type": "string"
    }
  },
  "type": "object"
}
```

</details>

<details><summary>Output schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "count": {
      "description": "number of blocking violations",
      "type": "integer"
    },
    "duration_ms": {
      "type": "integer"
    },
    "files_scanned": {
      "type": "integer"
    },
    "ok": {
      "description": "true when there are no blocking violations in scope",
      "type": "boolean"
    },
    "out_of_scope_count": {
      "description": "pre-existing violations in files you did not touch; these do not block and you should not fix them unless asked",
      "type": "integer"
    },
    "violations": {
      "description": "blocking violations, each with a fix and a citation",
      "items": {
        "additionalProperties": false,
        "properties": {
          "citation": {
            "type": "string"
          },
          "col": {
            "type": "integer"
          },
          "fix": {
            "type": "string"
          },
          "line": {
            "type": "integer"
          },
          "message": {
            "type": "string"
          },
          "path": {
            "type": "string"
          },
          "rule": {
            "type": "string"
          },
          "severity": {
            "type": "string"
          }
        },
        "required": [
          "rule",
          "severity",
          "path",
          "line",
          "message"
        ],
        "type": "object"
      },
      "type": [
        "null",
        "array"
      ]
    },
    "warnings": {
      "description": "non-blocking advice",
      "items": {
        "additionalProperties": false,
        "properties": {
          "citation": {
            "type": "string"
          },
          "col": {
            "type": "integer"
          },
          "fix": {
            "type": "string"
          },
          "line": {
            "type": "integer"
          },
          "message": {
            "type": "string"
          },
          "path": {
            "type": "string"
          },
          "rule": {
            "type": "string"
          },
          "severity": {
            "type": "string"
          }
        },
        "required": [
          "rule",
          "severity",
          "path",
          "line",
          "message"
        ],
        "type": "object"
      },
      "type": [
        "null",
        "array"
      ]
    }
  },
  "required": [
    "ok",
    "count",
    "violations",
    "out_of_scope_count",
    "files_scanned",
    "duration_ms"
  ],
  "type": "object"
}
```

</details>

---

## temporal_audit

Inline work that may belong off the request path: uploads, SMS, email, reports, outbound calls. Candidates only, no recommendation. Call when asked about async or Temporal.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `root` | string |  | workspace root; omit to use the server's default |

<details><summary>Input schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "root": {
      "description": "workspace root; omit to use the server's default",
      "type": "string"
    }
  },
  "type": "object"
}
```

</details>

<details><summary>Output schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "candidates": {
      "items": {
        "additionalProperties": false,
        "properties": {
          "call": {
            "type": "string"
          },
          "func": {
            "type": "string"
          },
          "kind": {
            "type": "string"
          },
          "line": {
            "type": "integer"
          },
          "path": {
            "type": "string"
          }
        },
        "required": [
          "path",
          "line",
          "func",
          "kind",
          "call"
        ],
        "type": "object"
      },
      "type": [
        "null",
        "array"
      ]
    },
    "note": {
      "type": "string"
    },
    "summary": {
      "type": "string"
    }
  },
  "required": [
    "candidates",
    "summary",
    "note"
  ],
  "type": "object"
}
```

</details>

---

## validation_audit

Every request field, its validate tag, and what the tag leaves unbounded. Call when writing or reviewing request DTOs: `required` alone means only 'not empty', so a 10MB string passes.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `root` | string |  | workspace root; omit to use the server's default |

<details><summary>Input schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "root": {
      "description": "workspace root; omit to use the server's default",
      "type": "string"
    }
  },
  "type": "object"
}
```

</details>

<details><summary>Output schema</summary>

```json
{
  "additionalProperties": false,
  "properties": {
    "fields": {
      "items": {
        "additionalProperties": false,
        "properties": {
          "field": {
            "type": "string"
          },
          "line": {
            "type": "integer"
          },
          "missing": {
            "type": "string"
          },
          "path": {
            "type": "string"
          },
          "struct": {
            "type": "string"
          },
          "tag": {
            "type": "string"
          },
          "type": {
            "type": "string"
          }
        },
        "required": [
          "path",
          "line",
          "struct",
          "field",
          "type",
          "tag"
        ],
        "type": "object"
      },
      "type": [
        "null",
        "array"
      ]
    },
    "summary": {
      "type": "string"
    }
  },
  "required": [
    "fields",
    "summary"
  ],
  "type": "object"
}
```

</details>
