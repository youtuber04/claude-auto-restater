# claude auto restater (구 otsukare)

Claude Code **터미널 세션**용 사용량 한도 가드레일입니다. 5시간 창과 주간(7일) 창을 함께 보다가 **둘 중 먼저 99%에 닿는 창에서 안전한 지점에 멈추고**, 그 창이 초기화된 뒤(+10분) 예약으로 깨어나 체크포인트부터 이어서 합니다. 원본 otsukare 스킬을 바탕으로 2026-11-18에 고친 판입니다.

## 빠른 설치 (Windows, 1분)

PowerShell 을 열고 아래 한 줄을 실행합니다(스킬 폴더 받기 + 설정 등록까지 자동).

```powershell
irm https://raw.githubusercontent.com/youtuber04/claude-auto-restater/main/install.ps1 | iex
```

git 이 있으면 이렇게 해도 됩니다.

```powershell
git clone https://github.com/youtuber04/claude-auto-restater "$HOME\.claude\skills\claude auto restater"
python -X utf8 "$HOME\.claude\skills\claude auto restater\scripts\setup.py"
```

그다음 **터미널에서 `claude` 를 새로 켜고**(이미 켜져 있던 창은 `/exit` 후 다시) 맨 아래에 `모델 · 5h N%` 줄이 보이면 준비 끝입니다.

> 스킬을 그냥 실행해도 됩니다. 스킬이 시작할 때 설정을 스스로 점검하고 빠져 있으면 `setup.py` 를 알아서 돌린 뒤, 터미널을 다시 켜라고 안내합니다.

## 사용

터미널 `claude` 세션에 이렇게 지시합니다.

```
claude auto restater 켜고 <시킬 작업>
```

예: `claude auto restater 켜고 쇼츠 20개 만들어`. "otsukare 켜고 …"도 같은 스킬입니다. 작업 중에는 터미널 창을 닫지 말고 컴퓨터가 깨어 있어야 합니다(재개 예약은 세션 안에서만 살아 있음).

## 꼭 알아 둘 것

- **데스크톱 앱 Code 탭에서는 동작하지 않습니다.** 앱은 상태표시줄을 그리지 않아 사용량 파일이 생기지 않습니다. 터미널 `claude` 에서 쓰세요.
- 상태표시줄 설정은 세션을 **시작할 때만** 읽힙니다. 설정을 넣은 뒤에는 터미널 claude 를 껐다 켜야 합니다.
- 주간 창만 보이는 요금제(5시간 창 없음)도 됩니다. 그때는 주간 초기화 시각을 기준으로 깨어납니다(며칠 뒤).
- 필요한 것: 파이썬 3, Claude Code 터미널 로그인(구독 계정).

## 점검

```powershell
python -X utf8 "$HOME\.claude\skills\claude auto restater\scripts\setup.py" --check
python -X utf8 "$HOME\.claude\skills\claude auto restater\scripts\otsukare_usage.py" --preflight
```

`--check` 는 설정·미러 파일 상태를 JSON 과 한국어로 보여 주고, `--preflight` 가 `"applicable": true` 면 정상입니다.

## 바뀐 점 (2026-11-18)

- 기준: 두 창 모두 97%에서 경고(soft), **99%에서 멈춤(hard)**. 원본은 5h 90/97, 7일 95/98.
- 깨어나는 시각: 먼저 찬 창의 초기화 시각 + 10분.
- 주간 창만 있는 요금제도 적용(원본은 5h 창이 없으면 "해당 없음"으로 끝났음).
- `scripts/setup.py` 추가: settings.json 에 상태표시줄 미러·SessionStart 훅을 자동 등록(백업 남김), 시험 실행 후 가짜 데이터는 지움, `--check` 로 점검.
- `install.ps1` 추가: git 없어도 ZIP 으로 받아 설치.
- 사전 점검 메시지를 한국어 + 다음 할 일 안내로 바꿈.
- 단위 테스트: `scripts` 폴더에서 `python -X utf8 -m unittest` (otsukare_usage 93개 + setup 6개).

## 파일

- `SKILL.md` — 에이전트가 따르는 절차(자동 설치 점검 → 사전 점검 → 안전망 예약 → 이음새마다 사용량 확인 → 멈춤/재개).
- `scripts/setup.py` — 설치·점검.
- `scripts/otsukare_usage.py` — 판정 helper(`--preflight`, `--wait-fresh`, `--arm`, 기본 실행은 decision JSON).
- `scripts/statusline.py` — 상태표시줄 미러.
- `scripts/session_hook.py` — 세션 시작 훅(끊긴 작업 재개 제안).
- `install.ps1` — Windows 설치 스크립트.
