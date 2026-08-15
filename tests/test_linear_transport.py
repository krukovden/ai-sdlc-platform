"""Every way the network can fail must become the documented exit code.

Exit code 2 means "the board rejected us, or could not be reached". A failure
that escapes as a traceback instead breaks that promise for every caller, and
a caller that cannot distinguish "unreachable" from "malformed request" cannot
decide whether retrying is safe. These tests pin one case per failure mode.
"""

import io
import unittest
import urllib.error
from unittest import mock

from support import ScriptTestCase, linear


def raises(exception):
    def opener(*args, **kwargs):
        raise exception
    return opener


class TransportFailureTests(ScriptTestCase):

    def call(self):
        return linear.query("token", "{ viewer { id } }")

    def test_exits_2_when_the_socket_times_out_while_reading_the_response(self):
        # A bare TimeoutError, not wrapped in URLError - the case that escaped.
        with mock.patch.object(linear.urllib.request, "urlopen",
                               raises(TimeoutError("timed out"))):
            message = self.assert_exits(2, self.call)

        self.assertIn("30s", message)
        self.assertIn("check the card", message)

    def test_exits_2_and_says_so_plainly_when_the_key_is_rejected(self):
        error = urllib.error.HTTPError("url", 401, "Unauthorized", {}, io.BytesIO(b""))
        with mock.patch.object(linear.urllib.request, "urlopen", raises(error)):
            message = self.assert_exits(2, self.call)

        self.assertIn("API key", message)

    def test_exits_2_when_the_host_cannot_be_reached(self):
        error = urllib.error.URLError("nodename nor servname provided")
        with mock.patch.object(linear.urllib.request, "urlopen", raises(error)):
            message = self.assert_exits(2, self.call)

        self.assertIn("cannot reach Linear", message)

    def test_exits_3_when_the_board_refuses_the_request_itself(self):
        payload = b'{"errors":[{"message":"Cannot query field \\"nope\\""}]}'
        response = mock.MagicMock()
        response.read.return_value = payload
        response.__enter__.return_value = response
        with mock.patch.object(linear.urllib.request, "urlopen", return_value=response):
            message = self.assert_exits(3, self.call)

        self.assertIn("Cannot query field", message)


if __name__ == "__main__":
    unittest.main()
