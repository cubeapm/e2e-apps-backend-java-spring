#!/usr/bin/env python3
"""Generate the unified data-generator stack from config.yaml.

Reads config.yaml and emits, all in the generator/ directory:
  - docker-compose.generated.yml   the whole stack (infra + apps + gateway + loadgen + synthetic)
  - nginx/nginx.conf               per-org tenant gateway (injects x-cube-org-slug)
  - loadgen/targets.json           what the load generator hits, and how fast
  - synthetic/streams.json         the synthetic OTLP streams

Run via `make generate` (which runs this inside a python container so you don't
need PyYAML on the host).
"""
import hashlib
import json
import os
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required. Run via `make generate`, or `pip install pyyaml`.")

HERE = os.path.dirname(os.path.abspath(__file__))
APP_IMAGE = "cube_unified_app:latest"
TELEMETRYGEN_IMAGE = "ghcr.io/open-telemetry/opentelemetry-collector-contrib/telemetrygen:latest"
GW_HTTP_BASE = 18080   # one HTTP listener per org that has datadog/elastic services
GW_TLS_BASE = 18443    # one TLS  listener per org that has newrelic services
MYSQL_HOST = "cube_java_springboot_mysql"   # hard-coded in application.properties
REDIS_HOST = "cube_java_springboot_redis"


def load_config():
    with open(os.path.join(HERE, "config.yaml")) as fh:
        return yaml.safe_load(fh)


# ---------------------------------------------------------------- vendor env ---
def otel_env(name, slug, env, ver, cube):
    host, ingest = cube["host"], cube["ingest_port"]
    transport = cube.get("traces_transport", "grpc")
    logs_headers = f"x-cube-org-slug={slug},Cube-Stream-Fields=service.name%2Cseverity"
    env_list = [
        "JAVA_TOOL_OPTIONS=-javaagent:/java/opentelemetry-javaagent.jar",
        f"OTEL_SERVICE_NAME={name}",
        "OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf",     # metrics/logs transport
        "OTEL_EXPORTER_OTLP_COMPRESSION=gzip",
        "OTEL_INSTRUMENTATION_RUNTIME_TELEMETRY_JAVA17_ENABLE_ALL=true",
        f"OTEL_RESOURCE_ATTRIBUTES=cube.environment={env},service.version={ver}",
        f"OTEL_EXPORTER_OTLP_HEADERS=x-cube-org-slug={slug}",
        "OTEL_TRACES_EXPORTER=otlp",
        "OTEL_METRICS_EXPORTER=otlp",
        f"OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=http://{host}:{ingest}/api/metrics/v1/save/otlp",
        "OTEL_LOGS_EXPORTER=otlp",
        f"OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://{host}:{ingest}/api/logs/insert/opentelemetry/v1/logs",
        f"OTEL_EXPORTER_OTLP_LOGS_HEADERS={logs_headers}",
    ]
    if transport == "grpc":
        port = cube.get("otlp_grpc_port", 4317)
        env_list += [
            "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=grpc",
            f"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://{host}:{port}",
        ]
    else:
        port = cube.get("otlp_http_port", 4318)
        env_list += [
            "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL=http/protobuf",
            f"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://{host}:{port}/v1/traces",
        ]
    return env_list


def telemetrygen_service(signal, name, slug, rate, env, cube):
    """One telemetrygen container = real OTLP protobuf for one stream+signal.
    traces -> gRPC (4317); logs/metrics -> OTLP/HTTP to CubeAPM's ingest paths."""
    host = cube["host"]
    common = [
        f"--service={name}",
        f'--otlp-header=x-cube-org-slug="{slug}"',
        f"--rate={rate}",
        "--duration=inf",
    ]
    if signal == "traces":
        if cube.get("traces_transport", "grpc") == "http":
            cmd = ["traces",
                   f"--otlp-endpoint={host}:{cube.get('otlp_http_port', 4318)}",
                   "--otlp-http", "--otlp-insecure",
                   "--otlp-http-url-path=/v1/traces",
                   f'--otlp-attributes=cube.environment="{env}"',
                   *common]
        else:
            cmd = ["traces",
                   f"--otlp-endpoint={host}:{cube.get('otlp_grpc_port', 4317)}",
                   "--otlp-insecure",
                   f'--otlp-attributes=cube.environment="{env}"',
                   *common]
    elif signal == "logs":
        cmd = ["logs",
               f"--otlp-endpoint={host}:{cube['ingest_port']}",
               "--otlp-http", "--otlp-insecure",
               "--otlp-http-url-path=/api/logs/insert/opentelemetry/v1/logs",
               '--otlp-header=Cube-Stream-Fields="service.name,severity"',
               *common]
    elif signal == "metrics":
        cmd = ["metrics",
               f"--otlp-endpoint={host}:{cube['ingest_port']}",
               "--otlp-http", "--otlp-insecure",
               "--otlp-http-url-path=/api/metrics/v1/save/otlp",
               *common]
    else:
        return None
    return {
        "image": TELEMETRYGEN_IMAGE,
        "container_name": f"syn-{name}-{signal}",
        "command": cmd,
        "extra_hosts": ["host.docker.internal:host-gateway"],
        "restart": "always",
    }


