#!/bin/sh
# 회귀 기준과 견주기: 종료 코드는 전부, metrics JSON은 칸(경로)마다, stderr는 줄마다 견준다.
# 쓰는 법: compare.sh <기준 폴더> <지금 폴더> [설명 파일]
#   설명 파일(JSON): [{"pattern": "<정규식>", "why": "B10 ..."}]. 차이 줄("대상 경로: 전 -> 후")이
#   pattern에 맞으면 그 이유로 설명된 차이다. 설명 못 한 차이가 하나라도 있으면 exit 1이다.
set -u
if [ $# -lt 2 ]; then
  echo "쓰는 법: compare.sh <기준 폴더> <지금 폴더> [설명 파일]" >&2
  exit 1
fi
HERE=$(cd "$(dirname "$0")" && pwd)
exec python3 -I "$HERE/regress.py" compare "$@"
