"""변동 감지 (7장).

직전 스냅샷의 payload 와 이번 회차를 비교하여 changes 레코드 리스트를 만든다.
순수 함수 — DB 나 프레임워크에 의존하지 않는다.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import PricingSnapshot, Tier


@dataclass
class Change:
    change_type: str  # price_changed | tier_added | tier_removed | feature_changed
    tier_name: Optional[str]
    field: Optional[str]
    old_value: Optional[str]
    new_value: Optional[str]
    summary: str


def _fmt_price(v: Optional[float]) -> str:
    if v is None:
        return "비공개"
    if v == int(v):
        return f"${int(v)}"
    return f"${v}"


def _tier_map(snap: PricingSnapshot) -> dict[str, Tier]:
    return {t.name: t for t in snap.tiers}


def diff_snapshots(
    company: str, old: Optional[PricingSnapshot], new: PricingSnapshot
) -> list[Change]:
    """old(직전) 와 new(이번) 를 비교. old 가 없으면(최초 수집) 빈 리스트."""
    if old is None:
        return []

    changes: list[Change] = []
    old_tiers = _tier_map(old)
    new_tiers = _tier_map(new)

    old_names = set(old_tiers)
    new_names = set(new_tiers)

    # ── 티어 추가/삭제 ──
    for name in sorted(new_names - old_names):
        changes.append(
            Change(
                change_type="tier_added",
                tier_name=name,
                field=None,
                old_value=None,
                new_value=name,
                summary=f"{company} {name} 티어 신설",
            )
        )
    for name in sorted(old_names - new_names):
        changes.append(
            Change(
                change_type="tier_removed",
                tier_name=name,
                field=None,
                old_value=name,
                new_value=None,
                summary=f"{company} {name} 티어 삭제",
            )
        )

    # ── 공통 티어: 가격 / 기능 비교 ──
    for name in sorted(old_names & new_names):
        ot, nt = old_tiers[name], new_tiers[name]

        for field, label in (
            ("monthly_price", "월"),
            ("annual_price_per_month", "연(월환산)"),
        ):
            ov = getattr(ot, field)
            nv = getattr(nt, field)
            if ov != nv:
                direction = ""
                if isinstance(ov, (int, float)) and isinstance(nv, (int, float)):
                    direction = " 인상" if nv > ov else " 인하"
                changes.append(
                    Change(
                        change_type="price_changed",
                        tier_name=name,
                        field=field,
                        old_value=_fmt_price(ov),
                        new_value=_fmt_price(nv),
                        summary=(
                            f"{company} {name}: {_fmt_price(ov)} → {_fmt_price(nv)} "
                            f"({label}){direction}"
                        ),
                    )
                )

        # ── 기능 차집합 ──
        old_feats = set(ot.features)
        new_feats = set(nt.features)
        added = sorted(new_feats - old_feats)
        removed = sorted(old_feats - new_feats)
        if added or removed:
            parts = []
            if added:
                parts.append("추가: " + ", ".join(added))
            if removed:
                parts.append("제거: " + ", ".join(removed))
            changes.append(
                Change(
                    change_type="feature_changed",
                    tier_name=name,
                    field="features",
                    old_value="; ".join(sorted(old_feats)) or None,
                    new_value="; ".join(sorted(new_feats)) or None,
                    summary=f"{company} {name} 기능 변경 — " + " / ".join(parts),
                )
            )

    return changes
