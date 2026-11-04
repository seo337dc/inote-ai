"""찾은 조각(과 앞뒤 이웃 조각)을 원문에서 다시 잘라 LLM에 줄 근거 텍스트를 만드는 순수 함수 — PLAN.md 6절 D 설계 4번.

DB에는 조각 본문이 없고 위치(start, end)와 해시만 있다. 그래서 BE에서 가져온 글을 같은 방식으로 텍스트로 바꾼 뒤(`html_to_text`) 그 위치를 잘라 쓴다.
**글이 그 사이에 바뀌었으면 위치가 어긋나므로**, 저장된 해시와 다시 계산한 해시를 비교해서 하나라도 다르면 None을 돌려준다(호출한 쪽이 앞부분 텍스트로 대신한다).
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Set, Tuple

from app.services.chunking import build_chunk_embedding_text
from app.services.embedding_store import ChunkInfo
from app.services.text_prep import content_hash

NEIGHBORS = 1  # 찾은 조각의 앞뒤로 함께 주는 조각 수
MAX_EXCERPT_CHARS = 3000  # 글 하나당 근거 텍스트 상한(프롬프트가 너무 길어지지 않게)


def _priority(matched: Sequence[int], available: Set[int], neighbors: int) -> List[int]:
    """찾은 조각을 먼저, 그다음 이웃 조각(가까운 것부터)을 순서대로 — 글자 수 상한을 넘으면 뒤쪽부터 버린다."""
    order: List[int] = []
    for m in matched:
        if m in available and m not in order:
            order.append(m)
    for distance in range(1, neighbors + 1):
        for m in matched:
            for k in (m - distance, m + distance):
                if k in available and k not in order:
                    order.append(k)
    return order


def _merge(ranges: List[Tuple[int, int]]) -> List[Tuple[int, int]]:
    """겹치거나 맞닿은(사이에 줄바꿈 한 글자뿐인) 구간을 합친다."""
    merged: List[Tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def build_excerpt(
    plain: str,
    title: str,
    layout: Sequence[ChunkInfo],
    matched: Sequence[int],
    neighbors: int = NEIGHBORS,
    max_chars: int = MAX_EXCERPT_CHARS,
) -> Optional[str]:
    """plain: 글을 html_to_text로 바꾼 텍스트, layout: 그 글의 저장된 조각들, matched: 질문과 가까운 조각 번호(가까운 순).
    사용할 조각이 없거나 원문이 저장 당시와 달라졌으면 None."""
    by_idx = {c.idx: c for c in layout}
    order = _priority(matched, set(by_idx), neighbors)
    if not order:
        return None

    chosen: List[ChunkInfo] = []
    for k in order:
        info = by_idx[k]
        if info.end > len(plain) or content_hash(build_chunk_embedding_text(title, plain[info.start : info.end])) != info.digest:
            return None  # 글이 바뀌었다(대기 중인 재임베딩이 아직 안 끝남) — 어긋난 위치로 엉뚱한 글자를 주지 않는다
        candidate = chosen + [info]
        size = sum(e - s for s, e in _merge([(c.start, c.end) for c in candidate]))
        if chosen and size > max_chars:
            continue  # 이 이웃 조각을 넣으면 상한을 넘는다 — 더 먼 조각이 들어갈 수도 있으니 다음으로
        chosen = candidate

    parts: List[str] = []
    previous_end: Optional[int] = None
    for start, end in _merge([(c.start, c.end) for c in chosen]):
        if previous_end is not None:
            parts.append("\n…\n")  # 떨어진 구간 사이는 생략 표시
        parts.append(plain[start:end])
        previous_end = end
    return "".join(parts)
