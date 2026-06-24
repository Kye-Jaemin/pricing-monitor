"""번들 구성요소 정가가 왜 합산에 안 들어가는지 진단.

사용법(앱을 띄우는 것과 '같은 폴더/같은 방식'으로 실행):
    python -m scripts.diag_bundle_price mybox
인자(검색어)는 서비스 이름 일부. 없으면 'mybox'.
출력은 ASCII 위주(콘솔 인코딩 안전).
"""
import sys
from pathlib import Path

from app import config
from app.core import store, presenters


def main() -> None:
    q = (sys.argv[1] if len(sys.argv) > 1 else "mybox").lower()
    print("=" * 60)
    print("DB_PATH(config) :", config.DB_PATH)
    print("DB_PATH(absolute):", Path(config.DB_PATH).expanduser().resolve())
    print("exists          :", Path(config.DB_PATH).expanduser().resolve().exists())
    print("search term     :", q)
    print("=" * 60)

    allc = store.list_companies(active_only=False)
    print(f"total companies : {len(allc)}")
    hits = [c for c in allc if q in c["name"].lower()]
    if not hits:
        print(f"!! '{q}' 를 이름에 포함하는 업체가 없습니다.")
        print("   등록된 구성요소(component) 목록:")
        for c in allc:
            if c["is_component"]:
                print("    -", c["name"])
        return

    smap = presenters._standalone_usd_map()
    for c in hits:
        print("-" * 60)
        print("company   :", c["name"])
        print("  flags   : component=%s bundle=%s active=%s" % (
            c["is_component"], c["is_bundle"], c["active"]))
        rows = store.latest_snapshots_for_company(c["name"])
        print("  snapshots:", len(rows))
        tiers = presenters._company_plan_tiers(c["name"])
        if not tiers:
            print("  tiers   : (none)  <-- 수집/추출에서 가격 티어가 안 나옴")
        for t in tiers:
            print("  tier    : name=%r free=%s monthly=%s annual=%s" % (
                t["name"], t["is_free"], t["monthly"], t["annual"]))
        key = presenters._normalize_feature(c["name"])
        print("  norm key:", key)
        print("  smap[key]=", smap.get(key), "(USD)")
        print("  match_standalone(name)=", presenters._match_standalone(c["name"], smap))

    print("=" * 60)
    print("번들 분석에 들어있는 서비스 이름 / list_price / 매칭:")
    for c in allc:
        if not c["is_bundle"]:
            continue
        row = store.get_bundle_analysis(c["name"])
        if not row:
            continue
        import json
        try:
            payload = json.loads(row["payload_json"]) or {}
        except (ValueError, TypeError):
            continue
        for p in payload.get("plans", []):
            for s in p.get("services", []):
                nm = (s.get("name") or "")
                if q not in nm.lower():
                    continue
                m = presenters._match_standalone(nm, smap)
                print("  [%s] svc=%r choice=%s list_price=%s -> match=%s" % (
                    c["name"], nm, s.get("choice"), s.get("list_price"), m))


if __name__ == "__main__":
    main()
