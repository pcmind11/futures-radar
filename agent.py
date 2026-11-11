"""미래 레이더 - 수집·종합 에이전트 (표준 라이브러리만 사용)"""
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).parent
KST = dt.timezone(dt.timedelta(hours=9))
API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
MODEL = os.environ.get("GEMINI_MODEL", "").strip()
MAX_PER_QUERY = 10
DATA_DIR = ROOT / "_data" / "signals"


def now():
    return dt.datetime.now(KST)


def front(title, kind, day, summary=""):
    lines = ["---",
             f"layout: {'daily' if kind == 'daily' else 'report'}",
             f"type: {kind}",
             f'day: "{day}"',
             f"title: {json.dumps(title, ensure_ascii=False)}"]
    if summary:
        lines.append(f"summary: {json.dumps(summary, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def pub_day(s):
    try:
        return parsedate_to_datetime(s).astimezone(KST).strftime("%Y-%m-%d")
    except Exception:
        return ""


def glance_lines(report):
    m = re.search(r"##\s*이번 주 한눈에\s*\n(.*?)(?=\n##\s|\Z)", report, re.S)
    if not m:
        return []
    out = []
    for ln in m.group(1).splitlines():
        ln = re.sub(r"\[\d{6}-\d{2}\]\([^)]*\)", "", ln)
        ln = re.sub(r"\d{6}-\d{2}", "", ln)
        ln = re.sub(r"\[\s*\]", "", ln)
        ln = re.sub(r"^\s*[-*•]\s*", "", ln)
        ln = re.sub(r"\*\*|__|`", "", ln)
        ln = re.sub(r"\s+([.,])", r"\1", ln)
        ln = re.sub(r"\s{2,}", " ", ln).strip()
        if ln:
            out.append(ln)
    return out[:5]


FALLBACK_MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash"]


def model_candidates():
    return [m for m in dict.fromkeys([MODEL] + FALLBACK_MODELS) if m]


def gemini(system_prompt, user_text, as_json=False):
    if not API_KEY:
        sys.exit("GEMINI_API_KEY(Secret) 설정이 없습니다.")
    cfg = {"temperature": 0.3}
    if as_json:
        cfg["responseMimeType"] = "application/json"
    body = json.dumps({
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": cfg,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "x-goog-api-key": API_KEY}
    last = ""
    for model in model_candidates():
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for attempt in range(4):
            data = None
            try:
                req = urllib.request.Request(url, data=body, headers=headers)
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = json.load(r)
                parts = data["candidates"][0]["content"]["parts"]
                if model != MODEL:
                    print(f"모델 '{MODEL}' 대신 '{model}' 사용")
                return "".join(p.get("text", "") for p in parts)
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "ignore")[:300]
                if e.code in (429, 500, 503) and attempt < 3:
                    time.sleep(30 * (attempt + 1))
                    continue
                last = f"{model} → 오류 {e.code}: {msg}"
                print(last)
                break
            except (KeyError, IndexError):
                last = f"{model} → 응답 형식 오류: {str(data)[:300]}"
                print(last)
                break
    sys.exit("Gemini 호출 실패. " + last)


def fetch_news(query, lang):
    loc = "hl=ko&gl=KR&ceid=KR:ko" if lang == "ko" else "hl=en-US&gl=US&ceid=US:en"
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query + " when:2d") + "&" + loc)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            root = ET.fromstring(r.read())
    except Exception as e:
        print("뉴스 수집 실패:", query, e)
        return []
    out = []
    for it in root.iter("item"):
        out.append({
            "title": (it.findtext("title") or "").strip(),
            "link": (it.findtext("link") or "").strip(),
            "date": (it.findtext("pubDate") or "").strip(),
            "source": (it.findtext("source") or "").strip(),
            "lang": lang,
        })
        if len(out) >= MAX_PER_QUERY:
            break
    return out


