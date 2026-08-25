---
slug: config-keys
handle: "@skill:config-keys"
fetch_when: "reading a config value, or adding a key — every key, and which environments declare it"
generated_from: config-keys
---

# Configuration keys

> **Generated.** Do not edit — run `make knowledge` and commit the result.
> Assembled from the reference template, so it cannot drift from what the linter enforces.

Every key declared across `configs/*.yaml`, and which environment files declare it.

A key the code reads but a config does not declare returns the **zero value**: a repository asking for an absent `db.QueryTimeoutLow` gets a 0s deadline, and every query it wraps fails immediately with `context deadline exceeded` — an error naming the context, not the config. A key present in the base file and missing from `config.prod.yaml` is how a service works in dev and dies in production.

Key lookups fold case, so `db.QueryTimeoutLow` and `db.querytimeoutlow` address the same value. The segments still have to be right.

Enforced by `config-key-exists` and `swagger-visible`. Generated from the reference template, so it cannot describe a key that is not there.

## Keys

79 scalar keys across 7 environment file(s). A ✓ means the environment declares the key; a blank means a lookup there returns the zero value.

| Key | dev | prod | sit | staging | test | training | base |
|---|---|---|---|---|---|---|---|
| `AADHAAR_CLIENT_ID` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `AADHAAR_CLIENT_SECRET` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `AADHAAR_OTP_AUTHENTICATION_URL` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `AADHAAR_OTP_GEN_URL` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `appname` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `assignChargeAPIURL` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `cache.islocalcacheenabled` | ✓ | | ✓ | ✓ | ✓ | ✓ | ✓ |
| `cache.isredisenabled` | ✓ | | ✓ | ✓ | ✓ | ✓ | ✓ |
| `cache.lcbatchbuffertimeout` | ✓ | | ✓ | ✓ | ✓ | ✓ | ✓ |
| `cache.lcbatchsize` | ✓ | | ✓ | ✓ | ✓ | ✓ | ✓ |
| `cache.lccapacity` | ✓ | | ✓ | ✓ | ✓ | ✓ | ✓ |
| `cache.lcevictionpercentage` | ✓ | | ✓ | ✓ | ✓ | ✓ | ✓ |
| `cache.lcmaxrefreshdelay` | ✓ | | ✓ | ✓ | ✓ | ✓ | ✓ |
| `cache.lcminrefreshdelay` | ✓ | | ✓ | ✓ | ✓ | ✓ | ✓ |
| `cache.lcnumshards` | ✓ | | ✓ | ✓ | ✓ | ✓ | ✓ |
| `cache.lcretrybasedelay` | ✓ | | ✓ | ✓ | ✓ | ✓ | ✓ |
| `cache.lcttl` | ✓ | | ✓ | ✓ | ✓ | ✓ | ✓ |
| `cache.redisdbindex` | ✓ | | ✓ | ✓ | ✓ | ✓ | ✓ |
| `cache.redisexpirationtime` | ✓ | | ✓ | ✓ | ✓ | ✓ | ✓ |
| `cache.redispassword` | ✓ | | ✓ | ✓ | ✓ | ✓ | ✓ |
| `cache.redisserver` | ✓ | | ✓ | ✓ | ✓ | ✓ | ✓ |
| `client.baseurl` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `client.smsurl` | | | | | | | ✓ |
| `db.database` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `db.healthcheckperiod` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `db.host` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `db.maxconnidletime` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `db.maxconnlifetime` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `db.maxconns` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `db.minconns` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `db.password` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `db.port` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `db.QueryTimeoutLow` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `db.QueryTimeoutMed` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `db.schema` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `db.username` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `info.description` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `info.email` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `info.name` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `info.terms` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `info.title` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `info.version` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `keycloakURL` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `LeaveSubstituteurl` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `log.format` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `log.level` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `log.output` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `metrics.buckets` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `metrics.collect.build` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `metrics.collect.go` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `metrics.collect.process` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `metrics.collect.routes` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `metrics.expose` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `minio.accessKey` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `minio.bucketName` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `minio.secretKey` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `minio.url` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `PayrollURL` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `RoleMngmtURL` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `server.addr` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `server.bodylimit` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `server.dashboard.Enabled` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `server.debug.pprof.expose` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `server.debug.pprof.path` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `server.debug.stats.expose` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `server.debug.stats.path` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `server.healthcheck.expose` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `server.healthcheck.path` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `server.readbuffersize` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `server.readtimeout` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `server.timeout` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `server.writetimeout` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `SmsURL` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `swagger.generation.mode` | | | | | | | ✓ |
| `trace.enabled` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `trace.processor.options.host` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `trace.processor.type` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `trace.sampler.options.ratio` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `trace.sampler.type` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

## Gaps

Keys declared in some environments and not others. Reading one of these in an environment that does not declare it returns the zero value, silently:

- `cache.islocalcacheenabled` is missing from prod
- `cache.isredisenabled` is missing from prod
- `cache.lcbatchbuffertimeout` is missing from prod
- `cache.lcbatchsize` is missing from prod
- `cache.lccapacity` is missing from prod
- `cache.lcevictionpercentage` is missing from prod
- `cache.lcmaxrefreshdelay` is missing from prod
- `cache.lcminrefreshdelay` is missing from prod
- `cache.lcnumshards` is missing from prod
- `cache.lcretrybasedelay` is missing from prod
- `cache.lcttl` is missing from prod
- `cache.redisdbindex` is missing from prod
- `cache.redisexpirationtime` is missing from prod
- `cache.redispassword` is missing from prod
- `cache.redisserver` is missing from prod
- `client.smsurl` is missing from dev, prod, sit, staging, test, training
- `swagger.generation.mode` is missing from dev, prod, sit, staging, test, training
