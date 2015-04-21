
import asyncore
import logging
import mock
import socket
import threading
import time

from shinken.check import Check

logging.basicConfig(level=logging.DEBUG)

from test_simple import (
    unittest,
    modconf,
    nrpe_poller,
    NrpePollerTestMixin,
)


class FakeNrpeServer(threading.Thread):
    def __init__(self, port=0):
        super(FakeNrpeServer, self).__init__()
        self.setDaemon(True)
        self.port = port
        self.cli_socks = []  # will retain the client socks here
        sock = self.sock = socket.socket()
        sock.settimeout(1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('localhost', port))
        if not port:
            self.port = sock.getsockname()[1]
        sock.listen(0)
        self.running = True
        self.start()

    def stop(self):
        self.sock.close()
        self.running = False

    def run(self):
        while self.running:
            try:
                sock, addr = self.sock.accept()
            except socket.error as err:
                pass
            else:
                self.cli_socks.append(sock)
                self.handle_connection(sock)

        for s in self.cli_socks:
            s.close()

    def handle_connection(self, sock):
        data = sock.recv(4096)
        # a valid nrpe response:
        data = b'\x00'*4 + b'\x00'*4 + b'\x00'*2 + 'OK'.encode() + b'\x00'*1022
        sock.send(data)


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
        command = ("$USER1$/check_nrpe -H localhost -p %s -n -u -t 5 -c check_load3 -a 20"
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

        # launch_new_checks() really launch a new check :
        # it creates the nrpe client and directly make it to connect
        # to the server.
        # To give a bit of time to our fake server thread to accept
        # the incoming connection from the client we actually need
        # to sleep just a bit of time:
        time.sleep(0.1)

        self.assertTrue(fake_server.cli_socks,
                        'the client should have connected to our fake server')

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

        save_con = chk.con  # we have to retain the con because its unset

        orig_logger = nrpe_poller.booster_nrpe.logger

        log_mock = mock.MagicMock(wraps=nrpe_poller.booster_nrpe.logger)
        nrpe_poller.booster_nrpe.logger = log_mock

        # by manage_finished_checks :
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
