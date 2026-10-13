#!/usr/bin/env bash
# Lint and render the chart: each example and test values file must render, the
# rendered integers and strings must be exact, and each file under
# tests/invalid must be refused. `just helm-check` and CI both run this.
set -euo pipefail

cd "$(dirname "$0")/.."
chart=corpus

render() { helm template corpus "$chart" -n corpus -f "$1"; }

for values in examples/values-*.yaml tests/values-*.yaml; do
    helm lint --strict "$chart" -f "$values"
    render "$values" > /dev/null
done

# Helm holds every number as a float64; a template that prints one unformatted
# writes 2e+06. Each value of the regression file must come back exactly.
out=$(render tests/values-large-numbers.yaml)
if grep -nE '[:-] "?[0-9]+(\.[0-9]+)?e[+-]?[0-9]+"?$' <<< "$out"; then
    echo "rendered output has a number in scientific notation" >&2
    exit 1
fi
expect=(
    'replicas: 1000000'
    'terminationGracePeriodSeconds: 2000001'
    'periodSeconds: 2000002'
    'successfulJobsHistoryLimit: 1000001'
    'failedJobsHistoryLimit: 1000002'
    'backoffLimit: 1000003'
    'activeDeadlineSeconds: 2000000'
    'terminationGracePeriodSeconds: 2000003'
    'limit: 2000005'
    'CORPUS_SHARED_LIMIT: "2000000"'
    'value: "2000004"'
    'value: "2000006"'
)
for line in "${expect[@]}"; do
    if ! grep -qE "^[[:space:]-]*${line}\$" <<< "$out"; then
        echo "rendered output lacks: $line" >&2
        exit 1
    fi
done

# server.port is the single source of the listen port: with no CORPUS_MCP_PORT
# in any user env, the container port, the Service and the env var the readiness
# probe reads must all agree on it.
out=$(render tests/values-port.yaml)
if grep -q 'CORPUS_MCP_PORT: ' <<< "$out"; then
    echo "CORPUS_MCP_PORT must come from the server container, not a ConfigMap" >&2
    exit 1
fi
for line in 'containerPort: 9100' 'port: 9100' '- name: CORPUS_MCP_PORT' 'value: "9100"'; do
    if ! grep -qE "^[[:space:]-]*${line}\$" <<< "$out"; then
        echo "rendered output lacks: $line" >&2
        exit 1
    fi
done
if ! grep -q 'os.environ\["CORPUS_MCP_PORT"\]' <<< "$out"; then
    echo "readiness probe does not read CORPUS_MCP_PORT" >&2
    exit 1
fi

# Refused by the schema, not by some other error on the way.
for values in tests/invalid/*.yaml; do
    if err=$(render "$values" 2>&1 > /dev/null); then
        echo "$values rendered, but must be refused" >&2
        exit 1
    fi
    if ! grep -q "don't meet the specifications of the schema" <<< "$err"; then
        echo "$values failed, but not on the schema:" >&2
        echo "$err" >&2
        exit 1
    fi
done

echo "helm chart: lint, render and value checks passed"
