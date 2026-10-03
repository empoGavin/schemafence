#!/usr/bin/env bash
# Install PostgreSQL + pgvector and load the demo schema.
#
# Written for containers that have no systemd (Cloud Studio, Codespaces,
# plain Docker images), where `systemctl start postgresql` does not work
# and the cluster has to be started with pg_ctlcluster or service instead.
#
#   bash scripts/setup_pg.sh
#
# Environment overrides: DB_NAME (fence_demo), DB_PASS (pgvec123)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

DB_NAME="${DB_NAME:-fence_demo}"
DB_PASS="${DB_PASS:-pgvec123}"

SUDO="sudo"
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
fi

step() { printf '\n==> %s\n' "$1"; }

step "environment"
. /etc/os-release 2>/dev/null || true
echo "    ${PRETTY_NAME:-unknown OS}   user=$(whoami)"

step "installing postgresql"
$SUDO apt-get update -qq
$SUDO apt-get install -y -qq postgresql postgresql-contrib

PG_VER="$(ls /usr/lib/postgresql/ 2>/dev/null | sort -n | tail -1)"
if [ -z "${PG_VER}" ]; then
  echo "    could not detect the PostgreSQL version — aborting"
  exit 1
fi
echo "    postgres major version: ${PG_VER}"

step "installing pgvector for pg${PG_VER}"
if $SUDO apt-get install -y -qq "postgresql-${PG_VER}-pgvector" 2>/dev/null; then
  echo "    installed from the distribution package"
else
  echo "    no package for pg${PG_VER} — building from source (about a minute)"
  $SUDO apt-get install -y -qq build-essential "postgresql-server-dev-${PG_VER}" git
  rm -rf /tmp/pgvector
  git clone -q --branch v0.8.0 --depth 1 https://github.com/pgvector/pgvector.git /tmp/pgvector
  make -s -C /tmp/pgvector >/dev/null
  $SUDO make -s -C /tmp/pgvector install >/dev/null
  echo "    built and installed"
fi

step "starting the cluster (no systemd in this container)"
if command -v pg_ctlcluster >/dev/null 2>&1; then
  $SUDO pg_ctlcluster "${PG_VER}" main start 2>/dev/null || true
fi
$SUDO service postgresql start 2>/dev/null || true
sleep 2

if ! $SUDO -u postgres psql -tAc "SELECT 1" >/dev/null 2>&1; then
  echo "    the cluster did not come up."
  echo "    check: sudo pg_lsclusters   /   tail -50 /var/log/postgresql/*.log"
  exit 1
fi
echo "    cluster is up"

step "setting the password and creating ${DB_NAME}"
cd /tmp
$SUDO -u postgres psql -q -c "ALTER USER postgres PASSWORD '${DB_PASS}';"
if ! $SUDO -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1; then
  $SUDO -u postgres createdb "${DB_NAME}"
fi

step "loading the demo schema"
$SUDO -u postgres psql -q -d "${DB_NAME}" -f "${REPO_ROOT}/examples/sample_schema.sql"

step "collecting statistics (pg_stats is what the NULL-fraction check reads)"
$SUDO -u postgres psql -q -d "${DB_NAME}" -c "ANALYZE;"

step "done"
echo "    database : ${DB_NAME}   user: postgres   password: ${DB_PASS}"
echo
echo "    offline demo :  python demo.py"
echo "    live demo    :  python demo.py --db postgresql://postgres:${DB_PASS}@localhost:5432/${DB_NAME}"
echo
echo "    inside this container the app connects over localhost:5432."
echo "    remember to stop the workspace when you are done, so it stops using your quota."
