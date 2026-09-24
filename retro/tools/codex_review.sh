#!/usr/bin/env bash
# Ask Codex (already logged in on this machine) to review retro's template plan.
# Read-only: downloads the plan into a temp folder and prints Codex's review.
#   ssh SERVER 'curl -fsSL <raw url of this file> | bash -l -s'
set -e
BR="${RETRO_BRANCH:-claude/quirky-hamilton-8p2uv6}"
RAW="https://raw.githubusercontent.com/ffogtive/ai-market/$BR/retro"
D="$(mktemp -d)"
cd "$D"
for f in PLAN-template.md HANDOFF.md PRIVACY.md; do curl -fsSL "$RAW/$f" -o "$f"; done
command -v codex >/dev/null || { echo "이 기기에 codex CLI가 없습니다 (PATH: $PATH)"; exit 1; }

cat > prompt.txt <<'PROMPT'
아래 <PLAN>은 "retro"(AI로 일하는 사람의 하루 로그 → 일간·주간 회고 페이지) 템플릿 기획서다.
<HANDOFF>는 제품 현황, <PRIVACY>는 데이터 처리 방식이다. 세 문서는 전부 아래에 붙어 있으니
도구·명령·파일 읽기를 쓰지 말고 이 본문만 보고 기획서를 리뷰해라.
사용자는 비개발자 1인 창업자이고, 대상 사용자는 AI 도구(Claude Code, Codex, ChatGPT)로 일하는 직장인·대학생이다.

한국어로, 아래 형식만, 80줄 이내로 답해라. 파일을 수정하지 마라.
## 1. 약점·빠진 것 (구체적으로, 최대 7개)
## 2. 보완안 (1의 항목별로 어떻게 고칠지, 계산 가능한 정의나 템플릿 문구까지)
## 3. 새 아이디어 (지표·섹션·보기 5~8개. 각 1줄 설명 + "로그로 계산 가능/LLM 필요/새 수집 필요" 표시)
## 4. 오해·역효과 위험 (예: 지표가 사용자를 불안하게 하거나 잘못 판단하게 할 수 있는 곳)
## 5. 우선순위 Top 5 (가치 대비 구현 난이도 기준, 한 줄씩)
PROMPT
# the review needs no tools: Codex's sandbox (bwrap) cannot start on some servers, so the documents go in the prompt
for f in PLAN-template.md HANDOFF.md PRIVACY.md; do
  tag="${f%%[-.]*}"; tag="$(echo "$tag" | tr a-z A-Z)"
  printf '\n<%s>\n' "$tag" >> prompt.txt; cat "$f" >> prompt.txt; printf '\n</%s>\n' "$tag" >> prompt.txt
done

if codex exec --skip-git-repo-check -s read-only --output-last-message review.md "$(cat prompt.txt)" >codex.log 2>&1; then
  cat review.md
else
  echo "codex 실행 실패 — 마지막 로그:"; tail -15 codex.log
  exit 1
fi