def datadog_env(name, env, ver, gw_http_port):
    # Agentless trace mode: tracer -> tenant-gateway (adds slug) -> CubeAPM :3130.
    return [
        "JAVA_TOOL_OPTIONS=-javaagent:/java/dd-java-agent.jar",
        f"DD_SERVICE={name}",
        f"DD_TRACE_AGENT_URL=http://tenant-gateway:{gw_http_port}",
        f"DD_ENV={env}",
        f"DD_VERSION={ver}",
    ]


def elastic_env(name, env, ver, gw_http_port):
    return [
        "JAVA_TOOL_OPTIONS=-javaagent:/java/elastic-apm-agent.jar",
        f"ELASTIC_APM_SERVICE_NAME={name}",
        f"ELASTIC_APM_SERVER_URL=http://tenant-gateway:{gw_http_port}",
        "ELASTIC_APM_CAPTURE_JMX_METRICS=object_name[java.nio:type=BufferPool,name=*] attribute[*]",
        "ELASTIC_APM_LOG_SENDING=true",
        f"ELASTIC_APM_ENVIRONMENT={env}",
        f"ELASTIC_APM_SERVICE_VERSION={ver}",
    ]


def newrelic_env(name, gw_tls_port, license_key, env, ver):
    # NR agent always uses TLS and can't add a header -> go through the gateway's
    # per-org TLS listener (self-signed cert trusted via NEW_RELIC_CA_BUNDLE_PATH).
    return [
        "JAVA_TOOL_OPTIONS=-javaagent:/java/newrelic-agent.jar",
        f"NEW_RELIC_APP_NAME={name}",
        "NEW_RELIC_HOST=tenant-gateway",
        f"NEW_RELIC_PORT={gw_tls_port}",
        f"NEW_RELIC_LICENSE_KEY={license_key}",
        "NEW_RELIC_CA_BUNDLE_PATH=/certs/gateway.crt",
        # The NR agent forwards every NEW_RELIC_METADATA_* env var; CubeAPM maps keys
        # prefixed NEW_RELIC_METADATA_CUBE_ATTRS_ into resource attributes (_P_ -> '.').
        # This sets cube.environment + service.version so NR matches the other vendors
        # (otherwise CubeAPM shows env=UNSET for the New Relic service).
        f"NEW_RELIC_METADATA_CUBE_ATTRS_CUBE_P_ENVIRONMENT={env}",
        f"NEW_RELIC_METADATA_CUBE_ATTRS_SERVICE_P_VERSION={ver}",
        "NEW_RELIC_APPLICATION_LOGGING_ENABLED=true",
        "NEW_RELIC_APPLICATION_LOGGING_FORWARDING_ENABLED=true",
        "NEW_RELIC_APPLICATION_LOGGING_LOCAL_DECORATING_ENABLED=true",
        "NEW_RELIC_JMX_ENABLED=true",
    ]


