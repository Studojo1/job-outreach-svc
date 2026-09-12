#!/bin/sh
# What fraction of the time can we actually put a verified email in front of a
# student?
#
# Pranav asked for a number instead of a hope. Every contact resolution already
# writes one structured [EXT-RESOLVE] line; this turns those lines into the
# rate, split by WHERE the answer came from so a bad number points at a cause.
#
#   usage:  ./scripts/hit_rate.sh [namespace] [since]
#   e.g.    ./scripts/hit_rate.sh studojo-staging 24h
#
# Read it like this:
#   reachable/total          the headline hit rate
#   source=page              the job page named someone we could reach
#   source=search            Apollo's free search found them
#   source=apollo            a PAID reveal resolved the address
#   source=cache             we already knew them, no spend
#   outcome=error            Apollo failed — NOT the same as "nobody there",
#                            and if this is large the hit rate is meaningless
set -eu
NS="${1:-studojo-staging}"
SINCE="${2:-24h}"

echo "namespace=$NS since=$SINCE"
# EXT_RESOLVE_FILE lets this be exercised against a captured log without a
# cluster — the parsing is the part that can be wrong, and it should not need
# kubectl to be checked.
if [ -n "${EXT_RESOLVE_FILE:-}" ]; then
  lines=$(grep '\[EXT-RESOLVE\]' "$EXT_RESOLVE_FILE" || true)
else
  lines=$(kubectl logs -n "$NS" -l app=job-outreach-svc --since="$SINCE" --tail=-1 2>/dev/null \
          | grep '\[EXT-RESOLVE\]' || true)
fi

if [ -z "$lines" ]; then
  echo "no [EXT-RESOLVE] lines found."
  echo "either nothing has been drafted in this window, or kubectl cannot reach the pods."
  exit 0
fi

total=$(printf '%s\n' "$lines" | wc -l | tr -d ' ')
reach=$(printf '%s\n' "$lines" | grep -c 'outcome=reachable' || true)
unreach=$(printf '%s\n' "$lines" | grep -c 'outcome=unreachable' || true)
err=$(printf '%s\n' "$lines" | grep -c 'outcome=error' || true)

echo
echo "resolutions: $total"
echo "  reachable   : $reach"
echo "  unreachable : $unreach"
echo "  error       : $err   <- Apollo failed; not evidence about the company"
echo

# The honest denominator EXCLUDES errors: an Apollo outage is not a company
# without contactable people, and counting it as a miss understates the tool.
den=$((reach + unreach))
if [ "$den" -gt 0 ]; then
  echo "HIT RATE (excluding errors): $((reach * 100 / den))%  ($reach/$den)"
else
  echo "HIT RATE: no conclusive resolutions yet"
fi

echo
echo "where the answer came from:"
for s in page search cache apollo; do
  n=$(printf '%s\n' "$lines" | grep -c "source=$s" || true)
  r=$(printf '%s\n' "$lines" | grep "source=$s" | grep -c 'outcome=reachable' || true)
  [ "$n" -gt 0 ] && echo "  $s: $r/$n reachable"
done

echo
echo "companies we could NOT reach (most frequent first):"
printf '%s\n' "$lines" | grep 'outcome=unreachable' \
  | sed -n 's/.*company=\(.*\) user=.*/\1/p' | sort | uniq -c | sort -rn | head -10
