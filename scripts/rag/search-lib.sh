# shellcheck shell=sh
# Shared helpers for scripts/rag/search-*.sh (sourced, POSIX sh).
#
# Executables are separate variables, never a command string, so they are
# called as "$DOCKER_BIN" compose ... without eval or word splitting:
#   DOCKER_BIN (docker)  CURL_BIN (curl)  UV_BIN (uv)
# Compose always runs with an explicit project name and file, and refuses the
# shared "eventlens" project: cleanup can only reach this run's resources.

DOCKER_BIN="${DOCKER_BIN:-docker}"
CURL_BIN="${CURL_BIN:-curl}"
UV_BIN="${UV_BIN:-uv}"
SEARCH_COMPOSE_FILE="${SEARCH_COMPOSE_FILE:-docker/search-eval.compose.yaml}"
SEARCH_ES_WAIT_SECONDS="${SEARCH_ES_WAIT_SECONDS:-180}"

# require_owned_project NAME: only eventlens-search-* projects are managed.
require_owned_project() {
    case "$1" in
        eventlens-search-dev | eventlens-search-eval-*) return 0 ;;
        *)
            printf 'refusing to manage compose project %s\n' "$1" >&2
            return 2
            ;;
    esac
}

# compose ARGS...: docker compose bound to $SEARCH_PROJECT and the search file.
compose() {
    require_owned_project "$SEARCH_PROJECT" || return 2
    "$DOCKER_BIN" compose --project-name "$SEARCH_PROJECT" \
        --file "$SEARCH_COMPOSE_FILE" "$@"
}

# run_id: lowercase, compose-safe unique id for one run.
new_run_id() {
    printf '%s-%s' "$(date -u +%Y%m%dt%H%M%S)" "$$"
}

# step NAME CMD...: run CMD, log to $ART/NAME.log, append "NAME<TAB>rc" to
# $ART/steps.tsv (only steps that actually ran appear there).
step() {
    _name=$1
    shift
    "$@" >"$ART/$_name.log" 2>&1
    _rc=$?
    printf '%s\t%s\n' "$_name" "$_rc" >>"$ART/steps.tsv"
    return "$_rc"
}

# host_port SERVICE PORT: published 127.0.0.1 port of a service of this run.
host_port() {
    compose port "$1" "$2" 2>>"$ART/compose-port.log" | tail -n 1 | sed 's/.*://'
}

# wait_http URL: poll until the URL answers, at most SEARCH_ES_WAIT_SECONDS.
wait_http() {
    _deadline=$(($(date +%s) + SEARCH_ES_WAIT_SECONDS))
    while :; do
        if "$CURL_BIN" --fail --silent --max-time 5 "$1" >/dev/null 2>&1; then
            return 0
        fi
        if [ "$(date +%s)" -ge "$_deadline" ]; then
            return 1
        fi
        sleep 2
    done
}

# diagnostics: compose state and recent logs of this run (never secrets:
# the evaluation database password is a fixed throwaway value).
diagnostics() {
    compose ps --all >"$ART/diagnostics-ps.log" 2>&1
    compose logs --no-color --tail 200 >"$ART/diagnostics-logs.log" 2>&1
}

# cleanup_owned: remove this run's project and verify nothing is left.
# Writes $ART/cleanup = verified | failed | not-owned.
cleanup_owned() {
    if [ "${OWNED:-0}" != 1 ]; then
        printf 'not-owned\n' >"$ART/cleanup"
        return 0
    fi
    step compose-down compose --profile postgres down --volumes --remove-orphans \
        --timeout 30
    _down=$?
    _left=$(compose ps --all --quiet 2>>"$ART/cleanup-verify.log")
    _verify=$?
    if [ "$_down" -eq 0 ] && [ "$_verify" -eq 0 ] && [ -z "$_left" ]; then
        printf 'verified\n' >"$ART/cleanup"
        return 0
    fi
    printf 'failed\n' >"$ART/cleanup"
    printf 'cleanup of %s not verified (down=%s verify=%s left=%s)\n' \
        "$SEARCH_PROJECT" "$_down" "$_verify" "$_left" >&2
    return 1
}

# finish CODE: cleanup, record the final exit code; a cleanup failure turns
# success into exit 5 (never reported as cleaned).
finish() {
    _code=$1
    trap - EXIT INT TERM
    if ! cleanup_owned && [ "$_code" -eq 0 ]; then
        _code=5
    fi
    printf '%s\n' "$_code" >"$ART/exit-code"
    exit "$_code"
}

# install_traps: INT -> 130, TERM -> 143, always through finish().
install_traps() {
    trap 'finish $?' EXIT
    trap 'printf "interrupted\n" >"$ART/interrupted"; finish 130' INT
    trap 'printf "terminated\n" >"$ART/interrupted"; finish 143' TERM
}
