#!/bin/sh
# 회귀 기준 굽기: 데이터 폴더의 facts마다 reit_calc.py metrics를 돌려 출력·exit·stderr를 한 폴더에 둔다.
# 쓰는 법: bake.sh <skill 폴더> <데이터 폴더(INVEST_DATA_DIR)> <출력 폴더>
#   예: bake.sh skills/reit-stock-review /tmp/data /tmp/base1
# 실제 ~/invest-data가 아니라 사본(macro·reitwatch·tracker·tickers/*/facts)을 데이터 폴더로 준다.
# 출력 폴더에는 <T>-<P>.json(metrics), <T>-<P>.err(stderr), exit.tsv(대상·exit)가 생긴다.
# 시각처럼 매번 다른 칸은 regress.py normalize가 뺀다(지금 metrics에는 없다. 생기면 DROP_KEYS에 더한다).
set -u
if [ $# -ne 3 ]; then
  echo "쓰는 법: bake.sh <skill 폴더> <데이터 폴더> <출력 폴더>" >&2
  exit 1
fi
SKILL=$(cd "$1" && pwd)
DATA=$(cd "$2" && pwd)
OUT=$3
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
: > "$OUT/exit.tsv"
n=0
for f in "$DATA"/tickers/*/facts/*.json; do
  [ -f "$f" ] || continue
  t=$(basename "$(dirname "$(dirname "$f")")")
  p=$(basename "$f" .json)
  name="$t-$p"
  INVEST_DATA_DIR="$DATA" python3 -I "$SKILL/scripts/reit_calc.py" metrics --facts "$f" --out "$OUT/$name.json" \
    > /dev/null 2> "$OUT/$name.err"
  code=$?
  printf '%s\t%s\n' "$name" "$code" >> "$OUT/exit.tsv"
  if [ -f "$OUT/$name.json" ]; then
    python3 -I "$HERE/regress.py" normalize "$OUT/$name.json" || exit 1
  fi
  n=$((n + 1))
done
echo "구움: $n개 -> $OUT" >&2
