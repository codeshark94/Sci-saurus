"""Offline process-group lifetime checks for bounded solver execution."""
import os
import signal
import subprocess
import sys
import time
import unittest

from scisaurus.runtime.program_sandbox import capture_process


@unittest.skipUnless(hasattr(os, "fork"), "requires POSIX process groups")
class ProgramProcessLifetimeTests(unittest.TestCase):
    def capture(self, source, *, timeout=3, max_bytes=4096):
        process = subprocess.Popen([sys.executable, "-c", source], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, bufsize=0)
        self.addCleanup(self.stop, process.pid)
        started = time.monotonic()
        result = capture_process(process, input_bytes=b"", timeout_seconds=timeout,
                                 max_bytes=max_bytes, mode="fixture")
        return result, time.monotonic() - started

    @staticmethod
    def stop(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def assert_child_stopped(self, pid):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = subprocess.run(["ps", "-p", str(pid), "-o", "state="],
                                    capture_output=True, text=True).stdout.strip()
            if not status or status.startswith("Z"):
                return
            time.sleep(.05)
        self.fail("owned descendant remained running after the program ended")

    def test_parent_success_drains_output_and_stops_inherited_pipe_owner(self):
        result, elapsed = self.capture("import os,time; p=os.fork();\n"
            "if p==0: time.sleep(30)\nelse: print(p,flush=True)")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(result.timed_out)
        self.assertLess(elapsed, 2)
        self.assert_child_stopped(int(result.stdout))

    def test_nonzero_parent_keeps_status_and_stops_descendant(self):
        result, elapsed = self.capture("import os,time,sys; p=os.fork();\n"
            "if p==0: time.sleep(30)\nelse: print(p,flush=True); sys.exit(7)")
        self.assertEqual(result.returncode, 7)
        self.assertFalse(result.timed_out)
        self.assert_child_stopped(int(result.stdout))

    def test_descendant_without_inherited_pipes_is_stopped_after_wait(self):
        result, _ = self.capture("import os,time; p=os.fork();\n"
            "if p==0:\n os.close(0); os.close(1); os.close(2); time.sleep(30)\n"
            "else: print(p,flush=True)")
        self.assertEqual(result.returncode, 0)
        self.assert_child_stopped(int(result.stdout))

    def test_live_parent_timeout_and_truncation_remain_failures(self):
        result, _ = self.capture("import time; print('partial',flush=True); time.sleep(30)", timeout=.2)
        self.assertTrue(result.timed_out)
        self.assertEqual(result.stdout, b"partial\n")
        result, _ = self.capture("print('x'*10000,flush=True)", max_bytes=16)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.stdout), 16)

    def test_live_parent_after_pipe_eof_keeps_original_deadline(self):
        result, elapsed = self.capture("import os,time; os.close(0); os.close(1); os.close(2); time.sleep(.5)",
                                       timeout=.2)
        self.assertTrue(result.timed_out)
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(elapsed, .5)


if __name__ == "__main__":
    unittest.main()
