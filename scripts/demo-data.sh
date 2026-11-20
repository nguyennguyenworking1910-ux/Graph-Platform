#!/usr/bin/env bash
# Build the demo tenant end to end: mock data, trunk, identity, graph, every problem.
set -euo pipefail
tenant="${1:-alpha_group}"
gp simulate "$tenant" --parties "${PARTIES:-20000}"
gp onboard "$tenant"
gp resolve "$tenant"
gp graph build "$tenant"
for config in conf/tenants/"$tenant"/branches/*.yaml; do
  name="$(basename "$config" .yaml)"
  [ "$name" = credit_limit_review ] || gp branch run "$tenant" "$name"
done
gp branch run "$tenant" credit_limit_review   # reads the results of two other problems
