"""Test package. Pin LEOBOT_HOME to a temp dir before anything else imports."""
import atexit
import os
import shutil
import tempfile

_TEST_HOME = tempfile.mkdtemp(prefix="leobot-test-home-")
os.environ["LEOBOT_HOME"] = _TEST_HOME
atexit.register(shutil.rmtree, _TEST_HOME, ignore_errors=True)
