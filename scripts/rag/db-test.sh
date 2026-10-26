#!/bin/sh
set -eu
exec sh "$(dirname "$0")/with-test-db.sh" sh "$(dirname "$0")/db-suite.sh"
