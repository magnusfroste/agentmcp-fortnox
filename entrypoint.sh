#!/bin/sh
# Runs the service as the unprivileged user "fortnox" (uid 10001). Started as root -- the default,
# and what a volume made by an older image needs -- it first hands the data folder to that user,
# then drops root for good. Started as any other user (docker run --user, a platform that never
# gives root), it simply runs as that user and the data folder must already be writable.
set -e
umask 027  # new files and folders: nobody but the service (and its group) can read them
if [ "$(id -u)" = "0" ]; then
  for d in "${DATA_DIR:-/data}" ${STATE_DIR:+"$STATE_DIR"}; do
    mkdir -p "$d"
    # Deepest first: a folder handed over before its files could no longer be entered by a root
    # that keeps only CHOWN (no DAC_OVERRIDE), as under cap_drop: ALL.
    find "$d" -depth ! -user fortnox -exec chown -h fortnox:fortnox {} +
  done
  exec su-exec fortnox "$@"
fi
exec "$@"
