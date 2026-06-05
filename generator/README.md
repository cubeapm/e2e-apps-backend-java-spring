# Unified CubeAPM data generator

One Docker stack, driven by **one config file** (`config.yaml`), that fills a
CubeAPM instance with **continuous, multi-tenant, multi-vendor** telemetry.

Where the per-vendor branches (`otel`, `datadog`, `newrelic`, `elastic`) each run
*one* app, *one* vendor, *one* org and need you to curl endpoints by hand, this
runs them **all together** and generates traffic on its own — each workload
carrying its own **service name** and **`x-cube-org-slug`** (tenant).

```
                          ┌────────────────────────────────────────────┐
 config.yaml  ──generate──▶ docker-compose.generated.yml + nginx + json │
                          └────────────────────────────────────────────┘
                                            │ docker compose up
        ┌───────────────────────────────────┼───────────────────────────────────┐
        ▼                                    ▼                                   ▼
  real Spring apps (1 JVM each)        tenant-gateway (nginx)         synthetic (telemetrygen)
  otel / datadog / elastic / nr        injects x-cube-org-slug         real OTLP protobuf,
        │  ▲                            per org for DD/Elastic/NR       sets the header itself
        │  └── loadgen (curl loop, continuous traffic) ──┘                       │
        ▼                                    ▼                                   ▼
                                  ┌──────────────────────┐
                                  │       CubeAPM        │  (org routing via x-cube-org-slug)
                                  └──────────────────────┘
```

## Prerequisites

1. **Docker** (Desktop on macOS). Nothing else — the generator itself runs in a container.
2. A running **CubeAPM** reachable at `host.docker.internal` on the ingest ports:
   `4318` (OTLP HTTP) **or** `4317` (OTLP gRPC) for traces, and `3130` for
   metrics/logs + Datadog/Elastic/New Relic intake. Adjust under `config.yaml › cubeapm`.
3. **Pre-create the org slugs** you use in `config.yaml` (`default`, `ola-money`,
   `ola-electric` by default; `default` always exists) in CubeAPM. An **unknown
   slug silently falls back to the default org** — so if everything shows up under
   "default", your slugs don't exist yet.

## Usage

```bash
cd generator
make up        # generate + build + start everything (detached)
make logs      # follow logs
make ps        # container status
make down      # stop (keeps mysql data);  make clean = also drop the volume
```

Edit `config.yaml`, then `make up` again (it regenerates first). The gateway
carries a config hash, so changing slugs recreates it automatically; the real
Datadog/Elastic/New Relic app containers don't need recreating because their slug
lives in the gateway, not their env.

## The config

| Key | Meaning |
|-----|---------|
| `cubeapm.host` | where CubeAPM is reachable from containers (`host.docker.internal`) |
| `cubeapm.traces_transport` | `http` → `otlp_http_port` (default) or `grpc` → `otlp_grpc_port` |
| `cubeapm.otlp_http_port` / `otlp_grpc_port` | OTLP HTTP `4318` / gRPC `4317` (traces) |
| `cubeapm.ingest_port` | `3130` — OTEL metrics/logs, Datadog, Elastic, synthetic logs |
| `cubeapm.newrelic.enabled` | `false` skips all New Relic services |
| `cubeapm.newrelic.upstream_port` | CubeAPM port that accepts New Relic intake (default `3130`) |
| `cubeapm.newrelic.license_key` | placeholder license key the NR agent needs to start |
| `defaults.environment / service_version` | stamped as resource attributes |
| `defaults.traffic.rps / error_ratio` | per-service load (overridable per service) |
| `services[]` | real app containers: `name`, `vendor`, `org_slug`, optional `traffic` |
| `synthetic[]` | telemetrygen streams: `service_name`, `org_slug`, `rps`, `signals` (`traces`,`logs`) |

`vendor` is one of `otel`, `datadog`, `elastic`, `newrelic`. "N services × M orgs"
= N×M app containers (one JVM each), so keep the real `services[]` list modest and
lean on `synthetic[]` for breadth.

### Traces transport

