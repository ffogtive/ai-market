#!/bin/sh
# retro installer — one line, no Python setup needed:
#   curl -fsSL https://raw.githubusercontent.com/ffogtive/ai-market/claude/eloquent-brahmagupta-2mkg25/retro/install.sh | sh
set -e
REPO="https://github.com/ffogtive/ai-market"
BRANCH="${RETRO_BRANCH:-claude/eloquent-brahmagupta-2mkg25}"
DIR="$HOME/.local/share/retro"
BIN="$HOME/.local/bin"

# uv supplies its own Python, so the system python3 version never matters
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$BIN/uv" ]; then
  echo "· uv 설치 중 (Python 관리 도구)"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
fi
UV="$(command -v uv || echo "$BIN/uv")"

if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull -q --ff-only
else
  git clone -q --depth 1 -b "$BRANCH" "$REPO" "$DIR"
fi

mkdir -p "$BIN"
cat > "$BIN/retro" <<SH
#!/bin/sh
[ "\$1" = "update" ] && exec git -C "$DIR" pull --ff-only
exec "$UV" run --quiet "$DIR/retro/retro.py" "\$@"
SH
chmod +x "$BIN/retro"
"$UV" run --quiet "$DIR/retro/retro.py" --help >/dev/null  # pre-fetch Python + deps now, not on first use

# put ~/.local/bin on PATH for future shells (zsh is the macOS default)
case "${SHELL##*/}" in
  zsh) RC="$HOME/.zshrc" ;;
  bash) RC="$HOME/.bashrc"; [ "$(uname)" = Darwin ] && RC="$HOME/.bash_profile" ;;
  *) RC="$HOME/.profile" ;;
esac
if ! grep -qs '.local/bin' "$RC"; then
  printf '\n# added by retro installer\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$RC"
fi

echo "✓ 설치 완료: retro"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "  이 터미널에서 바로 쓰려면: source $RC  (새 터미널은 자동)";; esac
echo "  retro                  오늘의 회고 페이지"
echo "  retro add-host \"ssh -p 포트 user@서버\"   서버 기록도 함께 수집"
