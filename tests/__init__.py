"""Test package. Pin LEOBOT_HOME to a temp dir before anything else imports."""
import atexit
import os
import shutil
import tempfile

# The home folder the shell named before the pin (None: the default ~/.leobot). Read only by the
# privacy guard (tests/test_no_usage_data.py), which learns the real project names from its
# data/projects/ so they can never be committed. Nothing in the tests writes there.
ORIGINAL_HOME = os.environ.get("LEOBOT_HOME") or None
_TEST_HOME = tempfile.mkdtemp(prefix="leobot-test-home-")
os.environ["LEOBOT_HOME"] = _TEST_HOME
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)
