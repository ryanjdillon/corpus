#!/usr/bin/env bash
# Lint and render the chart: each example and test values file must render, the
# rendered integers, strings and container commands must be exact, and each
# file under tests/invalid must be refused. `just helm-check` and CI both run
# this. Needs helm and yq (mikefarah, v4).
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

# Every container sets its command. Helm adopting a live object patches it
# against the render, so a command the render leaves out keeps the live one.
# One line per container: "<workload> <container> <command...>".
commands() {
    render "$1" | yq e -N '
        select(.kind == "Deployment" or .kind == "CronJob")
        | .metadata.name as $w
        | .. | select(tag == "!!map") | select(has("containers"))
        | (.containers + (.initContainers // []))[]
        | $w + " " + .name + " " + ((.command // []) | join(" "))' -
}
for values in examples/values-*.yaml tests/values-*.yaml; do
    out=$(commands "$values")
    if [[ -z "$out" ]]; then
        echo "$values: no container rendered" >&2
        exit 1
    fi
    if missing=$(awk 'NF < 3' <<< "$out") && [[ -n "$missing" ]]; then
        echo "$values: containers without a command:" >&2
        echo "$missing" >&2
        exit 1
    fi
done
for values in examples/values-*.yaml; do
    if wrong=$(awk '$3 != "corpus" || NF != 3' <<< "$(commands "$values")") && [[ -n "$wrong" ]]; then
        echo "$values: containers whose command is not the image's (corpus):" >&2
        echo "$wrong" >&2
        exit 1
    fi
done
expected='corpus server corpus-serve
corpus-backfill job corpus
corpus-backfill prepare corpus scan
corpus-index server corpus-index
corpus-pipeline-inbox-a pipeline corpus-pipeline
corpus-pipeline-inbox-b pipeline corpus-inbox-b'
if ! diff <(sort <<< "$expected") <(commands tests/values-command.yaml | sort) >&2; then
    echo "tests/values-command.yaml: commands do not follow the layering" >&2
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
