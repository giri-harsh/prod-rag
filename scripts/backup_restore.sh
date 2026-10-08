#!/usr/bin/env bash
# Logical backup / restore of the application database with pg_dump -Fc and pg_restore.
#
#   scripts/backup_restore.sh backup
#   scripts/backup_restore.sh list
#   scripts/backup_restore.sh verify  [FILE] [--restore-test]
#   scripts/backup_restore.sh restore [FILE] [--target-db NAME] [--drop-and-recreate --yes]
#
# FILE defaults to the newest *.dump in BACKUP_DIR.
#
# Environment
#   DIRECT_DATABASE_URL   connection used for dump/restore (session mode, NOT the pooler). Preferred.
#   DATABASE_URL          used when DIRECT_DATABASE_URL is unset; refused if the host looks like a pooler
#                         (pgbouncer/pooler in the name, or port 6432) unless ALLOW_POOLER=1.
#   ADMIN_DATABASE_URL    optional role with CREATEDB used for DROP/CREATE DATABASE (default: the same
#                         credentials against database "postgres").
#   BACKUP_DIR            where dumps live (default ./backups).
#   BACKUP_KEEP           if set to N>0, `backup` keeps only the N newest dumps of this database.
#   PGBIN                 directory holding pg_dump/pg_restore/psql (default: PATH).
#
# Passwords are taken from the URL and handed to the tools through PGPASSWORD; they are never printed and
# never appear on a command line. Every failure prints one line "backup_restore: error: <reason>" on stderr
# and exits non-zero (1 = runtime failure, 2 = usage / refused without confirmation).
# Dumps are restored with --no-owner --no-acl (objects belong to the restoring role) in ONE transaction.

set -u
PROG="backup_restore"

die() { printf '%s: error: %s\n' "$PROG" "$*" >&2; exit 1; }
usage_err() { printf '%s: error: %s\n' "$PROG" "$*" >&2; printf 'usage: %s backup | list | verify [FILE] [--restore-test] | restore [FILE] [--target-db NAME] [--drop-and-recreate --yes]\n' "$PROG" >&2; exit 2; }
info() { printf '%s: %s\n' "$PROG" "$*"; }
warn() { printf '%s: warning: %s\n' "$PROG" "$*" >&2; }

# NOTE: helpers below never call die inside $(...) (that would only exit the subshell); they return values
# through the global R / PSQL_OUT instead.

urldecode() { printf '%b' "${1//%/\\x}"; }

getv() { local n=$1; printf '%s' "${!n}"; }

