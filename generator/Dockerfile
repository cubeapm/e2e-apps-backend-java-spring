# Unified app image: the same Spring Boot app, with ALL FOUR vendor agents baked
# in. Which agent runs is chosen per-container at runtime via JAVA_TOOL_OPTIONS
# (set by generate.py in docker-compose.generated.yml), so we build this once and
# every real-service container reuses it.
#
# Build context is the REPOSITORY ROOT (see build.context: ".." in the generated
# compose), so the Spring source (pom.xml, mvnw, src/) is available to COPY.
FROM ubuntu:22.04

WORKDIR /java

RUN apt-get update && apt-get install -y --no-install-recommends \
        openjdk-17-jdk maven wget ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# App source (whole repo root; generator/ is harmless extra weight).
COPY . .
RUN chmod +x mvnw || true

# --- vendor agents (URLs lifted verbatim from each per-vendor branch Dockerfile) ---
ADD https://github.com/open-telemetry/opentelemetry-java-instrumentation/releases/latest/download/opentelemetry-javaagent.jar /java/opentelemetry-javaagent.jar
ADD https://dtdg.co/latest-java-tracer /java/dd-java-agent.jar
ADD https://download.newrelic.com/newrelic/java-agent/newrelic-agent/current/newrelic-agent.jar /java/newrelic-agent.jar
RUN wget -O /java/elastic-apm-agent.jar \
        "https://search.maven.org/remotecontent?filepath=co/elastic/apm/elastic-apm-agent/1.53.0/elastic-apm-agent-1.53.0.jar"

# New Relic looks for JMX extensions in <agent-dir>/extensions (i.e. /java/extensions).
RUN mkdir -p /java/extensions \
    && cp /java/generator/newrelic-extensions/jmx-metrics.yml /java/extensions/jmx-metrics.yml

RUN ./mvnw clean install -q -DskipTests

EXPOSE 8000

CMD ["./mvnw", "spring-boot:run"]
