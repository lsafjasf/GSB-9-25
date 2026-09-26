"""Demo: what a conflict diagnostic looks like when the target was modified."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from linepatch import ApplyError, apply_to_text, parse_patch

PATCH = """\
--- a/config.txt
+++ b/config.txt
@@ -3,5 +3,5 @@
 timeout = 30
 retries = 3
-mode = fast
+mode = safe
 verbose = true
 logfile = /var/log/app.log
"""

# Someone else edited the target file in the meantime:
TARGET = """\
# app config
version = 2
timeout = 30
retries = 5
mode = fast
verbose = true
logfile = /var/log/app.log
"""

patch = parse_patch(PATCH)
try:
    apply_to_text(TARGET, patch.files[0])
except ApplyError as e:
    print(e)