# --------------------------------------------------------------------- build ---
def build(cfg):
    cube = cfg["cubeapm"]
    nr_cfg = cube.get("newrelic", {}) or {}
    nr_enabled = nr_cfg.get("enabled", True)
    defaults = cfg.get("defaults", {})
    d_env = defaults.get("environment", "demo")
    d_ver = defaults.get("service_version", "1.0.0")
    d_traffic = defaults.get("traffic", {}) or {}

    services_in = cfg.get("services", []) or []
    # Drop newrelic services if NR is disabled.
    services_in = [s for s in services_in
                   if not (s["vendor"] == "newrelic" and not nr_enabled)]

    # Allocate gateway ports per org (ordered by first appearance).
    http_ports, tls_ports = {}, {}
    for s in services_in:
        slug, vendor = s["org_slug"], s["vendor"]
        if vendor in ("datadog", "elastic") and slug not in http_ports:
            http_ports[slug] = GW_HTTP_BASE + len(http_ports)
        if vendor == "newrelic" and slug not in tls_ports:
            tls_ports[slug] = GW_TLS_BASE + len(tls_ports)

    need_gateway = bool(http_ports or tls_ports)
    need_tls = bool(tls_ports)

    compose = {"services": {}, "volumes": {"cube_java_springboot_mysql": None}}
    svcs = compose["services"]

    # --- shared infra ---
    svcs["mysql"] = {
        "image": "mysql:8.0",
        "container_name": MYSQL_HOST,
        "environment": ["MYSQL_ROOT_PASSWORD=root", "MYSQL_DATABASE=test"],
        "volumes": ["cube_java_springboot_mysql:/var/lib/mysql"],
        "healthcheck": {
            "test": ["CMD", "mysqladmin", "ping", "-h", "localhost", "-proot"],
            "interval": "10s", "timeout": "5s", "retries": 12, "start_period": "30s",
        },
        "restart": "always",
    }
    svcs["redis"] = {
        "image": "redis:alpine3.18",
        "container_name": REDIS_HOST,
        "restart": "always",
    }

    # --- tenant gateway (only if any non-OTEL vendor is present) ---
    if need_gateway:
        gw = {
            "image": "nginx:alpine",
            "container_name": "tenant-gateway",
            "volumes": ["./nginx/nginx.conf:/etc/nginx/nginx.conf:ro"],
            "extra_hosts": ["host.docker.internal:host-gateway"],
            "restart": "always",
        }
        if need_tls:
            gw["volumes"].append("./certs:/certs:ro")
        svcs["tenant-gateway"] = gw

    # --- real app instances ---
    first_app = True
    app_names = []
    for s in services_in:
        name, vendor, slug = s["name"], s["vendor"], s["org_slug"]
        env = s.get("environment", d_env)
        ver = s.get("service_version", d_ver)
        if vendor == "otel":
            environment = otel_env(name, slug, env, ver, cube)
        elif vendor == "datadog":
            environment = datadog_env(name, env, ver, http_ports[slug])
        elif vendor == "elastic":
            environment = elastic_env(name, env, ver, http_ports[slug])
        elif vendor == "newrelic":
            environment = newrelic_env(name, tls_ports[slug], nr_cfg.get("license_key", ""), env, ver)
        else:
            sys.exit(f"Unknown vendor '{vendor}' for service '{name}'")

        depends = {
            "mysql": {"condition": "service_healthy"},
            "redis": {"condition": "service_started"},
        }
        svc = {
            "image": APP_IMAGE,
            "container_name": f"app-{name}",
            "environment": environment,
            "depends_on": depends,
            "extra_hosts": ["host.docker.internal:host-gateway"],
            "restart": "always",
        }
        if vendor != "otel":
            depends["tenant-gateway"] = {"condition": "service_started"}
        if vendor == "newrelic":
            svc["volumes"] = ["./certs:/certs:ro"]
        if first_app:
            # Build the image once; every other app container reuses APP_IMAGE.
            svc["build"] = {"context": "..", "dockerfile": "generator/Dockerfile"}
            first_app = False
        svcs[f"app-{name}"] = svc
        app_names.append(name)

    # --- load generator ---
    targets = []
    for s in services_in:
        t = dict(d_traffic)
        t.update(s.get("traffic", {}) or {})
        targets.append({
            "name": s["name"],
            "url": f"http://app-{s['name']}:8000",
            "rps": t.get("rps", 2),
            "error_ratio": t.get("error_ratio", 0.05),
        })
    if targets:
        svcs["loadgen"] = {
            "build": {"context": "./loadgen"},
            "container_name": "loadgen",
            "volumes": ["./loadgen/targets.json:/work/targets.json:ro"],
            "depends_on": {f"app-{n}": {"condition": "service_started"} for n in app_names},
            "restart": "always",
        }

    # --- synthetic streams (telemetrygen, real OTLP protobuf) ---
    streams = cfg.get("synthetic", []) or []
    syn_count = 0
    for st in streams:
        svc, slug = st["service_name"], st["org_slug"]
        rate = st.get("rps", 1)
        for sig in st.get("signals", ["traces"]):
            s = telemetrygen_service(sig, svc, slug, rate, d_env, cube)
            if s:
                svcs[s["container_name"]] = s
                syn_count += 1

    nginx_conf = render_nginx(http_ports, tls_ports, cube, nr_cfg)
    # Stamp the config hash so `docker compose up` recreates the gateway whenever
    # the (bind-mounted) nginx.conf changes — nginx won't reload it on its own.
    if "tenant-gateway" in svcs:
        digest = hashlib.md5(nginx_conf.encode()).hexdigest()[:12]
        svcs["tenant-gateway"]["environment"] = [f"NGINX_CONFIG_SHA={digest}"]
    return compose, targets, syn_count, nginx_conf, http_ports, tls_ports


