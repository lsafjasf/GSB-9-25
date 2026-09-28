"""Show the diagnostic produced when a hunk no longer matches the target."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from linepatch import ApplyConfig, HunkConflictError, apply_file_patch, parse_patch

PATCH = """\
--- a/config.ini
+++ b/config.ini
@@ -3,5 +3,5 @@
 [server]
 host = localhost
-port = 8080
+port = 9090
 workers = 4
 debug = off
"""

# Someone else edited the file after the patch was created: the line the
# patch expects at line 5 now says something else, and the whole block the
# patch was based on also exists further down (copied into a second section).
target = (
    "# example config\n"
    "\n"
    "[server]\n"
    "host = localhost\n"
    "port = 8081\n"          # <- tampered: patch expected "port = 8080"
    "workers = 4\n"
    "debug = off\n"
    "\n"
    "[server]\n"             # <- the original block, copied at offset +6
    "host = localhost\n"
    "port = 8080\n"
    "workers = 4\n"
    "debug = off\n"
)

patch = parse_patch(PATCH)
try:
    apply_file_patch(patch.files[0], target, ApplyConfig(max_offset=2))
except HunkConflictError as err:
    print(err)
