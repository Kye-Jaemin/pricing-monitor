"""공급 측 피쳐 가치 분석 (Feature Value Analysis).

수집된 snapshots(업체 × 티어 × 피쳐 × 가격)만으로 각 피쳐가 시장에서 어떻게
값매겨지는지 정량 분석한다. 수요 측(설문/리뷰)은 사용하지 않는다. 결과는
"시장이 이 피쳐를 어떻게 취급하는가"이며 실제 고객 지불의사(WTP)가 아니다.

지표
  ① 보급도   penetration / entry_inclusion
  ② 포지셔닝 unlock_price(중앙값) / gating_depth
  ③ 한계가격 NNLS 회귀로 인접 티어 차이를 피쳐(또는 묶음) 단위로 분해
2축(value/commodity) → 4분면 + 공급 측 내부 교차검증 confidence.

프레임워크 비의존. DB 접근은 store.py 만 사용한다.
CLI:  python -m app.core.value_analysis  → 표 + JSON + CSV 저장
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

from .. import config
from . import store
from .models import PricingSnapshot

DISCLAIMER = (
    "공급 측 가격 책정 행동에서 역산한 가치 신호이며, 실제 고객 지불의사가 아님."
)
DISCLAIMER_EN = (
    "Value signals reverse-engineered from supply-side pricing behavior — "
    "not actual customer willingness-to-pay."
)

MIN_SUPPORT = 3  # marginal 회귀에서 피쳐 등장 row 수 하한
# value_score 가중치 (가용 성분만으로 재정규화)
W_UNLOCK = 0.4
W_MARGINAL = 0.4
W_GATING = 0.2


# ── 0단계: 피쳐 정규화 (canonical_feature_id) ────────────────
# 결정적 슬러그 + 경량 동의어 통일. 저장된 매핑(feature_canonical)이 있으면 우선.
_SYNONYMS = [
    # (정규식, canonical_id) — 의미 단위로 자주 갈리는 표현을 통일
    (r"\b(sso|single sign[- ]?on|saml|scim)\b", "sso_saml"),
    (r"\b(audit log|audit trail|감사 ?로그)\b", "audit_log"),
    (r"\b(api|rest api|developer api|api access)\b", "api_access"),
    (r"\b(webhook)s?\b", "webhooks"),
    (r"\b(priority support|우선 ?지원)\b", "priority_support"),
    (r"\b(sla)\b", "sla"),
    (r"\b(unlimited|무제한)\b.*\b(storage|용량)\b", "unlimited_storage"),
    (r"\b(custom branding|white[- ]?label|화이트라벨)\b", "white_label"),
    (r"\b(custom domain|사용자 ?도메인)\b", "custom_domain"),
    (r"\b(2fa|mfa|two[- ]?factor)\b", "mfa"),
    (r"\b(analytics|analytic|분석|insight)\b", "analytics"),
    (r"\b(export|내보내기|csv export)\b", "data_export"),
]


def _slug(text: str) -> str:
    s = (text or "").strip().lower()
    s = re.sub(r"\(.*?\)", " ", s)          # 괄호 주석 제거
    s = re.sub(r"[^0-9a-z가-힣]+", "_", s)   # 비단어 → _
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "unknown"


def canonicalize_feature(raw: str, overrides: dict[str, str] | None = None) -> str:
    """원문 피쳐 → canonical_feature_id. 저장된 매핑 > 동의어 규칙 > 슬러그."""
    if overrides and raw in overrides:
        return overrides[raw]
    low = (raw or "").lower()
    for pat, cid in _SYNONYMS:
        if re.search(pat, low):
            return cid
    return _slug(raw)


# ── 입력 준비: 업체별 최신 스냅샷 → 정규화 티어 ──────────────
@dataclass
class NormTier:
    name: str
    price_pm: float | None        # 좌석당 월요금 (USD). 미공개면 None
    features: frozenset[str]      # canonical id 집합
    is_disclosed: bool
    billing_unit: str
    flat: bool                    # 좌석당 환산 불가(flat) 여부


@dataclass
class CompanyData:
    company: str
    tiers: list[NormTier]         # 가격 오름차순(미공개는 뒤)
    raw_labels: dict[str, set] = field(default_factory=dict)  # cid -> 원문들


def _norm_price(t) -> float | None:
    """월 단위 좌석당 가격: annual_price_per_month 우선, 없으면 monthly_price."""
    p = t.annual_price_per_month if t.annual_price_per_month is not None else t.monthly_price
    return float(p) if p is not None else None


def _is_free(t) -> bool:
    return (t.monthly_price == 0) or ("free" in t.name.lower()) or ("무료" in t.name)


def load_companies() -> list[CompanyData]:
    """업체별 최신 스냅샷(가장 티어가 풍부한 소스)을 정규화해 반환."""
    overrides = store.get_feature_canonical_map()
    by_company: dict[str, list] = {}
    for row in store.all_latest_by_source():
        by_company.setdefault(row["company"], []).append(row)

    out: list[CompanyData] = []
    for company, rows in by_company.items():
        # 소스가 여럿이면 티어 수가 가장 많은(정보가 풍부한) 스냅샷을 대표로 사용
        best = None
        best_snap = None
        for r in rows:
            snap = PricingSnapshot.from_payload_json(r["payload_json"])
            if best_snap is None or len(snap.tiers) > len(best_snap.tiers):
                best, best_snap = r, snap
        if best_snap is None or not best_snap.tiers:
            continue

        raw_labels: dict[str, set] = {}
        norm_tiers: list[NormTier] = []
        for t in best_snap.tiers:
            cids = set()
            for f in t.features:
                cid = canonicalize_feature(f, overrides)
                cids.add(cid)
                raw_labels.setdefault(cid, set()).add(f)
            price = _norm_price(t)
            flat = t.billing_unit == "flat"
            norm_tiers.append(
                NormTier(
                    name=t.name,
                    price_pm=price,
                    features=frozenset(cids),
                    is_disclosed=price is not None,
                    billing_unit=t.billing_unit,
                    flat=flat,
                )
            )
        # 가격 오름차순(미공개 None은 뒤로)
        norm_tiers.sort(key=lambda nt: (nt.price_pm is None, nt.price_pm or 0.0))
        out.append(CompanyData(company=company, tiers=norm_tiers, raw_labels=raw_labels))
    return out


# ── 통계 헬퍼 ────────────────────────────────────────────────
def _minmax_normalize(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    lo, hi = min(values.values()), max(values.values())
    if hi - lo < 1e-12:
        return {k: 0.5 for k in values}
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


def _percentile_rank(values: dict[str, float]) -> dict[str, float]:
    """각 값의 백분위(0..1). 동률은 평균 순위."""
    if not values:
        return {}
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    if n == 1:
        return {items[0][0]: 0.5}
    out: dict[str, float] = {}
    for i, (k, _) in enumerate(items):
        out[k] = i / (n - 1)
    return out


def _tertile_vote(values: dict[str, float], invert: bool = False) -> dict[str, int]:
    """상위 1/3 → +1, 하위 1/3 → -1, 중간 → 0. invert면 부호 반전(희소=신호)."""
    if not values:
        return {}
    ranked = _percentile_rank(values)
    out: dict[str, int] = {}
    for k, p in ranked.items():
        v = 1 if p >= 2 / 3 else (-1 if p <= 1 / 3 else 0)
        out[k] = -v if invert else v
    return out


# ── ① 보급도 ─────────────────────────────────────────────────
def _prevalence(companies: list[CompanyData]):
    n = len(companies)
    providers: dict[str, int] = {}
    entry_hit: dict[str, int] = {}
    for c in companies:
        union = set().union(*[t.features for t in c.tiers]) if c.tiers else set()
        for f in union:
            providers[f] = providers.get(f, 0) + 1
        # 가장 싼 티어(공개가 우선, 없으면 첫 티어)의 피쳐
        disclosed = [t for t in c.tiers if t.is_disclosed]
        entry_tier = (disclosed or c.tiers)[0]
        for f in entry_tier.features:
            entry_hit[f] = entry_hit.get(f, 0) + 1
    penetration = {f: providers[f] / n for f in providers} if n else {}
    entry_inclusion = {
        f: entry_hit.get(f, 0) / providers[f] for f in providers
    }
    return providers, penetration, entry_inclusion


# ── ② 포지셔닝 ───────────────────────────────────────────────
def _positioning(companies: list[CompanyData]):
    unlock_by_feat: dict[str, list[float]] = {}
    gating_by_feat: dict[str, list[float]] = {}
    for c in companies:
        disclosed = [t for t in c.tiers if t.is_disclosed]
        # unlock: 피쳐를 포함한 공개 티어들의 최저가
        per_feat_min: dict[str, float] = {}
        for t in disclosed:
            for f in t.features:
                if f not in per_feat_min or t.price_pm < per_feat_min[f]:
                    per_feat_min[f] = t.price_pm
        for f, p in per_feat_min.items():
            unlock_by_feat.setdefault(f, []).append(p)

        # gating depth: 티어 ≥2 업체만. 전 티어(가격순) 기준 최초 등장 index
        if len(c.tiers) >= 2:
            denom = len(c.tiers) - 1
            seen: set[str] = set()
            for idx, t in enumerate(c.tiers):
                for f in t.features:
                    if f not in seen:
                        seen.add(f)
                        gating_by_feat.setdefault(f, []).append(idx / denom)

    unlock_price = {f: median(v) for f, v in unlock_by_feat.items()}
    gating_depth = {f: sum(v) / len(v) for f, v in gating_by_feat.items()}
    gating_coverage = {f: len(v) for f, v in gating_by_feat.items()}
    return unlock_price, gating_depth, gating_coverage


# ── ③ 한계 가격 기여 (NNLS) ──────────────────────────────────
def _marginal_pricing(companies: list[CompanyData]):
    """인접 티어 차이를 피쳐(공선 묶음 단위) 회귀. 반환: marginal, support, bundles."""
    import numpy as np
    from scipy.optimize import nnls

    rows: list[tuple[float, frozenset]] = []
    for c in companies:
        disclosed = [t for t in c.tiers if t.is_disclosed]
        if not disclosed:
            continue
        # base: 빈 묶음 → entry 티어
        rows.append((disclosed[0].price_pm, disclosed[0].features))
        # 계단: 인접쌍의 "값 상승 vs 새로 추가된 피쳐"
        for k in range(len(disclosed) - 1):
            added = disclosed[k + 1].features - disclosed[k].features
            delta = disclosed[k + 1].price_pm - disclosed[k].price_pm
            if added:
                rows.append((delta, added))

    feats = sorted({f for _, added in rows for f in added})
    if not rows or not feats:
        return {}, {}, {}

    fidx = {f: j for j, f in enumerate(feats)}
    X = np.zeros((len(rows), len(feats)))
    y = np.zeros(len(rows))
    for i, (delta, added) in enumerate(rows):
        y[i] = delta
        for f in added:
            X[i, fidx[f]] = 1.0

    support = {f: int(X[:, fidx[f]].sum()) for f in feats}

    # 공선 묶음: 동일 컬럼 벡터(항상 함께 등장/부재)인 피쳐들을 하나로 합친다
    col_groups: dict[tuple, list[str]] = {}
    for f in feats:
        key = tuple(X[:, fidx[f]].tolist())
        col_groups.setdefault(key, []).append(f)

    bundle_of: dict[str, str] = {}
    bundle_members: dict[str, list[str]] = {}
    reduced_cols: list[tuple] = []
    bundle_ids: list[str] = []
    for key, members in col_groups.items():
        bid = "+".join(members) if len(members) > 1 else members[0]
        reduced_cols.append(key)
        bundle_ids.append(bid)
        bundle_members[bid] = members
        for m in members:
            bundle_of[m] = bid

    # 축약 설계행렬(묶음당 1열) — 열 순서가 bundle_ids 와 정확히 정렬
    Xr = np.array(reduced_cols).T if reduced_cols else np.zeros((len(rows), 0))
    beta, _ = nnls(Xr, y)

    bundle_beta = {bid: float(b) for bid, b in zip(bundle_ids, beta)}
    marginal = {f: bundle_beta[bundle_of[f]] for f in feats}
    return marginal, support, bundle_members


# ── 종합 ─────────────────────────────────────────────────────
@dataclass
class FeatureRow:
    canonical_feature: str
    raw_examples: list[str]
    providers: int
    penetration: float
    entry_inclusion: float
    unlock_price_median: float | None
    gating_depth: float | None
    gating_coverage: int
    marginal_usd: float | None
    bundle_id: str | None
    value_score: float
    commodity_score: float
    quadrant: str
    quadrant_en: str
    quadrant_key: str
    confidence: str
    flags: list[str]


# (value, commodity) → (key, 한국어, 영어)
QUADRANTS = {
    ("hi", "lo"): ("diff", "수익화 가능한 차별화", "Monetizable differentiator"),
    ("hi", "hi"): ("lever", "표준 수익 레버", "Standard revenue lever"),
    ("lo", "hi"): ("stakes", "기본기(필수)", "Table stakes"),
    ("lo", "lo"): ("niche", "실험적/틈새", "Experimental / niche"),
}


def analyze() -> dict:
    """전체 분석 실행 → 피쳐별 행 + 메타(중앙값/면책 등)."""
    companies = load_companies()
    n_companies = len(companies)

    providers, penetration, entry_inclusion = _prevalence(companies)
    unlock_price, gating_depth, gating_coverage = _positioning(companies)
    marginal, support, bundle_members = _marginal_pricing(companies)

    # canonical → 원문 예시(여러 업체 표현 합집합)
    raw_map: dict[str, set] = {}
    enterprise_gated: set[str] = set()
    flat_feats: set[str] = set()
    for c in companies:
        for cid, raws in c.raw_labels.items():
            raw_map.setdefault(cid, set()).update(raws)
        for t in c.tiers:
            if not t.is_disclosed:
                enterprise_gated |= set(t.features)
            if t.flat:
                flat_feats |= set(t.features)

    all_feats = sorted(providers.keys())

    # 정규화 성분
    unlock_pctile = _percentile_rank(unlock_price)        # 0..1 (공개가 있는 피쳐만)
    marginal_norm = _minmax_normalize(
        {f: marginal[f] for f in marginal if support.get(f, 0) >= MIN_SUPPORT}
    )

    # 묶음 역참조: feature → bundle_id(멤버 ≥2)
    feat_bundle: dict[str, str] = {}
    for bid, members in bundle_members.items():
        if len(members) > 1:
            for m in members:
                feat_bundle[m] = bid

    # value_score: 가용 성분만으로 가중 재정규화
    value_score: dict[str, float] = {}
    for f in all_feats:
        parts: list[tuple[float, float]] = []
        if f in unlock_pctile:
            parts.append((W_UNLOCK, unlock_pctile[f]))
        if f in marginal_norm:
            parts.append((W_MARGINAL, marginal_norm[f]))
        if f in gating_depth:
            parts.append((W_GATING, gating_depth[f]))
        if parts:
            wsum = sum(w for w, _ in parts)
            value_score[f] = sum(w * v for w, v in parts) / wsum
        else:
            value_score[f] = 0.0

    commodity_score = {
        f: 0.5 * penetration.get(f, 0.0) + 0.5 * entry_inclusion.get(f, 0.0)
        for f in all_feats
    }

    v_med = median(value_score.values()) if value_score else 0.5
    c_med = median(commodity_score.values()) if commodity_score else 0.5

    # confidence: 세 렌즈(unlock↑, marginal↑, penetration↓) 방향 일치도
    vote_unlock = _tertile_vote(unlock_price)
    vote_marginal = _tertile_vote(
        {f: marginal[f] for f in marginal if support.get(f, 0) >= MIN_SUPPORT}
    )
    vote_penet = _tertile_vote(penetration, invert=True)

    rows: list[FeatureRow] = []
    for f in all_feats:
        flags: list[str] = []
        if f in enterprise_gated:
            flags.append("enterprise_gated")
        if f in feat_bundle:
            flags.append(f"collinear_bundle:{feat_bundle[f]}")
        if f in marginal and support.get(f, 0) < MIN_SUPPORT:
            flags.append("low_support")
        if f in flat_feats:
            flags.append("flat_billing")

        votes = [v for v in (vote_unlock.get(f), vote_marginal.get(f),
                             vote_penet.get(f)) if v is not None]
        nonzero = [v for v in votes if v != 0]
        if len(votes) >= 2 and nonzero and all(v > 0 for v in nonzero) and \
                len(nonzero) == len(votes):
            confidence = "high"
        elif len(votes) >= 2 and nonzero and all(v < 0 for v in nonzero) and \
                len(nonzero) == len(votes):
            confidence = "high"
        elif any(v > 0 for v in votes) and any(v < 0 for v in votes):
            confidence = "low"
            flags.append("가격이 잘 분리 안 되는 묶음형")
        else:
            confidence = "medium"
        # ②③ 신뢰도 자동 down: gating 근거 빈약 + marginal 미지원
        if gating_coverage.get(f, 0) < 2 and support.get(f, 0) < MIN_SUPPORT \
                and confidence == "high":
            confidence = "medium"

        vq = "hi" if value_score[f] >= v_med else "lo"
        cq = "hi" if commodity_score[f] >= c_med else "lo"
        q_key, q_ko, q_en = QUADRANTS[(vq, cq)]

        rows.append(
            FeatureRow(
                canonical_feature=f,
                raw_examples=sorted(raw_map.get(f, []))[:5],
                providers=providers[f],
                penetration=round(penetration.get(f, 0.0), 4),
                entry_inclusion=round(entry_inclusion.get(f, 0.0), 4),
                unlock_price_median=(round(unlock_price[f], 2)
                                     if f in unlock_price else None),
                gating_depth=(round(gating_depth[f], 4) if f in gating_depth else None),
                gating_coverage=gating_coverage.get(f, 0),
                marginal_usd=(round(marginal[f], 2) if f in marginal else None),
                bundle_id=feat_bundle.get(f),
                value_score=round(value_score[f], 4),
                commodity_score=round(commodity_score[f], 4),
                quadrant=q_ko,
                quadrant_en=q_en,
                quadrant_key=q_key,
                confidence=confidence,
                flags=flags,
            )
        )

    # 정렬: value_score 내림차순
    rows.sort(key=lambda r: r.value_score, reverse=True)

    return {
        "disclaimer": DISCLAIMER,
        "disclaimer_en": DISCLAIMER_EN,
        "n_companies": n_companies,
        "value_median": round(v_med, 4),
        "commodity_median": round(c_med, 4),
        "rows": [r.__dict__ for r in rows],
    }


# ── CLI ──────────────────────────────────────────────────────
_COLS = [
    "canonical_feature", "penetration", "entry_inclusion", "unlock_price_median",
    "gating_depth", "gating_coverage", "marginal_usd", "bundle_id",
    "value_score", "commodity_score", "quadrant", "confidence", "flags",
]


def _write_outputs(result: dict, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "value_analysis.json"
    csv_path = out_dir / "value_analysis.csv"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(_COLS)
        for r in result["rows"]:
            w.writerow([
                r["canonical_feature"], r["penetration"], r["entry_inclusion"],
                r["unlock_price_median"], r["gating_depth"], r["gating_coverage"],
                r["marginal_usd"], r["bundle_id"], r["value_score"],
                r["commodity_score"], r["quadrant"], r["confidence"],
                ";".join(r["flags"]),
            ])
    return json_path, csv_path


def main() -> int:
    store.init_db()
    result = analyze()
    print("=== Feature Value Analysis (supply-side) ===")
    print(f"DB_PATH    = {config.DB_PATH}")
    print(f"업체 수    = {result['n_companies']}  ·  피쳐 수 = {len(result['rows'])}")
    print(f"면책       = {result['disclaimer']}")
    print("-" * 100)
    hdr = (f"{'feature':<24}{'pen':>6}{'entry':>7}{'unlock':>8}"
           f"{'gat':>6}{'marg':>8}  {'V':>5}{'C':>6}  {'quadrant':<18}{'conf':<8}flags")
    print(hdr)
    print("-" * 100)
    for r in result["rows"]:
        print(
            f"{r['canonical_feature'][:23]:<24}"
            f"{r['penetration']:>6.2f}{r['entry_inclusion']:>7.2f}"
            f"{(r['unlock_price_median'] if r['unlock_price_median'] is not None else 0):>8.1f}"
            f"{(r['gating_depth'] if r['gating_depth'] is not None else 0):>6.2f}"
            f"{(r['marginal_usd'] if r['marginal_usd'] is not None else 0):>8.1f}"
            f"  {r['value_score']:>5.2f}{r['commodity_score']:>6.2f}"
            f"  {r['quadrant'][:17]:<18}{r['confidence']:<8}{','.join(r['flags'])}"
        )
    print("-" * 100)
    json_path, csv_path = _write_outputs(result, Path(config.DB_PATH).expanduser().parent)
    print(f"저장: {json_path}\n      {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
