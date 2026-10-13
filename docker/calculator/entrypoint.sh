#!/bin/sh
set -eu

umask 0002

shared_dir=/work/platform/docker/calculator/out
mkdir -p "$shared_dir"
chown idp:idpwork "$shared_dir"
chmod g+rws "$shared_dir"

exec /usr/sbin/runuser -u idp -- "$@"
