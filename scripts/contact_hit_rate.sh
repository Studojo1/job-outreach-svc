#!/usr/bin/env bash
# What fraction of LinkedIn contacts can we actually email?
#
# This is the number that decides whether the flow works. If Apollo resolves
# most contacts, students reach real people. If it resolves few, they edit
# emails that can never be sent — and the design needs rethinking.
#
# Usage:  ./scripts/contact_hit_rate.sh [namespace] [since]
#   ./scripts/contact_hit_rate.sh staging 24h
set -euo pipefail

NS="${1:-staging}"
SINCE="${2:-24h}"

kubectl logs -n "$NS" -l app=job-outreach-svc --since="$SINCE" --tail=-1 2>/dev/null \
  | grep "\[EXT-RESOLVE\]" \
  | awk '
      { for (i=1;i<=NF;i++) if ($i ~ /^outcome=/) { split($i,a,"="); o=a[2]; c[o]++; n++ }
        for (i=1;i<=NF;i++) if ($i ~ /^source=/)  { split($i,b,"="); s[b[2]]++ } }
      END {
        if (n == 0) { print "No resolutions logged in the window."; exit }
        printf "resolutions: %d\n\n", n
        printf "  reachable   %5d  %5.1f%%\n", c["reachable"],   100*c["reachable"]/n
        printf "  unreachable %5d  %5.1f%%\n", c["unreachable"], 100*c["unreachable"]/n
        printf "  errors      %5d  %5.1f%%\n", c["error"],       100*c["error"]/n
        printf "\nby source (apollo = a paid call, cache/page = free)\n"
        for (k in s) printf "  %-8s %5d\n", k, s[k]
      }'
