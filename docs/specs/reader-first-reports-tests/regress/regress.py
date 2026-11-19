#!/usr/bin/env python3
"""bake.sh·compare.sh가 부르는 도우미. stdlib만 쓴다.

normalize F           metrics JSON에서 매번 다른 칸(DROP_KEYS)을 빼고 key 순서를 고정해 다시 쓴다.
compare A B [EXPLAIN] 두 굽기 폴더를 견준다. 종료 코드는 전부, JSON은 칸(경로)마다, stderr는 줄마다.
                      차이마다 설명 파일의 pattern(정규식)으로 이유(B 번호)를 붙이고, 설명 못 한 차이가 있으면 exit 1.
"""
import json
import os
import re
import sys

DROP_KEYS = ("generated_at",)
# 순서보다 key로 견줄 목록: 목록 이름 -> 줄을 가르는 칸
KEYED_LISTS = {"data_gaps": ("item", "status"), "checks": ("check",), "flags": ("flag",)}


def drop(x):
    if isinstance(x, dict):
        return {k: drop(v) for k, v in x.items() if k not in DROP_KEYS}
    if isinstance(x, list):
        return [drop(v) for v in x]
    return x


def normalize(path):
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(drop(doc), ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0


def short(v):
    s = json.dumps(v, ensure_ascii=False, sort_keys=True)
    return s if len(s) <= 160 else s[:157] + "..."


def walk_diff(a, b, path, out):
    """a·b의 다른 칸을 (경로, 전, 후)로 모은다. 한쪽에만 있는 칸은 <없음>."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in list(a) + [k for k in b if k not in a]:
            p = "%s.%s" % (path, k) if path else k
            if k not in a:
                out.append((p, "<없음>", short(b[k])))
            elif k not in b:
                out.append((p, short(a[k]), "<없음>"))
            else:
                walk_diff(a[k], b[k], p, out)
        return
    name = path.rsplit(".", 1)[-1]
    if isinstance(a, list) and isinstance(b, list) and name in KEYED_LISTS:
        keys = KEYED_LISTS[name]

        def keyed(xs):
            out, seen = {}, {}
            for e in xs:  # 같은 key가 여러 줄이면 차례대로 #2, #3을 붙인다
                k = "|".join(str(e.get(c)) for c in keys) if isinstance(e, dict) else short(e)
                seen[k] = seen.get(k, 0) + 1
                out[k if seen[k] == 1 else "%s#%d" % (k, seen[k])] = e
            return out
        ka, kb = keyed(a), keyed(b)
        for k in list(ka) + [k for k in kb if k not in ka]:
            p = "%s[%s]" % (path, k)
            if k not in ka:
                out.append((p, "<없음>", short(kb[k])))
            elif k not in kb:
                out.append((p, short(ka[k]), "<없음>"))
            else:
                walk_diff(ka[k], kb[k], p, out)
        return
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        for i, (x, y) in enumerate(zip(a, b)):
            walk_diff(x, y, "%s[%d]" % (path, i), out)
        return
    if a != b:
        out.append((path, short(a), short(b)))


def read_exits(d):
    out = {}
    p = os.path.join(d, "exit.tsv")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    name, code = line.rstrip("\n").split("\t")
                    out[name] = code
    return out


def read_text(p):
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return f.read().splitlines()


def compare(a_dir, b_dir, explain_path=None):
    rules = []
    if explain_path:
        with open(explain_path, encoding="utf-8") as f:
            rules = [(re.compile(r["pattern"]), r["why"]) for r in json.load(f)]
    ea, eb = read_exits(a_dir), read_exits(b_dir)
    lines = []  # "대상 경로: 전 -> 후"
    for name in sorted(set(ea) | set(eb)):
        if name not in ea:
            lines.append("%s exit: <기준에 없음> -> %s" % (name, eb[name]))
            continue
        if name not in eb:
            lines.append("%s exit: %s -> <지금에 없음>" % (name, ea[name]))
            continue
        if ea[name] != eb[name]:
            lines.append("%s exit: %s -> %s" % (name, ea[name], eb[name]))
        ja, jb = os.path.join(a_dir, name + ".json"), os.path.join(b_dir, name + ".json")
        if os.path.exists(ja) and os.path.exists(jb):
            with open(ja, encoding="utf-8") as f:
                da = json.load(f)
            with open(jb, encoding="utf-8") as f:
                db = json.load(f)
            diffs = []
            walk_diff(da, db, "", diffs)
            lines += ["%s %s: %s -> %s" % (name, p, x, y) for p, x, y in diffs]
        elif os.path.exists(ja) != os.path.exists(jb):
            lines.append("%s json: %s -> %s" % (name, "있음" if os.path.exists(ja) else "없음",
                                                  "있음" if os.path.exists(jb) else "없음"))
        sa = read_text(os.path.join(a_dir, name + ".err")) or []
        sb = read_text(os.path.join(b_dir, name + ".err")) or []
        for x in sa:
            if x not in sb:
                lines.append("%s stderr-: %s" % (name, x))
        for x in sb:
            if x not in sa:
                lines.append("%s stderr+: %s" % (name, x))
    unexplained, by_why = [], {}
    for ln in lines:
        why = next((w for rx, w in rules if rx.search(ln)), None)
        if why is None:
            unexplained.append(ln)
        else:
            by_why.setdefault(why, []).append(ln)
    for why, ls in by_why.items():
        print("== %s (%d)" % (why, len(ls)))
        for ln in ls:
            print("  " + ln)
    if unexplained:
        print("== 설명 못 한 차이 (%d)" % len(unexplained))
        for ln in unexplained:
            print("  " + ln)
    n = len(set(ea) | set(eb))
    exit_diff = sum(1 for k in set(ea) & set(eb) if ea[k] != eb[k])
    print("대상 %d개(기준 %d, 지금 %d), exit 다름 %d, 차이 %d줄(설명 %d, 설명 못 함 %d)" % (
        n, len(ea), len(eb), exit_diff, len(lines), len(lines) - len(unexplained), len(unexplained)), file=sys.stderr)
    return 1 if unexplained else 0


def main(argv):
    if len(argv) == 2 and argv[0] == "normalize":
        return normalize(argv[1])
    if len(argv) in (3, 4) and argv[0] == "compare":
        return compare(*argv[1:])
    print(__doc__, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
