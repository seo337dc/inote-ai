# -*- coding: utf-8 -*-
"""글쓰기 채팅이 '설명'과 '글 작성'을 도구 호출로 잘 구분하는지 확인한다 (Groq를 실제로 호출하는 수동 실행용).

    python scripts/eval_draft_intent.py [반복 횟수=2]

실제 서비스가 쓰는 프롬프트·도구(app/routers/chat.py)를 그대로 쓰므로, 프롬프트를 바꾼 뒤에 돌려서
판단이 나빠지지 않았는지 본다. 기대: 설명·애매한 요청은 도구 없이(no), 분명한 작성 요청은 도구 호출(write).
호출 한도(429)에 걸리면 잠시 쉬고 이어간다. 본문은 쓰지 않고 판단만 본다.
"""

import collections
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv  # noqa: E402

load_dotenv()
from groq import Groq  # noqa: E402

from app.routers.chat import DRAFT_RULES, DRAFT_TOOL, MODEL, SYSTEM_PROMPT  # noqa: E402

CASES = [
    # (발화, 기대, 그룹)
    ("도커 볼륨이 뭐야?", "no", "설명"), ("하네스 ai에 대해 설명해줘", "no", "설명"),
    ("RAG랑 파인튜닝 차이가 뭐야?", "no", "설명"), ("JWT는 어떻게 동작해?", "no", "설명"),
    ("useEffect는 언제 실행돼?", "no", "설명"), ("pgvector 설치 방법 알려줘", "no", "설명"),
    ("오늘 뭐부터 하면 좋을까?", "no", "설명"),
    ("글 잘 쓰는 방법이 뭐야?", "no", "함정"), ("블로그 제목 짓는 팁 알려줘", "no", "함정"),
    ("글감 추천해줘", "no", "함정"), ("이 주제로 글 쓸만할까?", "no", "함정"),
    ("초안 쓸 때 주의할 점 알려줘", "no", "함정"),
    ("글 써줘", "no", "애매"), ("블로그 글 하나 만들어줘", "no", "애매"),
    ("도커 볼륨 정리해줘", "no", "애매"), ("RAG에 대해 정리해줘", "no", "애매"),
    ("도커 볼륨에 대한 블로그 글 써줘", "write", "작성"), ("RAG 개념을 정리한 글 초안 작성해줘", "write", "작성"),
    ("JWT 동작 원리를 글로 써줘", "write", "작성"), ("KURE 임베딩 도입 후기를 블로그 글로 작성해줘", "write", "작성"),
    ("useEffect 정리 포스팅 초안 부탁해", "write", "작성"), ("pgvector 설치 방법을 글로 정리해서 써줘", "write", "작성"),
    ("도커 컴포즈 입문 튜토리얼 글 작성해줘", "write", "작성"), ("이번 주 회고 글 써줘. 이번 주에 임베딩 기능을 만들었어", "write", "작성"),
]


def decide(client, text):
    r = client.chat.completions.create(
        model=MODEL, max_tokens=800, tools=[DRAFT_TOOL], tool_choice="auto",
        messages=[{"role": "system", "content": SYSTEM_PROMPT + DRAFT_RULES}, {"role": "user", "content": text}],
    )
    return bool(r.choices[0].message.tool_calls)


def main():
    repeat = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    client = Groq(api_key=os.environ["GROQ_API_KEY"])
    score = collections.defaultdict(lambda: [0, 0])
    wrong = []
    for text, expect, group in CASES:
        for _ in range(repeat):
            for attempt in range(3):
                try:
                    called = decide(client, text)
                    break
                except Exception as e:  # 429 등: 잠시 쉬고 다시
                    time.sleep(8 * (attempt + 1))
                    called = None
            if called is None:
                continue
            ok = called == (expect == "write")
            score[group][0] += ok
            score[group][1] += 1
            if not ok:
                wrong.append((group, expect, text, called))
            time.sleep(2.5)
    for group, (a, b) in score.items():
        print(f"{group}: {a}/{b} = {a / b:.0%}")
    for w in wrong:
        print("오답:", w)
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
