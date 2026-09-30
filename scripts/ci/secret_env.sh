#!/usr/bin/env bash
# Keep secrets out of the Deployment spec (audit ST-N07).
#
# A literal `value:` in a Deployment env is readable by anyone who can read
# Deployments or old ReplicaSets, and the default 'view' role hides Secrets but
# not those. Every secret therefore lives in the app-secrets Secret and reaches
# the pod through valueFrom.secretKeyRef. Sourced by the deploy workflows:
#
#   NS=studojo DEP=job-outreach-svc . scripts/ci/secret_env.sh
#   secret_put  meta-capi-token "$META_CAPI_TOKEN"   # store (skips empty)
#   secret_adopt DODO_WEBHOOK_SECRET dodo-webhook-secret
#   secret_ref  RAZORPAY_KEY_SECRET razorpay-key-prod-secret
#
# Values never pass through the command line of anything but kubectl's patch
# body, and GitHub masks secrets in the log.
: "${NS:?NS is required}" "${DEP:?DEP is required}"  # no set -u: this is sourced into the deploy step

_b64() { printf '%s' "$1" | base64 | tr -d '\n'; }

secret_has() {  # secret_has KEY
  [ -n "$(kubectl -n "$NS" get secret app-secrets -o jsonpath="{.data.${1//./\\.}}")" ]
}

secret_put() {  # secret_put KEY VALUE  (an empty VALUE changes nothing)
  [ -n "${2:-}" ] || return 0
  kubectl -n "$NS" patch secret app-secrets --type=merge \
    -p "{\"data\":{\"$1\":\"$(_b64 "$2")\"}}" >/dev/null
  echo "app-secrets/$1 set"
}

_literal() {  # the plain value an env var holds on the Deployment today, if any
  kubectl -n "$NS" get "deployment/$DEP" \
    -o jsonpath="{.spec.template.spec.containers[?(@.name=='$DEP')].env[?(@.name=='$1')].value}"
}

secret_ref() {  # secret_ref ENV KEY: point ENV at app-secrets/KEY (drops any literal)
  if ! secret_has "$2"; then
    echo "::warning::app-secrets/$2 missing in $NS; $1 left as it is"
    return 0
  fi
  kubectl -n "$NS" patch "deployment/$DEP" --type=strategic -p "{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"$DEP\",\"env\":[{\"name\":\"$1\",\"value\":null,\"valueFrom\":{\"secretKeyRef\":{\"name\":\"app-secrets\",\"key\":\"$2\"}}}]}]}}}}" >/dev/null
  echo "$1 -> app-secrets/$2"
}

secret_adopt() {  # secret_adopt ENV KEY: move a hand-set literal into app-secrets, then ref it
  if ! secret_has "$2"; then
    secret_put "$2" "$(_literal "$1")"
  fi
  secret_ref "$1" "$2"
}
