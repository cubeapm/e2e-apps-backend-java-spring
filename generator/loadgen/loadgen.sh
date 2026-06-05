#!/usr/bin/env bash
# Continuous traffic generator. Reads /work/targets.json (one entry per real
# service) and drives each one in its own background loop at its configured rps,
# mixing endpoints so every service shows traces, DB/cache spans, metrics and a
# slice of errors. Service name + org slug are stamped by the receiving app.
set -u

TARGETS=/work/targets.json
echo "[loadgen] targets:"; cat "$TARGETS"; echo

# Hit one service forever.
hit() {
  local url="$1" rps="$2" err="$3"
  local interval threshold
  interval=$(awk "BEGIN{ if ($rps<=0) print 1; else printf \"%.4f\", 1/$rps }")
  # error_ratio as an integer threshold out of 10000 (bash $RANDOM, no awk reseed bias)
  threshold=$(awk "BEGIN{ printf \"%d\", $err*10000 }")
  while true; do
    if (( (RANDOM % 10000) < threshold )); then
      curl -s -o /dev/null -m 5 "$url/exception" || true
    else
      case $(( RANDOM % 5 )) in
        0) curl -s -o /dev/null -m 5 "$url/" ;;
        1) curl -s -o /dev/null -m 5 "$url/param/req$RANDOM" ;;
        2) curl -s -o /dev/null -m 5 "$url/redis" ;;
        3) curl -s -o /dev/null -m 5 "$url/all" ;;
        4) curl -s -o /dev/null -m 5 -X POST \
             "$url/mysql/add?name=user$RANDOM&email=user$RANDOM@example.com" ;;
      esac || true
    fi
    sleep "$interval"
  done
}

# Give the JVMs time to boot and instrument before hammering them.
echo "[loadgen] warming up (45s)..."
sleep 45

# Process substitution (not a pipe) so the loop runs in THIS shell — otherwise the
# backgrounded hit loops are children of a subshell and `wait` returns instantly,
# making the container exit and restart-loop.
while read -r t; do
  url=$(echo "$t" | jq -r '.url')
  rps=$(echo "$t" | jq -r '.rps')
  err=$(echo "$t" | jq -r '.error_ratio')
  echo "[loadgen] -> $url  rps=$rps  error_ratio=$err"
  hit "$url" "$rps" "$err" &
done < <(jq -c '.[]' "$TARGETS")

wait