CubeAPM reads `x-cube-org-slug` from **both** OTLP HTTP headers and gRPC metadata,
so multi-tenancy works either way. Default is **OTLP HTTP on 4318**; flip
`traces_transport: grpc` if 4318 is ever occupied by another process (CubeAPM also
serves OTLP gRPC on 4317). Metrics and logs always go OTLP/HTTP to `3130`.

## How multi-tenancy works per vendor

The tenant header is **`x-cube-org-slug`** (honored uniformly by CubeAPM across
OTLP/Datadog/New Relic/Elastic). Only some agents can set it themselves; the rest
go through the **tenant-gateway**:

| Vendor | How the slug is attached | Multi-org |
|--------|--------------------------|-----------|
| **OTEL** | native `OTEL_EXPORTER_OTLP_HEADERS` (+ gRPC metadata) — direct to CubeAPM | ✅ direct |
| **Synthetic** | `telemetrygen --otlp-header` — direct to CubeAPM | ✅ direct |
| **Datadog** | tracer → **tenant-gateway** (nginx adds header) → CubeAPM `:3130` | ✅ via gateway |
| **Elastic** | agent → **tenant-gateway** → CubeAPM `:3130` | ✅ via gateway |
| **New Relic** | agent (TLS) → **tenant-gateway** (TLS listener) → CubeAPM | ✅ via gateway |

The **tenant-gateway** is a tiny nginx with one listener per org; each listener
injects that org's `x-cube-org-slug` and forwards to CubeAPM. It exists only
because the Datadog/Elastic/New Relic agents can't add a per-request header. It
resolves `host.docker.internal` IPv4-only (`resolver 127.0.0.11 ipv6=off`) — the
dual-stack `/etc/hosts` entry otherwise causes `connect()` retries.

### New Relic

New Relic's Java agent always speaks **TLS** and offers no custom-header knob, so
its traffic goes through the gateway's per-org **TLS** listener (self-signed cert,
generated by `make certs`; the agent trusts it via `NEW_RELIC_CA_BUNDLE_PATH`).
Two extra pieces make it work end to end:

- **Collector redirect** — CubeAPM's NR collector returns a `redirect_host` echoed
  from the upstream `Host` header, so the gateway forces `Host: tenant-gateway`;
  otherwise the agent gets redirected to `host.docker.internal` and leaves the
  gateway (losing the slug).
- **Environment / version** — the NR agent forwards every `NEW_RELIC_METADATA_*`
  env var; CubeAPM maps keys prefixed `NEW_RELIC_METADATA_CUBE_ATTRS_` into resource
  attributes (`_P_` → `.`), so we set `cube.environment` and `service.version` there
  (otherwise NR shows `env=UNSET`).

If NR misbehaves in your environment, check `cubeapm.newrelic.upstream_port`, or set
`cubeapm.newrelic.enabled: false` to drop it — the other three vendors are unaffected.

### Datadog

Datadog runs **agentless** (traces only) via `DD_TRACE_AGENT_URL` → gateway. To add
Datadog logs/metrics you'd reintroduce a `datadog-agent` sidecar per org (see the
`datadog` branch) — out of scope here.

## What `make up` generates (git-ignored)

- `docker-compose.generated.yml` — the full stack (infra + apps + gateway + loadgen + telemetrygen)
- `nginx/nginx.conf` — the per-org gateway
- `loadgen/targets.json` — load generator targets
- `certs/` — self-signed gateway cert (for the New Relic TLS listener)

## Verify

1. CubeAPM up; org slugs pre-created.
2. `make up`, wait ~1–2 min for the JVMs to boot and the loadgen 45s warmup.
3. In the CubeAPM UI, switch orgs and confirm each shows only its own services,
   e.g. with the default config: `default` → checkout-otel, payments-dd,
   recommendations; `ola-money` → inventory-elastic, shipping-nr; `ola-electric`
   → search-otel, notifications — with traces (incl. DB/cache/error spans),
   metrics and logs flowing continuously, and `env` matching `defaults.environment`.
4. Negative check: set a service's `org_slug` to something not in CubeAPM →
   it appears under **default**, proving the pre-create requirement.
