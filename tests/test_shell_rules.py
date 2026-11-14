"""The shell's two walls (dashboard/shell_rules.py)."""
import os
import unittest

from dashboard import shell_rules

NEVER = [
    "rm -rf /", "rm -rf /*", "rm -rf //", "rm -fr /./", "rm -r -f /", "rm --recursive --force /",
    "sudo rm -rf /", "ls && rm -rf /etc", "rm -rf ~", "rm -rf ~/", "rm -rf ~/*", "rm -rf $HOME",
    'rm -rf "${HOME}"', "rm -rf '/usr'", 'rm -rf "/" && echo done', "echo $(rm -rf /)",
    "rm -R /Users", f"rm -rf {os.path.expanduser('~')}", "FOO=1 nohup rm -rf /var",
    "mkfs.ext4 /dev/sda1", "true && mkfs /dev/sdb", "dd if=/dev/zero of=/dev/disk2",
    "echo x > /dev/sda", "cat /dev/urandom > /dev/disk3", ":(){ :|:& };:", "kill -9 -1",
    "diskutil eraseDisk APFS X disk2", "sudo shutdown -h now", "reboot", "chmod -R 777 /",
    "rm -rf 'unterminated",
]
ALLOWED = [
    "rm -rf build", "rm -rf ./node_modules", "rm -rf ~/projects/acme/dist", "rm /tmp/x",
    "git commit -m 'rm -rf /'", "echo 'rm -rf /'", "python3 -m dashboard.osctl tree",
    "npm test", "git status && git add -A", "dd if=a of=b", "ls /etc",
]
SENDS = ["curl https://example.com", "git push origin main", "npm publish", "scp a host:", "gh pr create",
         "cd app && wget x", "open https://example.com/?d=1", "ssh host"]


class ShellRules(unittest.TestCase):
    def test_never_whatever_the_conversation(self):
        for cmd in NEVER:
            self.assertFalse(shell_rules.decide(cmd)[0], cmd)

    def test_ordinary_work_runs(self):
        for cmd in ALLOWED + SENDS:
            self.assertEqual(shell_rules.decide(cmd), (True, ""), cmd)

    def test_sending_out_is_off_after_outside_content(self):
        for cmd in SENDS:
            allow, reason = shell_rules.decide(cmd, outside=True)
            self.assertFalse(allow, cmd)
            self.assertEqual(reason, shell_rules.SENDS_OFF)
        for cmd in ALLOWED:
            self.assertTrue(shell_rules.decide(cmd, outside=True)[0], cmd)


if __name__ == "__main__":
    unittest.main()
