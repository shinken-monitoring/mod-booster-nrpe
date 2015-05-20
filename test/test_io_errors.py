
import asyncore
import mock
import socket
import threading
import time

from shinken.check import Check

from test_simple import (
    unittest,
    modconf,
    NrpePollerTestMixin,
)

from booster_nrpe import booster_nrpe


class FakeNrpeServer(threading.Thread):
    def __init__(self, port=0):
        super(FakeNrpeServer, self).__init__()
        self.setDaemon(True)
        self.port = port
        self.cli_socks = []  # will retain the client socks here
        sock = self.sock = socket.socket()
        sock.settimeout(1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', port))
        if not port:
            self.port = sock.getsockname()[1]
        sock.listen(0)
        self.running = True
        self.start()

    def stop(self):
        self.running = False
        self.sock.close()

    def run(self):
        while self.running:
            try:
                sock, addr = self.sock.accept()
            except socket.error as err:
                pass
            else:
                # so that we won't block indefinitely in handle_connection
                # in case the client doesn't send anything :
                sock.settimeout(3)
                self.cli_socks.append(sock)
                self.handle_connection(sock)
                self.cli_socks.remove(sock)

    def handle_connection(self, sock):
        data = sock.recv(4096)
        # a valid nrpe response:
        data = b'\x00'*4 + b'\x00'*4 + b'\x00'*2 + 'OK'.encode() + b'\x00'*1022
        sock.send(data)
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        sock.close()


class Test_Errors(NrpePollerTestMixin,
                  unittest.TestCase):

    def setUp(self):
        self.fake_server = FakeNrpeServer()

    def tearDown(self):
        self.fake_server.stop()
        self.fake_server.join()

    def test_retry_on_io_error(self):

        fake_server = self.fake_server

        inst = self._setup_nrpe(modconf)

        inst.returns_queue = mock.MagicMock()

        # We prepare a check in the to_queue
        command = ("$USER1$/check_nrpe -H 127.0.0.1 -p %s -n -u -t 5 -c check_load3 -a 20"
                   % fake_server.port)

        chk = Check('queue', command, None, time.time())

        # GO
        inst.add_new_check(chk)

        self.assertFalse(fake_server.cli_socks,
                        'there should have no connected client '
                        'to our fake server at this point')

        inst.launch_new_checks()

        self.assertEqual('launched', chk.status)
        self.assertEqual(0, chk.retried)
        self.assertEqual('Sending request and waiting response..',
                         chk.con.message,
                         "what? chk=%s " % chk)

        # launch_new_checks() really launch a new check :
        # it creates the nrpe client and directly make it to connect
        # to the server.
        # To give a bit of time to our fake server thread to accept
        # the incoming connection from the client we actually need
        # to sleep just a bit of time:
        time.sleep(0.1)

        if not chk.con.connected:
            asyncore.poll2(0)

        self.assertTrue(fake_server.cli_socks,
                        'the client should have connected to our fake server.\n'
                        '-> %s' % chk.con.message)

        # that should make the client to send us its request:
        asyncore.poll2(0)

        # give some time to the server thread to read it and
        # send its response:
        time.sleep(0.1)

        m = mock.MagicMock(side_effect=socket.error('boum'))
        chk.con.recv = m  # this is what will trigger the desired effect..

        self.assertEqual('Sending request and waiting response..',
                         chk.con.message)

        # that should make the client to have its recv() method called:
        asyncore.poll2(0)
        self.assertEqual("Error on read: boum", chk.con.message)

        save_con = chk.con  # we have to retain the con because its unset..

        log_mock = mock.MagicMock(wraps=booster_nrpe.logger)
        booster_nrpe.logger = log_mock

        # ..by manage_finished_checks :
        inst.manage_finished_checks()

        log_mock.warning.assert_called_once_with(
            '%s: Got an IO error (%s), retrying 1 more time.. (cur=%s)',
            chk.command, save_con.message, 0)

        self.assertEqual('queue', chk.status)
        self.assertEqual(1, chk.retried,
                         "the client has got the error we raised")

        # now the check is going to be relaunched:
        inst.launch_new_checks()

        # this makes sure for it to be fully processed.
        for _ in range(2):
            asyncore.poll2(0)
            time.sleep(0.1)

        inst.manage_finished_checks()

        log_mock.info.assert_called_once_with(
            '%s: Successfully retried check :)', command)

        self.assertEqual(
            [], inst.checks,
            "the check should have be moved out to the nrpe internal checks list")

        inst.returns_queue.put.assert_called_once_with(chk)

        self.assertEqual(0, chk.exit_status)
        self.assertEqual(1, chk.retried)


if __name__ == '__main__':
    unittest.main()
