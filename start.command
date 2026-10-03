#!/bin/bash
# macOS: double-click this file in Finder to start Job Agent (it opens a Terminal window; keep it open).
# Same as running ./start.sh in Terminal. If macOS says it can't be opened, right-click it and choose Open.
cd "$(dirname "$0")" && exec bash ./start.sh "$@"
