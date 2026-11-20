#!/usr/bin/env bash
# Local Neo4j for development: scripts/neo4j-local.sh start|stop|status
# Lives entirely under .tools/ (JDK + Neo4j Community). The password is generated once into .tools/neo4j-password.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
export JAVA_HOME="$(ls -d "$root"/.tools/jdk-*/Contents/Home)"
export NEO4J_HOME="$(ls -d "$root"/.tools/neo4j-community-*)"
secret="$root/.tools/neo4j-password"
action="${1:?start|stop|status}"
"$NEO4J_HOME/bin/neo4j" "$action"
if [ "$action" = start ] && [ ! -f "$secret" ]; then
  # first start: replace the default password with a generated one
  new="$(openssl rand -hex 16)"
  until "$NEO4J_HOME/bin/cypher-shell" -u neo4j -p neo4j -d system \
    "ALTER CURRENT USER SET PASSWORD FROM 'neo4j' TO '$new'" >/dev/null 2>&1; do sleep 2; done
  (umask 077 && printf '%s' "$new" >"$secret")
fi
