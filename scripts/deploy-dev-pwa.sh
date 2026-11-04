#!/usr/bin/env bash
# Deploy the working-tree frontend to the dev PWA (https://rain-on-trump-dev.pages.dev).
#
# Usage:
#   scripts/deploy-dev-pwa.sh            # build the staged copy and upload it
#   scripts/deploy-dev-pwa.sh --dry-run  # build the staged copy and list it, no upload
#
# The dev PWA lives in its own Cloudflare Pages project (direct upload, not
# Git-connected), so it can never touch production (rain-on-trump.pages.dev,
# which deploys from the public repo's main). Uncommitted frontend edits are
# included on purpose: this is for testing UI changes on a device before release.
# It talks to the production API (rain-api.fly.dev).
#
# First-time setup (once):
#   npx wrangler pages project create rain-on-trump-dev --production-branch main --force
#   (--force: wrangler 4.15x otherwise delegates new projects to Workers and fails)
set -euo pipefail

PROJECT=rain-on-trump-dev
DEV_NAME="Is It Raining on Trump? (DEV)"
DEV_SHORT_NAME="RoT DEV"

die() { echo "deploy-dev-pwa: $*" >&2; exit 1; }

cd "$(git rev-parse --show-toplevel)"

dry_run=false
case "${1:-}" in
  --dry-run) dry_run=true ;;
  "") ;;
  *) die "usage: scripts/deploy-dev-pwa.sh [--dry-run]" ;;
esac

branch=$(git branch --show-current)
[[ $branch == dev ]] || echo "deploy-dev-pwa: warning: on branch '$branch', not dev" >&2

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT

# Tracked + untracked-but-not-ignored frontend files, as they are on disk.
git ls-files -z --cached --others --exclude-standard -- frontend \
  | while IFS= read -r -d '' f; do
      [[ -f $f ]] || continue # deleted but still tracked
      mkdir -p "$stage/$(dirname "${f#frontend/}")"
      cp "$f" "$stage/${f#frontend/}"
    done
[[ -f $stage/index.html ]] || die "staging failed: index.html missing"

# Mark the dev build so its home-screen icon and tabs are distinguishable.
python3 - "$stage" "$DEV_NAME" "$DEV_SHORT_NAME" <<'EOF'
import json, re, sys
from pathlib import Path

stage, name, short = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
manifest = stage / "manifest.webmanifest"
data = json.loads(manifest.read_text())
data["name"], data["short_name"] = name, short
manifest.write_text(json.dumps(data, indent=2) + "\n")

for page in stage.glob("*.html"):
    html = page.read_text()
    new, n = re.subn(r"<title>", "<title>[DEV] ", html, count=1)
    if n:
        page.write_text(new)
EOF

version=$(grep -oE "const VERSION *= *'[^']+'" "$stage/sw.js" | grep -oE "'[^']+'" | tr -d "'")
echo "Staged frontend v${version} for $PROJECT"
echo "  app name: $(python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print(m["name"], "/", m["short_name"])' "$stage/manifest.webmanifest")"
echo "  title:    $(grep -o '<title>[^<]*' "$stage/index.html" | sed 's/<title>//')"
echo "Files:"
(cd "$stage" && find . -type f | sort | sed 's|^\./|  |')

if $dry_run; then
  echo "(dry run: nothing uploaded)"
  exit 0
fi

dirty=""
git diff --quiet -- frontend || dirty=" (+ uncommitted frontend changes)"
npx wrangler pages deploy "$stage" \
  --project-name "$PROJECT" \
  --branch main \
  --commit-hash "$(git rev-parse HEAD)" \
  --commit-message "dev PWA v${version} from $(git rev-parse --short HEAD)${dirty}" \
  --commit-dirty=true

echo
echo "Deployed. Verify: curl -s https://$PROJECT.pages.dev/sw.js | grep VERSION"
