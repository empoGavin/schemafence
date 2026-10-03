#!/usr/bin/env bash
# Install PostgreSQL + pgvector and load the demo schema.
#
# Works on both package-manager families:
#   * Debian / Ubuntu          -> apt  (postgresql, postgresql-N-pgvector)
#   * RHEL 8/9/10, Oracle Linux, Rocky, AlmaLinux, Fedora
#                              -> dnf  (postgresql-server, pgvector)
#
#   bash scripts/setup_pg.sh
#
# The script is idempotent — fix whatever went wrong and run it again.
#
# Environment overrides:
#   DB_NAME            (fence_demo)
#   DB_PASS            (pgvec123)
#   PGVECTOR_VERSION   (v0.8.7)  built from source by default
#   PGVECTOR_FROM_DIST (0)       set to 1 to use the distribution package
#                                (Oracle Linux 10 AppStream ships pgvector 0.6.x)
#                                instead of building the latest from source
#
# Family-specific notes this script handles for you:
#   * RHEL family needs an explicit `postgresql-setup --initdb`; Debian
#     initialises the cluster during package installation.
#   * RHEL family ships `ident` in pg_hba.conf for loopback TCP, which makes
#     password authentication fail. This script rewrites it to scram-sha-256.
#   * RHEL family has SELinux enforcing; a source-built extension needs
#     relabelling or the server refuses to load vector.so.
#   * Neither family has systemd inside thin containers (Cloud Studio,
#     Codespaces) — the cluster is started with pg_ctlcluster/service there.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

DB_NAME="${DB_NAME:-fence_demo}"
DB_PASS="${DB_PASS:-pgvec123}"
PGVECTOR_VERSION="${PGVECTOR_VERSION:-v0.8.7}"

# --- privilege helpers -------------------------------------------------------
# PKG  prefixes package commands; PSQL prefixes psql commands.
PKG="sudo"
PSQL="sudo -u postgres"
if [ "$(id -u)" -eq 0 ]; then
  PKG=""
  if command -v runuser >/dev/null 2>&1; then
    PSQL="runuser -u postgres --"
  fi
fi

# Running psql from an unreadable cwd makes the postgres user print
# "could not change directory to ...". /tmp is always readable.
cd /tmp

step() { printf '\n==> %s\n' "$1"; }
warn() { printf '    [warn] %s\n' "$1"; }
die()  { printf '\n[FAIL] %s\n' "$1" >&2; exit 1; }

# --- which family are we on? -------------------------------------------------
if command -v apt-get >/dev/null 2>&1; then
  FAMILY=debian
elif command -v dnf >/dev/null 2>&1; then
  FAMILY=rhel
elif command -v yum >/dev/null 2>&1; then
  FAMILY=rhel
else
  die "neither apt-get nor dnf is available — install PostgreSQL yourself, then run demo.py --db <dsn>"
fi

step "environment"
# shellcheck disable=SC1091
. /etc/os-release 2>/dev/null || true
echo "    ${PRETTY_NAME:-unknown OS}"
echo "    family=${FAMILY}   user=$(whoami)   pgvector=${PGVECTOR_VERSION}"

# --- install the server ------------------------------------------------------
step "installing postgresql"
if [ "$FAMILY" = debian ]; then
  $PKG apt-get update -qq || warn "apt-get update returned non-zero"
  $PKG apt-get install -y -qq postgresql postgresql-contrib \
    || die "apt-get install postgresql failed"
  PG_VER="$(ls /usr/lib/postgresql/ 2>/dev/null | sort -n | tail -1)"
else
  $PKG dnf -y -q install postgresql-server postgresql-contrib \
    || die "dnf install postgresql-server failed"
  PG_VER="$(rpm -q --qf '%{VERSION}\n' postgresql-server 2>/dev/null | head -1 | cut -d. -f1)"
  if [ -z "${PG_VER}" ]; then
    PG_VER="$(postgres --version 2>/dev/null \
      | sed -n 's/.*[^0-9]\([0-9][0-9]*\)\..*/\1/p')"
  fi
fi
[ -z "${PG_VER}" ] && die "could not detect the PostgreSQL major version"
echo "    postgres major version: ${PG_VER}"

