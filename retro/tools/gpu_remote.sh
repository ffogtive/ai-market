#!/usr/bin/env bash
# Keep a Claude Code Remote Control session running on this server for the retro repo,
# so sessions started from the Claude Code app work here directly.
#   ssh -t SERVER 'curl -fsSL <raw url of this file> -o /tmp/retro-rc.sh && bash -l /tmp/retro-rc.sh'
# Runs inside tmux (session "claude-rc") so it survives closing the terminal. git 1.8 compatible.
set -e
DIR="$HOME/retro"
URL="https://github.com/ffogtive/retro.git"

if [ -d "$DIR/.git" ]; then
  cd "$DIR"
  case "$(git config --get remote.origin.url)" in
    *ffogtive/retro*|*ffogtive/ai-market*) git pull -q --ff-only || echo "· git pull 실패 — 기존 내용으로 계속합니다" ;;
    *) echo "$DIR 는 다른 저장소입니다. 건드리지 않고 멈춥니다."; exit 1 ;;
  esac
elif [ -e "$DIR" ]; then
  echo "$DIR 가 이미 있습니다(retro 저장소 아님). 건드리지 않고 멈춥니다."; exit 1
else
  git clone -q "$URL" "$DIR"
  cd "$DIR"
fi

command -v claude >/dev/null || { echo "이 서버에 claude CLI가 없습니다 (PATH: $PATH)"; exit 1; }
CLAUDE_BIN="$(command -v claude)"
echo "· claude $("$CLAUDE_BIN" --version 2>&1 | head -1) ($CLAUDE_BIN), node $(node --version 2>&1)"
# tmux starts a non-login shell with another PATH, which found an older claude on CentOS 7's old Node
# ("ReadableStream is not defined"): hand it this login shell's PATH and the exact claude found here.
# If it stops, keep its message on screen instead of the window vanishing with "[exited]".
# --spawn is newer than some installs (2.1.272 on the server rejects it): pass it only when supported
SPAWN=""
"$CLAUDE_BIN" remote-control --help 2>&1 | grep -q -- "--spawn" && SPAWN="--spawn=same-dir"
RUN="export PATH='$PATH'; cd '$DIR' && '$CLAUDE_BIN' remote-control $SPAWN; code=\$?; echo; echo \"[retro] claude remote-control 종료 (코드 \$code). 위 메시지를 확인하세요. Enter를 누르면 닫힙니다.\"; read _"
if command -v tmux >/dev/null; then
  if tmux has-session -t claude-rc 2>/dev/null; then
    case "$(tmux list-panes -t claude-rc -F '#{pane_current_command}' 2>/dev/null | head -1)" in
      bash|sh|zsh|"")  # remote-control already ended; only the "press Enter" prompt is left
        echo "· 지난번에 멈춘 세션을 정리하고 새로 띄웁니다"
        tmux kill-session -t claude-rc ;;
      *)
        echo "· 이미 실행 중입니다 — 화면에 붙습니다 (창을 닫아도 계속 실행)"
        exec tmux attach -t claude-rc ;;
    esac
  fi
  exec tmux new -s claude-rc "$RUN"
fi
echo "· tmux가 없어 이 창에서 실행합니다 — 창을 닫으면 꺼집니다"
exec bash -c "$RUN"
