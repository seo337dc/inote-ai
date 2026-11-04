"""글 본문(순수 텍스트)을 검색 단위 조각(청크)으로 나누는 순수 함수 — inote 레포 docs/kure/PLAN.md 6절 D 설계 1번.

- 입력은 `text_prep.html_to_text`의 결과다(문단은 줄바꿈). 청크는 **그 텍스트의 연속된 조각**이라 `text[start:end]`가 청크 그대로다.
  그래서 DB에는 본문 대신 (start, end, 해시)만 저장하고, 보여 줄 때 원문에서 다시 잘라 낼 수 있다.
- 크기는 글자 수로 정한다. 실제 글(운영 공개 글 57편)을 KURE 토크나이저로 재 보니 한국어는 약 2.05자 = 1토큰(글별 1.76~2.43)이라
  목표 800자 ≈ 390토큰, 최대 1,000자 ≈ 490토큰(최악 1.76자/토큰이어도 570토큰)이다. 모델 한도(8,192토큰)보다 한참 작다.
- 나누는 순서: 문단 → (너무 길면) 문장 → (그래도 길면) 공백 → 글자 수. 문단·문장을 가능한 한 쪼개지 않는다.
- 겹침: 다음 청크는 앞 청크의 끝 문단(문장)들을 `OVERLAP_CHARS` 안에서 다시 포함해 시작한다. 경계에서 문맥이 끊기는 것을 줄인다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

CHUNKER_VERSION = 1  # 나누는 방식을 바꾸면 올린다 — 저장된 조각의 source_hash가 달라져 모든 글이 다시 나뉜다
TARGET_CHARS = 800
MAX_CHARS = 1000
OVERLAP_CHARS = 100
MIN_TAIL_CHARS = 100  # 마지막에 이보다 짧은 조각이 남으면 앞 청크에 붙인다(최대 MAX_CHARS 안에서)

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


@dataclass(frozen=True)
class Chunk:
    index: int
    start: int  # text 안의 시작 위치(포함)
    end: int  # text 안의 끝 위치(제외)
    text: str  # text[start:end]


def _trim(text: str, start: int, end: int) -> Tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _hard_split(text: str, start: int, end: int, max_chars: int) -> List[Tuple[int, int]]:
    """문장 하나가 max_chars보다 길 때: 가능하면 공백에서, 없으면 글자 수로 자른다."""
    pieces: List[Tuple[int, int]] = []
    while end - start > max_chars:
        cut = text.rfind(" ", start + max_chars // 2, start + max_chars)
        if cut == -1:
            cut = start + max_chars
        s, e = _trim(text, start, cut)
        if s < e:
            pieces.append((s, e))
        start = cut
    s, e = _trim(text, start, end)
    if s < e:
        pieces.append((s, e))
    return pieces


def _units(text: str, max_chars: int) -> List[Tuple[int, int]]:
    """더 쪼개지 않을 최소 단위 목록 [(start, end)] — 각 단위는 max_chars 이하다."""
    units: List[Tuple[int, int]] = []
    for line in re.finditer(r"[^\n]+", text):
        s, e = _trim(text, line.start(), line.end())
        if s >= e:
            continue
        if e - s <= max_chars:
            units.append((s, e))
            continue
        cursor = s
        for m in _SENTENCE_END.finditer(text, s, e):
            units.extend(_hard_split(text, cursor, m.start(), max_chars))
            cursor = m.end()
        units.extend(_hard_split(text, cursor, e, max_chars))
    return units


def chunk_text(
    text: str,
    target_chars: int = TARGET_CHARS,
    max_chars: int = MAX_CHARS,
    overlap_chars: int = OVERLAP_CHARS,
) -> List[Chunk]:
    """텍스트를 청크 목록으로 나눈다. 빈 텍스트는 빈 목록. 같은 입력은 항상 같은 결과다."""
    units = _units(text or "", max_chars)
    n = len(units)
    chunks: List[Chunk] = []
    i = 0
    while i < n:
        start = units[i][0]
        j = i
        while j + 1 < n and units[j + 1][1] - start <= target_chars:
            j += 1
        # 맨 끝에 너무 짧은 단위 하나만 남으면 이 청크에 붙인다(너무 짧은 청크를 만들지 않는다)
        if j + 2 == n and units[n - 1][1] - units[n - 1][0] < MIN_TAIL_CHARS and units[n - 1][1] - start <= max_chars:
            j = n - 1
        end = units[j][1]
        chunks.append(Chunk(index=len(chunks), start=start, end=end, text=text[start:end]))
        if j == n - 1:
            break
        # 다음 청크는 앞 청크의 끝 단위들을 overlap_chars 안에서 다시 포함한다. 단, 반드시 앞으로 나아간다(k > i).
        k = j + 1
        while k - 1 > i and end - units[k - 1][0] <= overlap_chars:
            k -= 1
        i = k
    return chunks


def build_chunk_embedding_text(title: str, chunk: str) -> str:
    """청크를 임베딩할 텍스트: 제목 + 줄바꿈 + 청크. 제목을 붙여 두면 조각만으로는 알 수 없는 글의 주제가 벡터에 반영된다."""
    return f"{title.strip()}\n{chunk.strip()}".strip()
