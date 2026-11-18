#!/usr/bin/env python3
"""macro-check: FRED에서 매크로·섹터 series를 받아 snapshot JSON을 쓴다.

명령
  fetch [--group G] [--sector S] [--out P]   series를 받아 snapshot을 쓴다
  web ID (--value V --as-of D --url U --quote Q [--secondary] | --missing 이유) [--snapshot P]
                                             웹 채우기 값을 snapshot에 넣는다
  --check                                    network 없이 fixture로 확인한다

exit: 0 = 다 채움, 2 = missing이나 check 실패가 있음, 1 = 입력·network 오류.
Python 3 stdlib만 쓴다(3.9 이상).
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

SCHEMA = "macro-snapshot/1"
GROUPS = ["rates", "credit", "inflation", "labor", "growth", "housing", "construction"]
SECTORS = ["office", "industrial", "retail", "residential", "healthcare", "data_center",
           "lodging", "self_storage", "net_lease"]
FREQS = ["daily", "weekly", "monthly", "quarterly"]
SOURCES = ["fred", "web"]
FIELDS = ["id", "name", "group", "sectors", "core", "source", "unit", "freq", "max_lag_days"]
CACHE_SECONDS = 24 * 3600
NEAR_DAYS = 7          # 일·주 자료: 목표일 이전 이 일수 안의 관측을 기준으로 쓴다
HISTORY_MONTHS = 133   # 10년 percentile과 12개월 변화에 쓸 history(11년 + 1개월)
# User-Agent는 urllib 기본값을 둔다. 2026-11-18에 직접 정한 값(브라우저 흉내 포함)은 fredgraph가 응답하지 않았다.
MAX_NETWORK_FAILS = 3  # network 오류가 연달아 이만큼 나면 남은 series는 받지 않는다
FREDGRAPH = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_API = "https://api.stlouisfed.org/fred/series/observations"


class FetchError(Exception):
    pass


class NetworkError(FetchError):
    pass


def log(msg):
    print(msg, file=sys.stderr)


def parse_date(s):
    return dt.date(*[int(x) for x in s[:10].split("-")])


def add_months(d, n):
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    for day in (d.day, 30, 29, 28):
        try:
            return dt.date(y, m, day)
        except ValueError:
            continue


def data_dir():
    return os.path.expanduser(os.environ.get("INVEST_DATA_DIR") or "~/invest-data")


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- series 목록

def load_series_list(path):
    return read_json(path)["series"]


def validate_series_list(items):
    problems, seen = [], set()
    for i, s in enumerate(items):
        label = s.get("id", "#%d" % i)
        for k in FIELDS:
            if k not in s:
                problems.append("%s: %s 칸이 없음" % (label, k))
        if s.get("id") in seen:
            problems.append("%s: id가 겹침" % label)
        seen.add(s.get("id"))
        if s.get("group") not in GROUPS:
            problems.append("%s: group %r" % (label, s.get("group")))
        if s.get("freq") not in FREQS:
            problems.append("%s: freq %r" % (label, s.get("freq")))
        if s.get("source") not in SOURCES:
            problems.append("%s: source %r" % (label, s.get("source")))
        bad = [x for x in s.get("sectors", []) if x not in SECTORS]
        if bad:
            problems.append("%s: sectors %r" % (label, bad))
        if not isinstance(s.get("max_lag_days"), int) or s.get("max_lag_days", 0) <= 0:
            problems.append("%s: max_lag_days" % label)
        if not isinstance(s.get("core"), bool):
            problems.append("%s: core는 true/false" % label)
    return problems


def select_series(items, group=None, sector=None):
    out = [s for s in items if s["core"] or (sector and sector in s["sectors"])]
    if group:
        out = [s for s in out if s["group"] == group]
    return out


# ---------------------------------------------------------------- 받기

def parse_fredgraph_csv(text, sid):
    rows = list(csv.reader(io.StringIO(text)))
    if not rows or len(rows[0]) < 2 or rows[0][0] not in ("observation_date", "DATE"):
        raise FetchError("응답이 CSV가 아님")
    obs = []
    for r in rows[1:]:
        if len(r) < 2 or not r[0]:
            continue
        v = r[1].strip()
        obs.append((parse_date(r[0]), None if v in ("", ".") else float(v)))
    if not any(v is not None for _, v in obs):
        raise FetchError("값이 하나도 없음")
    return obs


def parse_fred_api_json(text):
    data = json.loads(text)
    return [(parse_date(o["date"]), None if o["value"] in ("", ".") else float(o["value"]))
            for o in data["observations"]]


def to_csv(obs, sid):
    lines = ["observation_date,%s" % sid]
    lines += ["%s,%s" % (d.isoformat(), "" if v is None else repr(v)) for d, v in obs]
    return "\n".join(lines) + "\n"


def http_get(url, safe_url):
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404 or e.code == 400:
                raise FetchError("HTTP %d (%s)" % (e.code, safe_url))
            last = "HTTP %d (%s)" % (e.code, safe_url)
        except (urllib.error.URLError, OSError) as e:
            last = "network 오류 %s (%s)" % (getattr(e, "reason", e), safe_url)
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    raise NetworkError(last)


def network_loader(api_key):
    cache_dir = os.path.join(data_dir(), "macro", "cache")
    fails = [0]

    def load(sid, start):
        path = os.path.join(cache_dir, sid + ".csv")
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < CACHE_SECONDS:
            with open(path, encoding="utf-8") as f:
                return parse_fredgraph_csv(f.read(), sid), ""
        try:
            if fails[0] >= MAX_NETWORK_FAILS:
                raise FetchError("network 오류가 %d번 이어져 받지 않음" % MAX_NETWORK_FAILS)
            if api_key:
                q = {"series_id": sid, "api_key": api_key, "file_type": "json",
                     "observation_start": start.isoformat()}
                text = http_get(FRED_API + "?" + urllib.parse.urlencode(q), "FRED API " + sid)
                obs = parse_fred_api_json(text)
                if not any(v is not None for _, v in obs):
                    raise FetchError("값이 하나도 없음")
            else:
                url = "%s?id=%s&cosd=%s" % (FREDGRAPH, sid, start.isoformat())
                obs = parse_fredgraph_csv(http_get(url, "fredgraph.csv " + sid), sid)
            fails[0] = 0
            time.sleep(0.2)
        except (FetchError, ValueError, KeyError) as e:
            if isinstance(e, NetworkError):
                fails[0] += 1
            if os.path.exists(path):
                when = dt.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M")
                with open(path, encoding="utf-8") as f:
                    return parse_fredgraph_csv(f.read(), sid), "받기 실패(%s), %s에 받은 cache를 씀" % (e, when)
            raise FetchError(str(e))
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(to_csv(obs, sid))
        return obs, ""

    return load


# ---------------------------------------------------------------- 계산

def _ref(obs, target, freq):
    """target 시점의 기준 관측 (date, value). 없으면 (None, 이유)."""
    if freq in ("daily", "weekly"):
        best = None
        for d, v in obs:
            if d > target:
                break
            if v is not None and (target - d).days <= NEAR_DAYS:
                best = (d, v)
        return best if best else (None, "%s 앞 %d일 안에 값 없음" % (target.isoformat(), NEAR_DAYS))
    for d, v in obs:
        if d == target:
            return (d, v) if v is not None else (None, "%s 값 없음" % target.isoformat())
    return None, "%s 값 없음" % target.isoformat()


def derive(obs, latest_d, latest_v, spec):
    notes, ref, out = [], {}, {}
    for key, months in (("1m", 1), ("3m", 3), ("12m", 12)):
        if spec["freq"] == "quarterly" and months == 1:
            out["chg_1m"], ref["1m"] = None, None
            notes.append("chg_1m: 분기 자료라 없음")
            continue
        d, v = _ref(obs, add_months(latest_d, -months), spec["freq"])
        if d is None:
            out["chg_" + key], ref[key] = None, None
            notes.append("chg_%s: %s" % (key, v))
        else:
            out["chg_" + key], ref[key] = round(latest_v - v, 4), d.isoformat()
            if key == "12m":
                base12 = v
    if spec["unit"] == "Percent":
        out["yoy_pct"] = None
        notes.append("yoy_pct: 단위가 Percent라 계산하지 않음(chg_12m을 %p로 본다)")
    elif ref.get("12m") is None:
        out["yoy_pct"] = None
        notes.append("yoy_pct: 12개월 전 값 없음")
    elif base12 <= 0:
        out["yoy_pct"] = None
        notes.append("yoy_pct: 12개월 전 값이 0 이하")
    else:
        out["yoy_pct"] = round((latest_v / base12 - 1) * 100, 4)
    start = add_months(latest_d, -120)
    first = next(d for d, v in obs if v is not None)
    if first > start:
        out["pctile_10y"] = None
        notes.append("pctile_10y: history가 10년보다 짧음(%s부터)" % first.isoformat())
    else:
        window = [v for d, v in obs if start < d <= latest_d and v is not None]
        out["pctile_10y"] = round(100.0 * sum(1 for v in window if v <= latest_v) / len(window), 4)
    out["ref"] = ref
    out["note"] = "; ".join(notes)
    return out


def _base(spec):
    return {"name": spec["name"], "group": spec["group"], "sectors": spec["sectors"], "freq": spec["freq"]}


def series_entry(spec, obs, today, load_note=""):
    e = _base(spec)
    if obs is None:
        e.update({"value": None, "unit": spec["unit"], "status": "missing", "as_of": None,
                  "source": "fred:" + spec["id"], "locator": "", "note": load_note})
        e["derived"] = {"status": "missing", "chg_1m": None, "chg_3m": None, "chg_12m": None,
                        "yoy_pct": None, "pctile_10y": None, "ref": {"1m": None, "3m": None, "12m": None},
                        "note": "값이 missing이라 계산하지 않음"}
        return e
    latest_d, latest_v = [(d, v) for d, v in obs if v is not None][-1]
    age = (today - latest_d).days
    status = "stale" if age > spec["max_lag_days"] else "reported"
    notes = [load_note] if load_note else []
    if status == "stale":
        notes.append("최신 관측이 %d일 전(허용 %d일)" % (age, spec["max_lag_days"]))
    e.update({"value": latest_v, "unit": spec["unit"], "status": status, "as_of": latest_d.isoformat(),
              "source": "fred:" + spec["id"],
              "locator": "https://fred.stlouisfed.org/series/%s observation_date=%s" % (spec["id"], latest_d.isoformat()),
              "note": "; ".join(notes)})
    d = derive(obs, latest_d, latest_v, spec)
    e["derived"] = dict([("status", "stale" if status == "stale" else "derived")] + list(d.items()))
    return e


def web_placeholder(spec):
    e = _base(spec)
    e.update({"value": None, "unit": spec["unit"], "status": "missing", "as_of": None,
              "source": "", "locator": "", "note": "웹 채우기 전"})
    return e


def summarize(snap):
    s = {"missing": [], "stale": []}
    for part in ("series", "web"):
        for sid, e in snap[part].items():
            if e["status"] in s:
                s[e["status"]].append(sid)
    return s


def build_snapshot(chosen, loader, today, access, request):
    snap = {"schema": SCHEMA, "date": today.isoformat(),
            "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "fred_access": access, "requests": [request], "series": {}, "web": {}, "sectors": {}}
    start = add_months(today, -HISTORY_MONTHS)
    code = 0
    for spec in chosen:
        if spec["source"] == "web":
            snap["web"][spec["id"]] = web_placeholder(spec)
            continue
        try:
            obs, note = loader(spec["id"], start)
            entry = series_entry(spec, obs, today, note)
        except (FetchError, ValueError, KeyError) as e:
            entry = series_entry(spec, None, today, "받기 실패: %s" % e)
            code = 2
        snap["series"][spec["id"]] = entry
    sector = request.get("sector")
    if sector:
        snap["sectors"][sector] = [s["id"] for s in chosen if sector in s["sectors"]]
    snap["summary"] = summarize(snap)
    return snap, code


def merge_snapshot(old, new):
    out = dict(old)
    out["updated_at"], out["fred_access"] = new["updated_at"], new["fred_access"]
    out["requests"] = old.get("requests", []) + new["requests"]
    series = dict(old.get("series", {}))
    for sid, e in new["series"].items():
        if e["status"] == "missing" and sid in series and series[sid]["status"] != "missing":
            continue  # 같은 날 앞서 받은 값을 실패로 덮지 않는다
        series[sid] = e
    web = dict(old.get("web", {}))
    for sid, e in new["web"].items():
        web.setdefault(sid, e)
    sectors = dict(old.get("sectors", {}))
    sectors.update(new["sectors"])
    out.update({"series": series, "web": web, "sectors": sectors})
    out["summary"] = summarize(out)
    return out


def write_snapshot(snap, path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- 웹 채우기

def web_set(path, spec, today, value=None, as_of=None, url=None, quote=None, secondary=False, missing=None):
    if not os.path.exists(path):
        log("snapshot이 없음: %s (먼저 fetch를 돌린다)" % path)
        return 1
    snap = read_json(path)
    e = _base(spec)
    e["unit"] = spec["unit"]
    if missing is not None:
        if not missing.strip():
            log("--missing에는 이유를 쓴다")
            return 1
        e.update({"value": None, "status": "missing", "as_of": None, "source": "", "locator": "",
                  "note": missing.strip()})
    else:
        if not url or not url.startswith(("http://", "https://")):
            log("--url이 없거나 http(s)로 시작하지 않음. 출처 URL이 없으면 --missing으로 둔다")
            return 1
        if not quote or not quote.strip():
            log("--quote(원문 문장)가 없음. 원문 문장이 없으면 --missing으로 둔다")
            return 1
        try:
            v = float(value)
            d = parse_date(as_of + "-01" if len(as_of or "") == 7 else as_of)
        except (TypeError, ValueError):
            log("--value는 숫자, --as-of는 YYYY-MM 또는 YYYY-MM-DD")
            return 1
        kind = "2차 자료" if secondary else "1차 자료"
        status = "estimated" if secondary else "reported"
        note = kind
        age = (today - d).days
        if age > spec["max_lag_days"]:
            status = "stale"
            note += "; 기준일이 %d일 전(허용 %d일)" % (age, spec["max_lag_days"])
        e.update({"value": v, "status": status, "as_of": d.isoformat(), "source": "web:" + url,
                  "locator": quote.strip(), "note": note})
    snap.setdefault("web", {})[spec["id"]] = e
    snap["summary"] = summarize(snap)
    write_snapshot(snap, path)
    log("%s: %s %s" % (spec["id"], e["status"], "" if e["value"] is None else e["value"]))
    return 0


# ---------------------------------------------------------------- CLI

def assets_path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "series.json")


def snapshot_path(today):
    return os.path.join(data_dir(), "macro", "snapshots", today.isoformat() + ".json")


def cmd_fetch(args):
    if args.group and args.group not in GROUPS:
        log("모르는 group: %s (있는 것: %s)" % (args.group, ", ".join(GROUPS)))
        return 1
    if args.sector and args.sector not in SECTORS:
        log("모르는 sector: %s (있는 것: %s)" % (args.sector, ", ".join(SECTORS)))
        return 1
    items = load_series_list(assets_path())
    chosen = select_series(items, group=args.group, sector=args.sector)
    if not chosen:
        log("고른 series가 없음")
        return 1
    key = os.environ.get("FRED_API_KEY", "").strip()
    access = "fred-api" if key else "fredgraph.csv"
    today = dt.date.today()
    log("%d개 series를 %s로 받는다" % (len([s for s in chosen if s["source"] == "fred"]), access))
    snap, code = build_snapshot(chosen, network_loader(key), today, access,
                                {"group": args.group, "sector": args.sector})
    fred = [e for e in snap["series"].values()]
    if fred and all(e["status"] == "missing" for e in fred):
        log("모든 series 받기 실패. network를 확인한다. 예: %s" % fred[0]["note"])
        return 1
    path = args.out or snapshot_path(today)
    if os.path.exists(path):
        try:
            old = read_json(path)
            if old.get("schema") == SCHEMA:
                snap = merge_snapshot(old, snap)
        except ValueError:
            pass
    write_snapshot(snap, path)
    s = snap["summary"]
    for sid in snap["series"]:
        e = snap["series"][sid]
        if e["status"] in ("missing", "stale"):
            log("  %s %s: %s" % (e["status"], sid, e["note"]))
    log("missing %d, stale %d (웹 채우기 전 항목 포함)" % (len(s["missing"]), len(s["stale"])))
    print(path)
    return code


def cmd_web(args):
    items = {s["id"]: s for s in load_series_list(assets_path())}
    spec = items.get(args.id)
    if not spec or spec["source"] != "web":
        log("웹 채우기 항목이 아님: %s (있는 것: %s)" % (
            args.id, ", ".join(k for k, s in items.items() if s["source"] == "web")))
        return 1
    today = dt.date.today()
    return web_set(args.snapshot or snapshot_path(today), spec, today, value=args.value, as_of=args.as_of,
                   url=args.url, quote=args.quote, secondary=args.secondary, missing=args.missing)


def main(argv):
    p = argparse.ArgumentParser(prog="macro.py", description="FRED 매크로·섹터 snapshot")
    p.add_argument("--check", action="store_true", help="network 없이 fixture로 확인")
    sub = p.add_subparsers(dest="cmd")
    f = sub.add_parser("fetch", help="series를 받아 snapshot을 쓴다")
    f.add_argument("--group")
    f.add_argument("--sector")
    f.add_argument("--out")
    w = sub.add_parser("web", help="웹 채우기 값을 snapshot에 넣는다")
    w.add_argument("id")
    w.add_argument("--value")
    w.add_argument("--as-of")
    w.add_argument("--url")
    w.add_argument("--quote")
    w.add_argument("--secondary", action="store_true", help="기사·블로그 같은 2차 자료")
    w.add_argument("--missing", help="못 찾았을 때 이유")
    w.add_argument("--snapshot")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 1
    if args.check:
        return run_check()
    if args.cmd == "fetch":
        return cmd_fetch(args)
    if args.cmd == "web":
        return cmd_web(args)
    p.print_help(sys.stderr)
    return 1


# ---------------------------------------------------------------- --check

def _fixture_loader(fixture_dir):
    def load(sid, start):
        path = os.path.join(fixture_dir, sid + ".csv")
        if not os.path.exists(path):
            raise FetchError("HTTP 404")
        with open(path, encoding="utf-8") as f:
            return parse_fredgraph_csv(f.read(), sid), ""
    return load


def run_check():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    fixtures = os.path.join(root, "tests", "fixtures")
    with open(os.path.join(root, "tests", "expected.json"), encoding="utf-8") as f:
        exp = json.load(f)
    today = parse_date(exp["today"])
    fails = []

    def ok(cond, label):
        if not cond:
            fails.append(label)
        log(("PASS " if cond else "FAIL ") + label)

    # 1. assets/series.json 모양
    try:
        problems = validate_series_list(load_series_list(os.path.join(root, "assets", "series.json")))
    except Exception as e:  # noqa: BLE001
        problems = ["읽기 실패: %s" % e]
    for p in problems:
        log("  " + p)
    ok(not problems, "assets/series.json 모양")

    test_list = load_series_list(os.path.join(fixtures, "series.json"))
    ok(not validate_series_list(test_list), "tests/fixtures/series.json 모양")

    # 2. 고르기
    def ids(**kw):
        return [s["id"] for s in select_series(test_list, **kw)]
    sel = exp["select"]
    ok(ids() == sel["default"], "기본 fetch는 core만")
    ok(ids(sector="office") == sel["sector_office"], "--sector office는 core + office")
    ok(ids(group="rates") == sel["group_rates"], "--group rates")
    ok(ids(group="housing", sector="residential") == sel["sector_residential_group_housing"], "--group housing --sector residential")

    # 3. snapshot 값 (손계산과 견줌)
    chosen = select_series(test_list, sector="office") + select_series(test_list, sector="residential", group="housing")
    snap, code = build_snapshot(chosen, _fixture_loader(fixtures), today, "fixture",
                                request={"group": None, "sector": "office"})
    keys = ["value", "status", "as_of", "chg_1m", "chg_3m", "chg_12m", "yoy_pct", "pctile_10y",
            "ref_1m", "ref_3m", "ref_12m", "derived_status"]
    for sid, want in exp["series"].items():
        got = snap["series"].get(sid)
        if got is None:
            ok(False, "%s: snapshot에 없음" % sid)
            continue
        flat = dict(got)
        d = got.get("derived", {})
        flat.update({k: d.get(k) for k in ("chg_1m", "chg_3m", "chg_12m", "yoy_pct", "pctile_10y")})
        flat.update({"ref_" + k: (d.get("ref") or {}).get(k) for k in ("1m", "3m", "12m")})
        flat["derived_status"] = d.get("status")
        bad = [k for k in keys if not _same(flat.get(k), want[k])]
        ok(not bad, "%s 값 %s" % (sid, "" if not bad else "다름: " + ", ".join(
            "%s got=%r want=%r" % (k, flat.get(k), want[k]) for k in bad)))
        if want["status"] == "missing":
            ok(bool(got.get("note")), "%s: missing 이유가 note에 있음" % sid)
        if want["status"] == "reported":
            ok(bool(got.get("source")) and bool(got.get("locator")), "%s: reported에 source·locator" % sid)
    ok("분기" in snap["series"]["RRVRUSQ156N"]["derived"]["note"], "분기 자료의 1개월 변화 없음을 note에 적음")
    ok(snap.get("schema") == SCHEMA and list(snap.keys())[0] == "schema", "snapshot 첫 칸이 schema")
    for wid, want in exp["web_placeholder"].items():
        w = snap["web"].get(wid, {})
        ok(w.get("value") is None and w.get("status") == want["status"] and bool(w.get("note")),
           "%s: 웹 채우기 전 자리(missing)" % wid)
    ok(snap["sectors"].get("office") == ["USPBS", "ECOMPCTSA"], "sectors.office 목록")
    ok(code == exp["exit_code"], "FRED missing이 있으면 exit 2 (got %s)" % code)

    # 4. 공식 API JSON 읽기
    with open(os.path.join(fixtures, "api_DGS10.json"), encoding="utf-8") as f:
        obs = parse_fred_api_json(f.read())
    want = exp["api_parse"]
    ok([d.isoformat() for d, _ in obs] == want["dates"] and [v for _, v in obs] == want["values"], "공식 API JSON 읽기")

    # 5. 웹 채우기와 같은 날 합치기 (임시 폴더)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "snap.json")
        write_snapshot(snap, path)
        spec = {s["id"]: s for s in test_list}["ISM_MFG_PMI"]
        e1 = web_set(path, spec, today, value="48.7", as_of="2026-10", url="", quote="x")
        e2 = web_set(path, spec, today, value="48.7", as_of="2026-10", url="https://example.org/pmi (가상)", quote="")
        ok(e1 == 1 and e2 == 1, "URL이나 원문 문장이 없으면 웹 값을 거절")
        e3 = web_set(path, spec, today, value="48.7", as_of="2026-10",
                     url="https://www.ismworld.org/ (가상)", quote="The Manufacturing PMI registered 48.7 percent (가상)")
        w = read_json(path)["web"]["ISM_MFG_PMI"]
        ok(e3 == 0 and w["value"] == 48.7 and w["status"] == "reported" and w["as_of"] == "2026-10-01"
           and w["source"].startswith("web:https://") and w["locator"].startswith("The Manufacturing"),
           "1차 자료 웹 값은 reported, source=web:URL, locator=원문")
        web_set(path, spec, today, value="48.7", as_of="2026-10", url="https://news.example.com/x (가상)",
                quote="PMI came in at 48.7 (가상)", secondary=True)
        ok(read_json(path)["web"]["ISM_MFG_PMI"]["status"] == "estimated", "2차 자료 웹 값은 estimated")
        web_set(path, spec, today, value="48.7", as_of="2026-08", url="https://www.ismworld.org/ (가상)", quote="q (가상)")
        ok(read_json(path)["web"]["ISM_MFG_PMI"]["status"] == "stale", "오래된 웹 값은 stale")
        web_set(path, spec, today, missing="ISM 발표문을 찾지 못함")
        w = read_json(path)["web"]["ISM_MFG_PMI"]
        ok(w["value"] is None and w["status"] == "missing" and w["note"] == "ISM 발표문을 찾지 못함", "--missing")
        web_set(path, spec, today, value="48.7", as_of="2026-10", url="https://www.ismworld.org/ (가상)", quote="q (가상)")
        again, _ = build_snapshot(select_series(test_list), _fixture_loader(fixtures), today, "fixture",
                                  request={"group": None, "sector": None})
        merged = merge_snapshot(read_json(path), again)
        ok("USPBS" in merged["series"] and merged["web"]["ISM_MFG_PMI"]["value"] == 48.7
           and merged["requests"][-1] == {"group": None, "sector": None},
           "같은 날 다시 받으면 다른 series와 웹 값을 남김")

        def down(sid, start):
            raise FetchError("network 오류 (가상)")
        failed, _ = build_snapshot(select_series(test_list, sector="office"), down, today, "fixture",
                                   request={"group": None, "sector": "office"})
        merged = merge_snapshot(merged, failed)
        ok(merged["series"]["USPBS"]["status"] == "reported", "같은 날 받기 실패가 앞서 받은 값을 덮지 않음")

    # 6. 입력 오류는 exit 1
    ok(main(["fetch", "--sector", "casino"]) == 1, "모르는 sector는 exit 1")
    ok(main(["fetch", "--group", "weather"]) == 1, "모르는 group은 exit 1")

    log("check: %d개 실패" % len(fails) if fails else "check: 모두 통과")
    return 2 if fails else 0


def _same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and abs(a - b) < 1e-9
    return a == b


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
