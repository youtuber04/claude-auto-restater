# claude auto restater (구 otsukare)

Claude Code 터미널 세션용 **사용량 한도 가드레일**입니다. 5시간 창과 주간(7일) 창을 함께 보다가, **둘 중 먼저 99%에 닿는 창에서 안전한 지점에 멈추고**, 그 창이 초기화된 뒤(+10분) 예약으로 깨어나 체크포인트부터 이어서 합니다.

원본 otsukare 스킬을 바탕으로 2026-11-18에 아래를 고친 판입니다.

## 바뀐 점 (2026-11-18)
- 기준: 두 창 모두 97%에서 경고(soft), **99%에서 멈춤(hard)**. 원본은 5h 90/97, 7일 95/98.
- 깨어나는 시각: 먼저 찬 창의 초기화 시각 + 10분(5h 창이면 몇 시간 뒤, 주간 창이면 며칠 뒤).
- **주간 창만 보이는 요금제도 적용**: 원본은 5시간 창이 없으면 "해당 없음"으로 끝났지만, 이제 5h 창이 없으면 주간 창을 기준으로 사전 점검·신선도·안전망 예약을 합니다(`_primary_reset`).
- 단위 테스트 93개 통과(`python -X utf8 -m unittest test_otsukare_usage`, scripts 폴더에서).

## 설치
1. 이 폴더를 `~/.claude/skills/claude auto restater/` 에 둡니다(폴더 이름에 띄어쓰기 있음).
2. `~/.claude/settings.json` 의 상태표시줄을 이 스킬의 statusline.py 로 지정합니다(사용량을 `~/.claude/last-statusline-input.json` 에 비춰 줍니다). Windows 예:
   ```json
   "statusLine": { "type": "command", "command": "\"C:\\Users\\<사용자>\\AppData\\Local\\Programs\\Python\\Python314\\python.exe\" -X utf8 \"C:\\Users\\<사용자>\\.claude\\skills\\claude auto restater\\scripts\\statusline.py\"" }
   ```
3. **터미널 `claude` 세션**에서만 동작합니다(데스크톱 앱 Code 탭은 상태표시줄을 그리지 않아 미러 파일이 안 생김). 터미널에서 `claude` 를 띄우고 맨 아래에 `모델 · 5h N%` 줄이 보이면 준비된 것입니다.
4. 확인: `python -X utf8 "~/.claude/skills/claude auto restater/scripts/otsukare_usage.py" --preflight` → `"applicable": true`.

## 사용
터미널 세션에 `claude auto restater 켜고 <작업>` 또는 `otsukare 켜고 <작업>` 이라고 지시합니다. 긴 무인 작업 시작 전에 켜야 하며, 작업 중에는 터미널 창을 닫지 말고 컴퓨터가 깨어 있어야 합니다(재개 예약은 세션 안에서만 살아 있음).

## 파일
- `SKILL.md` — 에이전트가 따르는 절차(사전 점검 → 안전망 예약 → 이음새마다 사용량 확인 → 멈춤/재개).
- `scripts/otsukare_usage.py` — 판정 helper(`--preflight`, `--wait-fresh`, `--arm`, 기본 실행은 decision JSON).
- `scripts/statusline.py` — 상태표시줄 미러.
- `scripts/session_hook.py` — 세션 시작 훅(재개용).
- `scripts/test_*.py` — 단위 테스트.
