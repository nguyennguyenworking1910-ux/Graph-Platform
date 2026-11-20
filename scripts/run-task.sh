#!/usr/bin/env bash
# One supervision pass for one tenant: stage in from S3, run, stage out. S3 is the durable store.
# Required env: TENANT, DATA_BUCKET.
set -euo pipefail
: "${TENANT:?}" "${DATA_BUCKET:?}"
base="s3://${DATA_BUCKET}/${TENANT}"
lock="${TENANT}/control/supervisor.lock"

# single writer per tenant: the conditional put fails when another pass holds the lock
# a lock older than 6 hours belongs to a task that died
stale=$(aws s3api list-objects-v2 --bucket "$DATA_BUCKET" --prefix "$lock" \
  --query "Contents[?LastModified<='$(date -u -d '6 hours ago' +%Y-%m-%dT%H:%M:%SZ)'].Key" --output text)
[ -n "$stale" ] && [ "$stale" != "None" ] && aws s3 rm "s3://${DATA_BUCKET}/${lock}" >/dev/null
if ! aws s3api put-object --bucket "$DATA_BUCKET" --key "$lock" --if-none-match '*' --body /dev/null >/dev/null 2>&1; then
  echo "another pass is running for ${TENANT}; exiting"
  exit 0
fi
trap 'aws s3 rm "s3://${DATA_BUCKET}/${lock}" >/dev/null' EXIT

mkdir -p "/data/${TENANT}"
aws s3 sync "${base}/data/" "/data/${TENANT}/" --only-show-errors
aws s3 sync "${base}/conf/" "/app/conf/tenants/${TENANT}/" --only-show-errors

gp supervise "$TENANT"

aws s3 sync "/app/conf/tenants/${TENANT}/" "${base}/conf/" --only-show-errors --delete
aws s3 sync "/data/${TENANT}/" "${base}/data/" --only-show-errors --delete --exclude "landing/*"
