#!/bin/sh
set -eu

umask 0002

prepare_dir() {
    dir=$1
    mkdir -p "$dir"
    chown idp:idpwork "$dir"
    chmod g+rws "$dir"
}

prepare_dir /work/runs
prepare_dir /work/platform/docker/calculator/out

exec /usr/sbin/runuser -u idp -- "$@"
