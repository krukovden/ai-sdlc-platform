#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
image_name=idp-calculator-e2e
container_name=idp-ado-test
default_token_file=$HOME/Documents/ORCA/Halliburton/copilot-docker.token
host_runs_dir=$HOME/orca/docker/idp-ado-test
ado_run_name=ado-robotics
ado_run_root=/work/runs/$ado_run_name
ado_home=$ado_run_root/home/idp
ado_out=$ado_run_root/out
mode=fake
env_file=

if [ "${1:-}" = "ado" ]; then
    mode=ado
    shift
elif [ "${1:-}" = "fake" ]; then
    shift
fi

token_file=${1:-$default_token_file}

if [ "$mode" = "fake" ] && [ $# -gt 0 ]; then
    shift
fi

if [ ! -f "$token_file" ]; then
    printf 'token file not found: %s\n' "$token_file" >&2
    exit 1
fi

out_dir=$repo_root/docker/calculator/out
mkdir -p "$out_dir"
mkdir -p "$host_runs_dir"

stop_container() {
    if ! docker ps -a --format '{{.Names}}' | grep -qx "$container_name"; then
        return 0
    fi
    docker stop "$container_name" >/dev/null
    running=$(docker inspect -f '{{.State.Running}}' "$container_name")
    if [ "$running" != "false" ]; then
        printf 'container %s is still running after docker stop\n' "$container_name" >&2
        return 1
    fi
}

cleanup() {
    status=$?
    if [ -n "$env_file" ]; then
        rm -f "$env_file"
    fi
    if [ "$mode" = "ado" ]; then
        if ! stop_container; then
            status=1
        fi
    fi
    exit "$status"
}
trap cleanup EXIT INT TERM HUP

token=$(tr -d '\r\n' < "$token_file")
if [ -z "$token" ]; then
    printf 'token file is empty: %s\n' "$token_file" >&2
    exit 1
fi

docker build --file "$repo_root/docker/calculator/Dockerfile" --tag "$image_name" "$repo_root"

ensure_container() {
    if docker ps -a --format '{{.Names}}' | grep -qx "$container_name"; then
        running=$(docker inspect -f '{{.State.Running}}' "$container_name")
        if [ "$running" != "true" ]; then
            docker start "$container_name" >/dev/null
        fi
        return 0
    fi
    docker run -d --init \
        --name "$container_name" \
        --volume "$host_runs_dir:/work/runs" \
        "$image_name" >/dev/null
}

prepare_run_root() {
    docker exec -u root "$container_name" /bin/sh -lc "
        mkdir -p '$ado_run_root/home/idp' '$ado_run_root/home/agent' '$ado_out' &&
        chown -R idp:idpwork '$ado_run_root' &&
        chmod -R g+rwX '$ado_run_root'
    "
}

if [ "$mode" = "ado" ]; then
    if [ -z "${AZURE_DEVOPS_EXT_PAT:-}" ]; then
        printf 'AZURE_DEVOPS_EXT_PAT is not set. Source ~/.halliburton/ado-env.sh first.\n' >&2
        exit 1
    fi
    ensure_container
    prepare_run_root
    HOME="$ado_home" COPILOT_GITHUB_TOKEN="$token" docker exec \
        -u idp \
        -e HOME \
        -e AZURE_DEVOPS_EXT_PAT \
        -e COPILOT_GITHUB_TOKEN \
        -w "$ado_run_root" \
        "$container_name" \
        python3 /work/platform/docker/calculator/scenario.py \
        --board ado \
        --org "https://dev.azure.com/HAL-TSS" \
        --project "TSS-Orion" \
        --out "$ado_out" \
        --i-understand-this-writes
    printf 'container left stopped for reuse: docker start %s\n' "$container_name"
else
    env_file=$(mktemp "${TMPDIR:-/tmp}/copilot-docker.XXXXXX")
    printf 'COPILOT_GITHUB_TOKEN=%s\n' "$token" > "$env_file"
    docker run --rm \
        --env-file "$env_file" \
        --volume "$out_dir:/work/platform/docker/calculator/out" \
        "$image_name" \
        python3 docker/calculator/scenario.py "$@"
fi
