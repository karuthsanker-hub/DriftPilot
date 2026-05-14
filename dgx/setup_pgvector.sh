#!/usr/bin/env bash
# Setup PostgreSQL + pgvector on DGX Spark for the Brain server.
#
# Prerequisites: PostgreSQL 15+ installed
#   Ubuntu/Debian: sudo apt install postgresql postgresql-contrib
#   Then install pgvector extension:
#     cd /tmp && git clone https://github.com/pgvector/pgvector.git
#     cd pgvector && make && sudo make install
#
# Usage: bash setup_pgvector.sh

set -euo pipefail

DB_NAME="${BRAIN_PG_DB:-brain}"
DB_USER="${BRAIN_PG_USER:-brain}"
DB_PASS="${BRAIN_PG_PASS:-brain}"

echo "Setting up PostgreSQL for Brain server..."
echo "  Database: $DB_NAME"
echo "  User:     $DB_USER"

# Create user and database (idempotent)
sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASS';" 2>/dev/null || echo "  User $DB_USER already exists"
sudo -u postgres psql -c "CREATE DATABASE $DB_NAME OWNER $DB_USER;" 2>/dev/null || echo "  Database $DB_NAME already exists"
sudo -u postgres psql -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>/dev/null || echo "  pgvector extension already installed"

# Grant privileges
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;" 2>/dev/null

echo ""
echo "PostgreSQL ready. Connection string:"
echo "  postgresql://$DB_USER:$DB_PASS@localhost:5432/$DB_NAME"
echo ""
echo "To use pgvector backend, start brain server with:"
echo "  BRAIN_DB_BACKEND=pgvector BRAIN_PG_DSN=postgresql://$DB_USER:$DB_PASS@localhost:5432/$DB_NAME python brain_server.py"