def render_nginx(http_ports, tls_ports, cube, nr_cfg):
    host, ingest = cube["host"], cube["ingest_port"]
    nr_upstream_port = nr_cfg.get("upstream_port", ingest)
    lines = [
        "# GENERATED by generate.py — do not edit. Per-org tenant gateway.",
        "# Each server injects x-cube-org-slug for one org and forwards to CubeAPM.",
        "worker_processes auto;",
        "events { worker_connections 1024; }",
        "http {",
        "  # Resolve host.docker.internal via Docker DNS, IPv4 only: /etc/hosts also",
        "  # carries a non-routable IPv6 entry that otherwise causes connect() retries.",
        "  resolver 127.0.0.11 ipv6=off valid=30s;",
        "  proxy_http_version 1.1;",
        "  client_max_body_size 50m;",
    ]
    # HTTP listeners (Datadog + Elastic) -> CubeAPM ingest port.
    for slug, port in http_ports.items():
        lines += [
            f"  # org '{slug}' (datadog/elastic)",
            "  server {",
            f"    listen {port};",
            f"    set $cube_up {host};",
            "    location / {",
            f"      proxy_set_header x-cube-org-slug {slug};",
            f"      proxy_pass http://$cube_up:{ingest};",
            "    }",
            "  }",
        ]
    # TLS listeners (New Relic) -> CubeAPM NR upstream port.
    # Host is forced to 'tenant-gateway' so CubeAPM's NR collector redirect_host
    # points back to the gateway (keeping the slug header) instead of host.docker.internal.
    for slug, port in tls_ports.items():
        lines += [
            f"  # org '{slug}' (newrelic, TLS)",
            "  server {",
            f"    listen {port} ssl;",
            "    ssl_certificate     /certs/gateway.crt;",
            "    ssl_certificate_key /certs/gateway.key;",
            f"    set $cube_up {host};",
            "    location / {",
            f"      proxy_set_header x-cube-org-slug {slug};",
            "      proxy_set_header Host tenant-gateway;",
            f"      proxy_pass http://$cube_up:{nr_upstream_port};",
            "    }",
            "  }",
        ]
    lines.append("}")
    return "\n".join(lines) + "\n"


def main():
    cfg = load_config()
    compose, targets, syn_count, nginx_conf, http_ports, tls_ports = build(cfg)

    os.makedirs(os.path.join(HERE, "nginx"), exist_ok=True)
    os.makedirs(os.path.join(HERE, "loadgen"), exist_ok=True)

    with open(os.path.join(HERE, "docker-compose.generated.yml"), "w") as fh:
        fh.write("# GENERATED by generate.py from config.yaml — do not edit by hand.\n")
        yaml.safe_dump(compose, fh, sort_keys=False, default_flow_style=False)
    with open(os.path.join(HERE, "nginx", "nginx.conf"), "w") as fh:
        fh.write(nginx_conf)
    with open(os.path.join(HERE, "loadgen", "targets.json"), "w") as fh:
        json.dump(targets, fh, indent=2)

    print("Generated:")
    print(f"  docker-compose.generated.yml  ({len(compose['services'])} services)")
    print(f"  nginx/nginx.conf              (http orgs: {list(http_ports)}, tls orgs: {list(tls_ports)})")
    print(f"  loadgen/targets.json          ({len(targets)} real services)")
    print(f"  telemetrygen services         ({syn_count} synthetic stream-signals)")
    if tls_ports:
        print("Note: New Relic uses the TLS gateway — run `make certs` (make up does this).")


if __name__ == "__main__":
    main()
