#!/bin/bash
# Set up image-watch: Discord, GitHub, and which AI writes the update summaries.
# Run it again any time to change a setting; press Enter to keep what's there.
set -e
cd "$(dirname "$0")"
mkdir -p secrets state && chmod 700 secrets
[ -f watch.json ] || { cp watch.json.example watch.json; echo "Made watch.json from the example: list the containers to watch there."; }

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
have() { [ -s "secrets/$1" ]; }

# ask_secret FILE PROMPT: hidden input saved to secrets/FILE; Enter keeps the old one.
ask_secret() {
  local hint=""; have "$1" && hint=" (Enter keeps the saved one)"
  read -rs -p "$2$hint: " v; echo
  if [ -n "$v" ]; then printf '%s\n' "$v" > "secrets/$1"; chmod 600 "secrets/$1"; fi
  unset v
  have "$1" || { echo "Nothing saved for $1."; return 1; }
}

# ask VAR PROMPT DEFAULT: plain input, default shown.
ask() { read -r -p "$2 [$3]: " v; printf -v "$1" '%s' "${v:-$3}"; unset v; }

command -v docker >/dev/null || { echo "Docker isn't installed."; exit 1; }
[ -f .env ] && . ./.env

bold "1. Discord"
echo "Server Settings > Integrations > Webhooks > New Webhook > pick a channel > Copy Webhook URL."
ask_secret discord-webhook "Discord webhook URL"

bold "2. GitHub (optional)"
echo "Without a token GitHub allows 60 requests an hour, enough for a few containers."
echo "For more: github.com/settings/personal-access-tokens/new, fine-grained,"
echo "Repository access: Public repositories, no permissions. Enter to skip."
ask_secret github-token "GitHub token" || true

bold "3. Who writes the summaries?"
cat <<'EOF'
  1) Cloudflare Workers AI   free daily allowance, needs a Cloudflare account
  2) OpenAI API              pay as you go (a summary costs well under a cent)
  3) Claude Code             your Claude Pro or Max subscription, no API billing
  4) Claude API              pay as you go (a summary costs about 2 cents)
  5) None                    just the list of commits
EOF
PREV_PROVIDER=${AI_PROVIDER:-} PREV_MODEL=${AI_MODEL:-}
# model_default DEFAULT: the saved model if this provider was already chosen, else DEFAULT.
model_default() { if [ "$AI_PROVIDER" = "$PREV_PROVIDER" ] && [ -n "$PREV_MODEL" ]; then echo "$PREV_MODEL"; else echo "$1"; fi; }
read -r -p "Choose 1-5 [${AI_PROVIDER:-1}]: " pick
case "${pick:-${AI_PROVIDER:-1}}" in
  1|cloudflare)
    AI_PROVIDER=cloudflare AI_TARGET=base
    bold "Cloudflare Workers AI"
    echo "a) dash.cloudflare.com > My Profile > API Tokens > Create Token."
    echo "b) Use the \"Workers AI\" template, Account Resources: your account, Create Token."
    echo "c) Your Account ID is on the account home page, right-hand column (or in the URL)."
    ask CF_ACCOUNT_ID "Account ID" "${CF_ACCOUNT_ID:-}"
    ask_secret ai-key "Workers AI token"
    ask AI_MODEL "Model" "$(model_default @cf/meta/llama-3.3-70b-instruct-fp8-fast)" ;;
  2|openai)
    AI_PROVIDER=openai AI_TARGET=base CF_ACCOUNT_ID=
    bold "OpenAI API"
    echo "a) platform.openai.com/api-keys > Create new secret key."
    echo "b) The API is billed separately from ChatGPT: add a few dollars under Billing."
    ask_secret ai-key "OpenAI API key"
    ask AI_MODEL "Model" "$(model_default gpt-5-mini)" ;;
  3|claude-code)
    AI_PROVIDER=claude-code AI_TARGET=claude-code CF_ACCOUNT_ID=
    bold "Claude Code (subscription)"
    echo "a) On any machine with Claude Code installed, run:  claude setup-token"
    echo "b) Sign in in the browser it opens, then copy the token it prints (sk-ant-oat...)."
    echo "   It lasts a year and uses your plan's usage, a little per summary."
    ask_secret ai-key "Claude Code token"
    ask AI_MODEL "Model (opus, sonnet or haiku)" "$(model_default opus)" ;;
  4|claude-api)
    AI_PROVIDER=claude-api AI_TARGET=base CF_ACCOUNT_ID=
    bold "Claude API"
    echo "a) platform.claude.com > API Keys > Create Key."
    echo "b) The API is billed separately from a Claude subscription: add credits under Billing."
    ask_secret ai-key "Claude API key"
    ask AI_MODEL "Model (claude-opus-5-5; claude-haiku-5-5 costs less)" "$(model_default claude-opus-5-5)" ;;
  5|none)
    AI_PROVIDER=none AI_TARGET=base AI_MODEL= CF_ACCOUNT_ID= ;;
  *) echo "Pick 1 to 5."; exit 1 ;;
esac

cat > .env <<EOF
AI_PROVIDER=$AI_PROVIDER
AI_TARGET=$AI_TARGET
AI_MODEL=$AI_MODEL
CF_ACCOUNT_ID=$CF_ACCOUNT_ID
EOF

bold "4. Build and test"
docker compose build -q image-watch
if docker compose run --rm -T image-watch python3 /app/watch.py --test; then
  docker compose up -d
  bold "Done. image-watch checks every hour; there's a test message in your Discord channel."
else
  bold "The test failed (see above). Fix the setting and run ./install.sh again."
  exit 1
fi