def run_daily():
    today = now().strftime("%Y-%m-%d")
    tag = now().strftime("%y%m%d")
    cfg = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    cands, seen = [], set()
    for domain, queries in cfg["domains"].items():
        for q in queries:
            for it in fetch_news(q["q"], q["lang"]):
                key = re.sub(r"\W+", "", it["title"].lower())[:60]
                if key and key not in seen:
                    seen.add(key)
                    it["domain"] = domain
                    cands.append(it)
    if not cands:
        sys.exit("수집된 항목이 없습니다. 실행 방식을 check로 돌려 원인을 확인하세요.")
    lines = [f'[{i}] {c["domain"]} | {c["title"]} | {c["source"]} | {c["date"]} | {c["lang"]}'
             for i, c in enumerate(cands)]
    raw = gemini((ROOT / "prompts/daily.md").read_text(encoding="utf-8"),
                 "\n".join(lines), as_json=True)
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        picked = json.loads(cleaned)
    except json.JSONDecodeError:
        sys.exit("JSON 해석 실패: " + raw[:300])
    if isinstance(picked, dict):
        picked = next((v for v in picked.values() if isinstance(v, list)), [])
    items, used = [], set()
    for p in picked:
        try:
            idx = int(p["id"])
            c = cands[idx]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if idx in used:
            continue
        used.add(idx)
        items.append({
            "id": f"{tag}-{len(items) + 1:02d}",
            "domain": c["domain"],
            "ko_title": p.get("ko_title") or c["title"],
            "summary": p.get("summary", ""),
            "speaker": p.get("speaker", "불명"),
            "horizon": p.get("horizon", "불명"),
            "kind": p.get("kind", "기타"),
            "source": c["source"],
            "date": c["date"],
            "published": pub_day(c["date"]),
            "day": today,
            "link": c["link"],
        })
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (ROOT / "daily").mkdir(exist_ok=True)
    (DATA_DIR / f"{today}.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    md = [front(f"{today} 일일 신호", "daily", today)]
    for domain in cfg["domains"]:
        group = [i for i in items if i["domain"] == domain]
        if not group:
            continue
        md.append(f"\n## {domain}\n")
        for i in group:
            e = html.escape
            md.append(f'- **{e(i["ko_title"])}** `{i["horizon"]}` `{i["kind"]}`  \n'
                      f'  {e(i["summary"])} (발언자: {e(i["speaker"])}) — '
                      f'[{e(i["source"] or "원문")}]({i["link"]}) · {i["id"]}')
    md.append("\n> 헤드라인만 보고 만든 요약이라 근거 강도는 '낮음'입니다. 중요한 내용은 원문으로 확인하세요.")
    (ROOT / f"daily/{today}.md").write_text("\n".join(md), encoding="utf-8")
    print(f"일일 신호 {len(items)}건 저장")


def run_weekly():
    files = sorted(DATA_DIR.glob("*.json"))[-7:] if DATA_DIR.exists() else []
    items = [it for f in files for it in json.loads(f.read_text(encoding="utf-8"))]
    if not items:
        print("주간 리포트: 데이터가 없어 건너뜁니다.")
        return
    by_id = {i["id"]: i for i in items}
    text = "\n".join(
        f'[{i["id"]}] {i["domain"]} | {i["ko_title"]} | {i["summary"]} | '
        f'발언자:{i["speaker"]} | 시점:{i["horizon"]} | 유형:{i["kind"]} | {i["source"]}'
        for i in items)
    report = gemini((ROOT / "prompts/weekly.md").read_text(encoding="utf-8"), text)

    def link_group(m):
        ids = re.findall(r"\d{6}-\d{2}", m.group(0))
        return " ".join(f"[{i}]({by_id[i]['link']})" if i in by_id else i for i in ids)

    report = re.sub(r"\[\s*\d{6}-\d{2}(?:\s*,\s*\d{6}-\d{2})*\s*\](?!\()", link_group, report)
    d = now().strftime("%Y-%m-%d")
    (ROOT / "reports").mkdir(exist_ok=True)
    summary = " ".join(glance_lines(report))[:300]
    (ROOT / f"reports/{d}-weekly.md").write_text(
        front(f"{d} 주간 리포트", "weekly", d, summary) + "\n" + report, encoding="utf-8")
    print("주간 리포트 저장")


def write_recent():
    files = sorted(DATA_DIR.glob("*.json"), reverse=True)[:30] if DATA_DIR.exists() else []
    recent = []
    for f in files:
        for it in json.loads(f.read_text(encoding="utf-8")):
            it.setdefault("day", f.stem)
            recent.append(it)
    (ROOT / "_data").mkdir(exist_ok=True)
    (ROOT / "_data/recent.json").write_text(
        json.dumps(recent, ensure_ascii=False, indent=1), encoding="utf-8")


def write_status():
    files = sorted(DATA_DIR.glob("*.json")) if DATA_DIR.exists() else []
    latest_day = files[-1].stem if files else ""
    count = len(json.loads(files[-1].read_text(encoding="utf-8"))) if files else 0
    weekly = None
    rdir = ROOT / "reports"
    reps = sorted(rdir.glob("*-weekly.md")) if rdir.exists() else []
    if reps:
        text = reps[-1].read_text(encoding="utf-8")
        body = re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S)
        weekly = {"date": reps[-1].stem.replace("-weekly", ""),
                  "url": f"/reports/{reps[-1].stem}.html",
                  "lines": glance_lines(body)}
    status = {
        "last_run_kst": now().strftime("%Y-%m-%d %H:%M"),
        "latest_day": latest_day,
        "daily_count": count,
        "schedule": "매일 한국시간 07:00 자동 수집 · 주 1회(일요일) 주간 리포트",
        "latest_weekly": weekly,
    }
    (ROOT / "_data").mkdir(exist_ok=True)
    (ROOT / "_data/status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")


def run_check():
    print("== 점검 모드 ==")
    print("API 키 설정:", "있음" if API_KEY else "없음")
    print("모델 변수:", MODEL or "(비어 있음 → 기본 후보 사용)")
    for p in ["sources.json", "prompts/daily.md", "prompts/weekly.md",
              "_config.yml", "index.html", "assets/style.css",
              "_layouts/default.html", "_layouts/daily.html",
              "_layouts/report.html", "_layouts/horizon.html", "_includes/card.html"]:
        print("파일", p, "있음" if (ROOT / p).exists() else "없음!")
    cfg = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))
    total = 0
    for domain, queries in cfg["domains"].items():
        for q in queries:
            n = len(fetch_news(q["q"], q["lang"]))
            total += n
            print(f"  뉴스 {n}건 | {domain} | {q['q'][:40]}")
    print("뉴스 합계:", total)
    print("Gemini 응답:", gemini("한 단어로만 답하라.", "ping").strip()[:50])
    if total == 0:
        sys.exit("뉴스가 0건입니다.")
    print("점검 통과")


if __name__ == "__main__":
    mode = os.environ.get("MODE", "").strip() or "normal"
    if mode == "check":
        run_check()
    else:
        run_daily()
        if now().weekday() == 6 or mode == "weekly":
            run_weekly()
        write_recent()
        write_status()
