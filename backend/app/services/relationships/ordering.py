from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.services.relationships.overlap import OverlapScore


@dataclass(frozen=True, slots=True)
class OrderingIssueData:
    code: str
    message: str
    image_ids: list[str]


@dataclass(frozen=True, slots=True)
class OrderingResolution:
    ordered_ids: list[str]
    auto_applied: bool
    issues: list[OrderingIssueData]


def _issue(code: str, message: str, ids: list[str]) -> OrderingIssueData:
    return OrderingIssueData(code=code, message=message, image_ids=ids)


def resolve_order(
    image_ids: list[str], scores: list[OverlapScore], settings: Settings
) -> OrderingResolution:
    candidates = [
        item
        for item in scores
        if item.source_id != item.target_id
        and item.combined_score >= settings.overlap_candidate_threshold
    ]
    outgoing: dict[str, list[OverlapScore]] = {image_id: [] for image_id in image_ids}
    incoming: dict[str, list[OverlapScore]] = {image_id: [] for image_id in image_ids}
    for item in candidates:
        if item.source_id in outgoing and item.target_id in incoming:
            outgoing[item.source_id].append(item)
            incoming[item.target_id].append(item)
    for values in [*outgoing.values(), *incoming.values()]:
        values.sort(key=lambda item: item.combined_score, reverse=True)

    issues: list[OrderingIssueData] = []
    for source_id, values in outgoing.items():
        if len(values) > 1:
            issues.append(
                _issue(
                    "BRANCH",
                    "一张截图存在多个可能的下一张截图",
                    [source_id, *[item.target_id for item in values]],
                )
            )
            if values[0].combined_score - values[1].combined_score <= settings.overlap_ambiguity_margin:
                issues.append(
                    _issue(
                        "NEAR_TIE",
                        "多个候选顺序的重叠分数过于接近",
                        [source_id, values[0].target_id, values[1].target_id],
                    )
                )
    for target_id, values in incoming.items():
        if len(values) > 1:
            issues.append(
                _issue(
                    "MERGE",
                    "多张截图可能指向同一张下一截图",
                    [*[item.source_id for item in values], target_id],
                )
            )

    best_out = {key: values[0] for key, values in outgoing.items() if values}
    best_in = {key: values[0] for key, values in incoming.items() if values}
    selected = {
        source: edge.target_id
        for source, edge in best_out.items()
        if edge.combined_score >= settings.overlap_auto_threshold
        and best_in.get(edge.target_id) == edge
    }

    for start in image_ids:
        seen: set[str] = set()
        current = start
        while current in selected:
            if current in seen:
                issues = [_issue("CYCLE", "候选顺序形成环路", sorted(seen))]
                return OrderingResolution(image_ids, False, issues)
            seen.add(current)
            current = selected[current]

    if issues:
        return OrderingResolution(image_ids, False, issues)
    targets = set(selected.values())
    starts = [image_id for image_id in image_ids if image_id not in targets]
    if len(image_ids) == 1:
        return OrderingResolution(image_ids, True, [])
    if len(selected) != len(image_ids) - 1 or len(starts) != 1:
        return OrderingResolution(
            image_ids,
            False,
            [_issue("DISCONNECTED", "无法得到覆盖全部截图的唯一顺序链", image_ids)],
        )
    ordered = [starts[0]]
    while ordered[-1] in selected:
        ordered.append(selected[ordered[-1]])
    if len(ordered) != len(image_ids):
        return OrderingResolution(
            image_ids,
            False,
            [_issue("DISCONNECTED", "候选顺序链未覆盖全部截图", image_ids)],
        )
    return OrderingResolution(ordered, True, [])