# parse_url PREFIX URL -> PREFIX_USER _PASS _HOST _PORT _DB _SSLMODE _SSLROOTCERT _SSLCERT _SSLKEY _CONNECT_TIMEOUT _OPTIONS
parse_url() {
  local p=$1 url=$2 rest q="" path="" auth="" hostport user pass host port db k v kv after
  case $url in postgresql://*|postgres://*) ;; *) return 1 ;; esac
  rest=${url#*://}
  case $rest in *\?*) q=${rest#*\?}; rest=${rest%%\?*} ;; esac
  case $rest in */*) path=${rest#*/}; rest=${rest%%/*} ;; esac
  hostport=$rest
  case $rest in *@*) auth=${rest%@*}; hostport=${rest##*@} ;; esac
  user=$auth; pass=""
  case $auth in *:*) user=${auth%%:*}; pass=${auth#*:} ;; esac
  host=""; port=""
  case $hostport in
    \[*\]*) host=${hostport%%]*}; host=${host#[}; after=${hostport#*]}; case $after in :*) port=${after#:} ;; esac ;;
    *:*) host=${hostport%%:*}; port=${hostport#*:} ;;
    *) host=$hostport ;;
  esac
  db=$(urldecode "$path"); user=$(urldecode "$user"); pass=$(urldecode "$pass"); host=$(urldecode "$host")
  for k in SSLMODE SSLROOTCERT SSLCERT SSLKEY CONNECT_TIMEOUT OPTIONS; do printf -v "${p}_$k" '%s' ""; done
  while [ -n "$q" ]; do
    kv=${q%%&*}
    case $q in *\&*) q=${q#*&} ;; *) q="" ;; esac
    k=${kv%%=*}; v=$(urldecode "${kv#*=}")
    case $k in
      host) host=$v ;;
      port) port=$v ;;
      sslmode) printf -v "${p}_SSLMODE" '%s' "$v" ;;
      sslrootcert) printf -v "${p}_SSLROOTCERT" '%s' "$v" ;;
      sslcert) printf -v "${p}_SSLCERT" '%s' "$v" ;;
      sslkey) printf -v "${p}_SSLKEY" '%s' "$v" ;;
      connect_timeout) printf -v "${p}_CONNECT_TIMEOUT" '%s' "$v" ;;
      options) printf -v "${p}_OPTIONS" '%s' "$v" ;;
    esac
  done
  printf -v "${p}_USER" '%s' "$user"; printf -v "${p}_PASS" '%s' "$pass"; printf -v "${p}_HOST" '%s' "$host"
  printf -v "${p}_PORT" '%s' "$port"; printf -v "${p}_DB" '%s' "$db"
  return 0
}

# conn PREFIX DBNAME CMD... : run CMD with the libpq environment of connection PREFIX against database DBNAME
conn() {
  local p=$1 db=$2; shift 2
  (
    [ -n "$(getv "${p}_USER")" ] && export PGUSER="$(getv "${p}_USER")"
    [ -n "$(getv "${p}_PASS")" ] && export PGPASSWORD="$(getv "${p}_PASS")"
    [ -n "$(getv "${p}_HOST")" ] && export PGHOST="$(getv "${p}_HOST")"
    [ -n "$(getv "${p}_PORT")" ] && export PGPORT="$(getv "${p}_PORT")"
    [ -n "$(getv "${p}_SSLMODE")" ] && export PGSSLMODE="$(getv "${p}_SSLMODE")"
    [ -n "$(getv "${p}_SSLROOTCERT")" ] && export PGSSLROOTCERT="$(getv "${p}_SSLROOTCERT")"
    [ -n "$(getv "${p}_SSLCERT")" ] && export PGSSLCERT="$(getv "${p}_SSLCERT")"
    [ -n "$(getv "${p}_SSLKEY")" ] && export PGSSLKEY="$(getv "${p}_SSLKEY")"
    [ -n "$(getv "${p}_CONNECT_TIMEOUT")" ] && export PGCONNECT_TIMEOUT="$(getv "${p}_CONNECT_TIMEOUT")"
    [ -n "$(getv "${p}_OPTIONS")" ] && export PGOPTIONS="$(getv "${p}_OPTIONS")"
    export PGDATABASE="$db"
    exec "$@"
  )
}

scrub() { # remove any password value from a message (prints the result)
  local m=$1 pw
  for pw in "${T_PASS:-}" "${A_PASS:-}"; do
    [ -n "$pw" ] && m=${m//"$pw"/***}
  done
  printf '%s' "$m"
}

lastline() { printf '%s' "$1" | tr -d '\r' | awk 'NF{sub(/^[[:space:]]+/, ""); print; exit}'; }  # first non-empty line (the most specific one)

find_bin() { # find_bin NAME -> R (dies when missing)
  local name=$1 c
  if [ -n "${PGBIN:-}" ]; then
    for c in "$PGBIN/$name" "$PGBIN/$name.exe"; do
      if [ -f "$c" ]; then R=$c; return 0; fi
    done
    die "$name not found in PGBIN=$PGBIN"
  fi
  c=$(command -v "$name" 2>/dev/null) || die "$name not found (add the PostgreSQL client tools to PATH or set PGBIN)"
  R=$c
}

valid_name() { case $1 in ""|*[!A-Za-z0-9_.-]*) return 1 ;; *) return 0 ;; esac; }

sha_of() { # FILE -> R
  if command -v sha256sum >/dev/null 2>&1; then R=$(sha256sum "$1" | awk '{print $1}')
  elif command -v shasum >/dev/null 2>&1; then R=$(shasum -a 256 "$1" | awk '{print $1}')
  else die "neither sha256sum nor shasum is available"; fi
}

# psql_q PREFIX DBNAME SQL -> PSQL_OUT (dies with one line on failure)
psql_q() {
  local out rc
  out=$(conn "$1" "$2" "$PSQL" -X -q -At -v ON_ERROR_STOP=1 -c "$3" 2>&1); rc=$?
  if [ $rc -ne 0 ]; then die "$(scrub "psql failed on database $2 (exit $rc): $(lastline "$out")")"; fi
  PSQL_OUT=$out
}

Q_TABLES="SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') AND c.relkind IN ('r','p')"
Q_RELATIONS="SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast') AND c.relkind IN ('r','p','v','m','S','f')"

BACKUP_DIR=${BACKUP_DIR:-./backups}

load_conn() {
  local url=${DIRECT_DATABASE_URL:-}
  if [ -z "$url" ]; then
    url=${DATABASE_URL:-}
    [ -n "$url" ] || usage_err "set DIRECT_DATABASE_URL (or DATABASE_URL)"
    warn "DIRECT_DATABASE_URL is not set; using DATABASE_URL. Dump/restore must NOT go through a transaction-mode pooler."
  fi
  parse_url T "$url" || usage_err "the database URL must start with postgresql:// or postgres://"
  [ -n "$T_DB" ] || usage_err "the database URL has no database name"
  if [ "${ALLOW_POOLER:-0}" != "1" ]; then
    case "$T_HOST" in
      *[Pp]gbouncer*|*pooler*) die "host '$T_HOST' looks like a connection pooler; use DIRECT_DATABASE_URL (session mode) or set ALLOW_POOLER=1" ;;
    esac
    [ "$T_PORT" = "6432" ] && die "port 6432 looks like pgbouncer; use DIRECT_DATABASE_URL (session mode) or set ALLOW_POOLER=1"
  fi
  if [ -n "${ADMIN_DATABASE_URL:-}" ]; then
    parse_url A "$ADMIN_DATABASE_URL" || usage_err "ADMIN_DATABASE_URL must start with postgresql:// or postgres://"
    A_MAINT_DB=${A_DB:-postgres}; [ -n "$A_MAINT_DB" ] || A_MAINT_DB=postgres
  else
    parse_url A "$url"; A_MAINT_DB=postgres
  fi
}

load_tools() {
  find_bin pg_dump; PG_DUMP=$R
  find_bin pg_restore; PG_RESTORE=$R
  find_bin psql; PSQL=$R
}

where() { printf '%s:%s/%s' "${T_HOST:-local}" "${T_PORT:-5432}" "$1"; }

latest_dump() { # -> R
  local f
  f=$(ls -1t "$BACKUP_DIR"/*.dump 2>/dev/null | head -n 1)
  [ -n "$f" ] || die "no *.dump files in $BACKUP_DIR"
  R=$f
}

check_file() { # FILE -> FILE_SHA (dies on missing/empty/checksum mismatch)
  local f=$1 want
  [ -f "$f" ] || die "backup file not found: $f"
  [ -s "$f" ] || die "backup file is empty: $f"
  sha_of "$f"; FILE_SHA=$R
  if [ -f "$f.sha256" ]; then
    want=$(awk '{print $1}' "$f.sha256" | head -n 1)
    [ "$want" = "$FILE_SHA" ] || die "checksum mismatch for $f (the file was modified or truncated)"
  else
    warn "no $f.sha256 sidecar; only the archive structure is checked"
  fi
}

toc_entries() { # FILE -> R = number of TOC entries (dies when pg_restore cannot read the archive)
  local out rc
  out=$("$PG_RESTORE" --list "$1" 2>&1); rc=$?
  [ $rc -eq 0 ] || die "not a readable pg_dump custom-format archive: $(lastline "$out")"
  R=$(printf '%s\n' "$out" | grep -c '^[0-9]')
}

# ---------------------------------------------------------------------------------------------------------
cmd_backup() {
  [ $# -eq 0 ] || usage_err "backup takes no arguments"
  load_conn; load_tools
  valid_name "$T_DB" || die "unsupported characters in database name '$T_DB'"
  mkdir -p "$BACKUP_DIR" || die "cannot create BACKUP_DIR=$BACKUP_DIR"
  local ts base final tmp out rc n=0 size sha f i
  ts=$(date -u +%Y%m%dT%H%M%SZ)
  base="$BACKUP_DIR/${T_DB}_${ts}"
  final="$base.dump"
  while [ -e "$final" ]; do n=$((n + 1)); final="${base}-$n.dump"; done
  tmp="$final.partial"
  out=$(conn T "$T_DB" "$PG_DUMP" --format=custom --no-owner --no-acl --file "$tmp" 2>&1); rc=$?
  if [ $rc -ne 0 ]; then rm -f "$tmp"; die "$(scrub "pg_dump failed (exit $rc): $(lastline "$out")")"; fi
  [ -s "$tmp" ] || { rm -f "$tmp"; die "pg_dump produced an empty file"; }
  if ! out=$("$PG_RESTORE" --list "$tmp" 2>&1); then rm -f "$tmp"; die "dump is not a readable archive: $(lastline "$out")"; fi
  sha_of "$tmp"; sha=$R
  mv "$tmp" "$final" || { rm -f "$tmp"; die "cannot move the finished dump into place"; }
  printf '%s  %s\n' "$sha" "$(basename "$final")" > "$final.sha256.partial" && mv "$final.sha256.partial" "$final.sha256" \
    || die "cannot write the checksum sidecar for $final"
  size=$(wc -c < "$final" | tr -d ' ')
  info "backup ok: $final ($size bytes, sha256 ${sha:0:12}..., from $(where "$T_DB"))"
  case ${BACKUP_KEEP:-} in
    "") ;;
    *[!0-9]*) warn "BACKUP_KEEP='$BACKUP_KEEP' is not a number; nothing pruned" ;;
    *)
      if [ "$BACKUP_KEEP" -gt 0 ]; then
        i=0
        ls -1t "$BACKUP_DIR/${T_DB}_"*.dump 2>/dev/null | while IFS= read -r f; do
          i=$((i + 1))
          if [ $i -gt "$BACKUP_KEEP" ]; then rm -f "$f" "$f.sha256"; info "pruned $f"; fi
        done
      fi ;;
  esac
}

cmd_list() {
  [ $# -eq 0 ] || usage_err "list takes no arguments"
  [ -d "$BACKUP_DIR" ] || { info "no backups ($BACKUP_DIR does not exist)"; return 0; }
  local out
  out=$(ls -1t "$BACKUP_DIR"/*.dump 2>/dev/null)
  [ -n "$out" ] || { info "no *.dump files in $BACKUP_DIR"; return 0; }
  printf '%s\n' "$out" | while IFS= read -r f; do
    flag="no-sha256"; [ -f "$f.sha256" ] && flag="sha256"
    printf '%s  %s bytes  %s\n' "$(basename "$f")" "$(wc -c < "$f" | tr -d ' ')" "$flag"
  done
}

cmd_verify() {
  local file="" rt=0 n scratch out rc t
  while [ $# -gt 0 ]; do
    case $1 in
      --restore-test) rt=1 ;;
      -*) usage_err "unknown option for verify: $1" ;;
      *) [ -z "$file" ] || usage_err "verify takes at most one FILE"; file=$1 ;;
    esac
    shift
  done
  load_tools
  if [ -z "$file" ]; then latest_dump; file=$R; fi
  check_file "$file"
  toc_entries "$file"; n=$R
  if [ $rt -eq 0 ]; then
    info "verify ok: $file ($n TOC entries, sha256 ${FILE_SHA:0:12}...)"
    return 0
  fi
  load_conn
  scratch="${T_DB}_verify_$$"
  valid_name "$scratch" || die "unsupported characters in database name '$scratch'"
  psql_q A "$A_MAINT_DB" "CREATE DATABASE \"$scratch\""
  # shellcheck disable=SC2064
  trap "conn A '$A_MAINT_DB' '$PSQL' -X -q -At -c 'DROP DATABASE IF EXISTS \"$scratch\" WITH (FORCE)' >/dev/null 2>&1" EXIT
  out=$(conn T "$scratch" "$PG_RESTORE" --no-owner --no-acl --exit-on-error --single-transaction --dbname "$scratch" "$file" 2>&1); rc=$?
  if [ $rc -ne 0 ]; then die "$(scrub "restore test failed (exit $rc): $(lastline "$out")")"; fi
  psql_q T "$scratch" "$Q_TABLES"; t=$PSQL_OUT
  info "verify ok: $file ($n TOC entries, sha256 ${FILE_SHA:0:12}..., test restore into a scratch database produced $t tables; scratch dropped)"
}

cmd_restore() {
  local file="" target="" drop=0 yes=0 n exists out rc rels tables
  while [ $# -gt 0 ]; do
    case $1 in
      --drop-and-recreate) drop=1 ;;
      --yes) yes=1 ;;
      --target-db) shift; [ $# -gt 0 ] || usage_err "--target-db needs a name"; target=$1 ;;
      --target-db=*) target=${1#*=} ;;
      -*) usage_err "unknown option for restore: $1" ;;
      *) [ -z "$file" ] || usage_err "restore takes at most one FILE"; file=$1 ;;
    esac
    shift
  done
  load_conn; load_tools
  [ -n "$target" ] || target=$T_DB
  valid_name "$target" || die "unsupported characters in database name '$target'"
  valid_name "$T_USER" || die "unsupported characters in the database user name"
  if [ $drop -eq 1 ] && [ $yes -ne 1 ]; then
    printf '%s: error: refusing to drop and recreate database "%s" without --yes\n' "$PROG" "$target" >&2
    exit 2
  fi
  if [ -z "$file" ]; then latest_dump; file=$R; fi
  check_file "$file"
  toc_entries "$file"; n=$R
  [ "$A_MAINT_DB" != "$target" ] || die "the admin connection database equals the target ($target); point ADMIN_DATABASE_URL at the postgres database"

  psql_q A "$A_MAINT_DB" "SELECT 1 FROM pg_database WHERE datname = '$target'"; exists=$PSQL_OUT
  if [ "$exists" = "1" ] && [ $drop -eq 1 ]; then
    psql_q A "$A_MAINT_DB" "DROP DATABASE \"$target\" WITH (FORCE)"
    info "dropped database $(where "$target")"
    exists=""
  fi
  if [ "$exists" != "1" ]; then
    if [ -n "$A_USER" ] && [ "$A_USER" != "$T_USER" ]; then
      psql_q A "$A_MAINT_DB" "CREATE DATABASE \"$target\" OWNER \"$T_USER\""
    else
      psql_q A "$A_MAINT_DB" "CREATE DATABASE \"$target\""
    fi
    info "created database $(where "$target")"
  else
    psql_q T "$target" "$Q_RELATIONS"; rels=$PSQL_OUT
    [ "$rels" = "0" ] || die "database '$target' is not empty ($rels relations); use --drop-and-recreate --yes, or --target-db NEW"
  fi
  out=$(conn T "$target" "$PG_RESTORE" --no-owner --no-acl --exit-on-error --single-transaction --dbname "$target" "$file" 2>&1); rc=$?
  if [ $rc -ne 0 ]; then die "$(scrub "pg_restore failed (exit $rc): $(lastline "$out")")"; fi
  psql_q T "$target" "ANALYZE"
  psql_q T "$target" "$Q_TABLES"; tables=$PSQL_OUT
  info "restore ok: $file -> $(where "$target") ($n TOC entries, $tables tables)"
}

# ---------------------------------------------------------------------------------------------------------
[ $# -ge 1 ] || usage_err "missing subcommand"
sub=$1; shift
case $sub in
  backup) cmd_backup "$@" ;;
  list) cmd_list "$@" ;;
  verify) cmd_verify "$@" ;;
  restore) cmd_restore "$@" ;;
  -h|--help|help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  *) usage_err "unknown subcommand: $sub" ;;
esac