# --- install pgvector --------------------------------------------------------
# Default: build the latest release from source. The RHEL 10 / Oracle Linux 10
# AppStream package is 0.6.x — fine for the demo, but older than upstream.
# Set PGVECTOR_FROM_DIST=1 to prefer the distribution package instead.
step "installing pgvector for pg${PG_VER}"
PGVECTOR_INSTALLED=0
if [ "${PGVECTOR_FROM_DIST:-0}" = "1" ]; then
  if [ "$FAMILY" = debian ]; then
    if $PKG apt-get install -y -qq "postgresql-${PG_VER}-pgvector" 2>/dev/null; then
      PGVECTOR_INSTALLED=1
      echo "    installed from the distribution package"
    fi
  else
    if $PKG dnf -y -q install pgvector 2>/dev/null; then
      PGVECTOR_INSTALLED=1
      echo "    installed from the distribution package (0.6.x on Oracle Linux 10)"
    fi
  fi
  [ "$PGVECTOR_INSTALLED" -eq 0 ] && warn "no distribution package — falling back to a source build"
else
  echo "    building ${PGVECTOR_VERSION} from source (latest; set PGVECTOR_FROM_DIST=1 to use the distro's 0.6.x instead)"
fi

if [ "$PGVECTOR_INSTALLED" -eq 0 ]; then
  if [ "$FAMILY" = debian ]; then
    $PKG apt-get install -y -qq build-essential "postgresql-server-dev-${PG_VER}" git \
      || die "could not install the build toolchain"
    PG_SHAREDIR="/usr/share/postgresql/${PG_VER}"
    PG_LIBDIR="/usr/lib/postgresql/${PG_VER}/lib"
  else
    # OL10/RHEL10: `postgresql-devel` installs but does NOT ship pg_config;
    # `postgresql-server-devel` is what extension builds actually need.
    $PKG dnf -y -q install gcc make git 2>/dev/null \
      || die "could not install gcc/make/git"
    if ! command -v pg_config >/dev/null 2>&1; then
      $PKG dnf -y -q install postgresql-server-devel 2>/dev/null \
        || $PKG dnf -y -q install libpq-devel 2>/dev/null \
        || die "could not install the PostgreSQL development headers"
    fi
    if ! command -v pg_config >/dev/null 2>&1; then
      die "pg_config is still missing — run: sudo dnf install postgresql-server-devel, then rerun this script"
    fi
    PG_SHAREDIR="$(pg_config --sharedir 2>/dev/null)"
    PG_LIBDIR="$(pg_config --pkglibdir 2>/dev/null)"
  fi

  rm -rf /tmp/pgvector
  git clone -q --branch "${PGVECTOR_VERSION}" --depth 1 \
    https://github.com/pgvector/pgvector.git /tmp/pgvector \
    || die "git clone pgvector failed"
  make -s -C /tmp/pgvector >/dev/null || die "pgvector build failed"
  $PKG make -s -C /tmp/pgvector install >/dev/null || die "pgvector install failed"

  # SELinux: a file dropped in by `make install` may carry the wrong type,
  # and the server is then denied permission to load it. Relabel it.
  if command -v restorecon >/dev/null 2>&1; then
    $PKG restorecon -R "${PG_SHAREDIR}" "${PG_LIBDIR}" 2>/dev/null || true
  fi
  echo "    built and installed"
fi

# --- bring the cluster up ----------------------------------------------------
step "starting the cluster"
if [ "$FAMILY" = debian ]; then
  # Thin containers have no systemd, so try the tools that work without it.
  if command -v pg_ctlcluster >/dev/null 2>&1; then
    $PKG pg_ctlcluster "${PG_VER}" main start 2>/dev/null || true
  fi
  $PKG service postgresql start 2>/dev/null || true
  $PKG systemctl enable --now postgresql >/dev/null 2>&1 || true
else
  # RHEL family: the cluster is NOT initialised by the package.
  if [ ! -f /var/lib/pgsql/data/PG_VERSION ]; then
    echo "    initialising /var/lib/pgsql/data (RHEL family requires this)"
    $PKG postgresql-setup --initdb --unit postgresql >/dev/null 2>&1 \
      || $PKG postgresql-setup --initdb >/dev/null 2>&1 \
      || warn "postgresql-setup failed — run it by hand and check the output"
  fi
  $PKG systemctl enable --now postgresql >/dev/null 2>&1 || true
fi
sleep 2

