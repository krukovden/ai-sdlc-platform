#!/bin/sh
set -eu
# The test creates its own HOME, source checkout and local git release remote.
cd "$(dirname "$0")/.."
node --test test/install-smoke.test.js
