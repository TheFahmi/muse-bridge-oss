#!/bin/bash
# Build the admin dashboard (Next.js static export) and upload it to the server.
#
#   ./deploy.sh                        # build only -> out/
#   BRIDGE_HOST=ubuntu@1.2.3.4 ./deploy.sh   # build + upload via ssh
#
set -e
cd "$(dirname "$0")"

echo "== build =="
npm run build

if [ -n "$BRIDGE_HOST" ]; then
  echo "== upload to $BRIDGE_HOST =="
  tar czf - -C out . | ssh "$BRIDGE_HOST" 'mkdir -p /home/ubuntu/muse-bridge/admin-next/out && tar xzf - -C /home/ubuntu/muse-bridge/admin-next/out'
  echo "deployed."
else
  echo "build done -> out/"
  echo "Copy the contents of out/ to /home/ubuntu/muse-bridge/admin-next/out on your server,"
  echo "or set BRIDGE_HOST='user@host' and re-run to upload via ssh."
fi