if ! $PSQL psql -tAc "SELECT 1" >/dev/null 2>&1; then
  echo
  echo "    the cluster did not come up. diagnostics:"
  if [ "$FAMILY" = rhel ]; then
    echo "      systemctl status postgresql --no-pager"
    echo "      journalctl -u postgresql -n 50 --no-pager"
    echo "      ls -l /var/lib/pgsql/data/PG_VERSION    # missing? run: sudo postgresql-setup --initdb"
  else
    echo "      pg_lsclusters"
    echo "      tail -50 /var/log/postgresql/postgresql-${PG_VER}-main.log"
  fi
  exit 1
fi
echo "    cluster is up"

# --- TCP password authentication --------------------------------------------
# RHEL family ships `ident` for host connections in pg_hba.conf. With no ident
# daemon answering, every password login over 127.0.0.1 fails — which is exactly
# how live mode (--db postgresql://...) connects. Debian already ships scram.
step "configuring password authentication"
HBA_FILE="$($PSQL psql -tAc 'SHOW hba_file' 2>/dev/null | tr -d '[:space:]')"
# The data directory is mode 700 (postgres-only): a plain `[ -f ]` as the
# invoking user cannot traverse it and would wrongly report "not found".
# Run the test under $PKG (sudo/root), which can always reach it.
if [ -n "${HBA_FILE}" ] && $PKG test -f "${HBA_FILE}"; then
  echo "    pg_hba.conf : ${HBA_FILE}"
  if grep -qE '^[[:space:]]*host[[:space:]]+.*[[:space:]]ident[[:space:]]*$' "${HBA_FILE}"; then
    $PKG cp "${HBA_FILE}" "${HBA_FILE}.bak.$(date +%Y%m%d%H%M%S)"
    $PKG sed -i -E \
      's/^([[:space:]]*host[[:space:]]+.*[[:space:]])ident([[:space:]]*)$/\1scram-sha-256\2/' \
      "${HBA_FILE}"
    echo "    rewrote 'ident' -> 'scram-sha-256' for TCP (RHEL default), backup kept"
    if command -v systemctl >/dev/null 2>&1; then
      $PKG systemctl reload postgresql >/dev/null 2>&1 || true
    else
      $PKG service postgresql reload >/dev/null 2>&1 || true
    fi
  else
    echo "    already using a password method for TCP"
  fi
else
  warn "could not locate pg_hba.conf — check password auth manually"
fi

# --- password, database, schema ---------------------------------------------
step "setting the password and creating ${DB_NAME}"
$PSQL psql -q -c "ALTER USER postgres PASSWORD '${DB_PASS}';" \
  || die "could not set the postgres password"
if ! $PSQL psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
  $PSQL createdb "${DB_NAME}" || die "could not create the database ${DB_NAME}"
fi

step "enabling the vector extension in ${DB_NAME}"
if $PSQL psql -q -d "${DB_NAME}" -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>/dev/null; then
  echo "    extension 'vector' is active"
else
  warn "pgvector is not loadable — the offline demo still works, live vector search will not"
fi

step "loading the demo schema"
# Feed the SQL via stdin, not `-f`: the postgres user cannot read files under
# the invoking user's home (e.g. /home/<user> is not world-traversable), while
# the stdin redirect is performed by the invoking user itself.
$PSQL psql -q -d "${DB_NAME}" < "${REPO_ROOT}/examples/sample_schema.sql" \
  || die "loading examples/sample_schema.sql failed"

step "collecting statistics (pg_stats is what the NULL-fraction check reads)"
$PSQL psql -q -d "${DB_NAME}" -c "ANALYZE;" || warn "ANALYZE failed"

# --- summary -----------------------------------------------------------------
VECTOR_VER="$($PSQL psql -d "${DB_NAME}" -tAc \
  "SELECT COALESCE((SELECT extversion FROM pg_extension WHERE extname='vector'),'NOT INSTALLED')" \
  2>/dev/null | tr -d '[:space:]')"
TABLE_COUNT="$($PSQL psql -d "${DB_NAME}" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='shop'" \
  2>/dev/null | tr -d '[:space:]')"

step "done"
echo "    postgres : ${PG_VER}   pgvector: ${VECTOR_VER:-unknown}   shop tables: ${TABLE_COUNT:-unknown}"
echo "    database : ${DB_NAME}   user: postgres   password: ${DB_PASS}"
echo
echo "    offline demo :  python3 demo.py"
echo "    live demo    :  python3 demo.py --db postgresql://postgres:${DB_PASS}@localhost:5432/${DB_NAME}"
echo
echo "    the application connects over 127.0.0.1:5432, so no firewall change is needed."
echo "    stop the VM when you are done, so it stops using your machine's memory."
