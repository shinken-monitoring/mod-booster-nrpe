#!/usr/bin/python
# -*- coding: utf-8 -*-

# Copyright (C) 2009-2012:
#    Gabes Jean, naparuba@gmail.com
#    Gerhard Lausser, Gerhard.Lausser@consol.de
#    Gregory Starck, g.starck@gmail.com
#    Hartmut Goebel, h.goebel@goebel-consult.de
#
# This file is part of Shinken.
#
# Shinken is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Shinken is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Shinken.  If not, see <http://www.gnu.org/licenses/>.


# This Class is an example of an Scheduler module
# Here for the configuration phase AND running one

import sys
import signal
import time
import socket
import struct
import binascii
import asyncore
import getopt
import shlex
import traceback
import re

from Queue import Empty

communication_errors = (socket.error,)

try:
    import OpenSSL
except ImportError as openssl_import_error:
    OpenSSL = None
    SSLError = None
    SSLWantReadOrWrite = None
else:
    SSLError = OpenSSL.SSL.Error
    SSLWantReadOrWrite = (OpenSSL.SSL.WantReadError,
                          OpenSSL.SSL.WantWriteError)

    # consider SSLError's to also be a kind of communication error.
    communication_errors += (SSLError,)
    # effectively, under SSL mode, any TCP reset or such failure
    # will be raised as such an instance of SSLError, which isn't
    # a subclass of IOError nor socket.error but we want to catch
    # both so to retry a check in such cases.
    # Look for 'retried' and 'readwrite_error' in the code..


from shinken.basemodule import BaseModule
from shinken.log import logger


properties = {
    'daemons': ['poller'],
    'type': 'nrpe_poller',
    'external': False,
    # To be a real worker module, you must set this
    'worker_capable': True,
    }


# called by the plugin manager to get a broker
def get_instance(mod_conf):
    logger.info("[NRPEPoller] Get a nrpe poller module for plugin %s" % mod_conf.get_name())
    return Nrpe_poller(mod_conf)


NRPE_DATA_PACKET_SIZE = 1034  # REALLY important .. !


class NRPE(object):

    def __init__(self, host, port, use_ssl, command):
        self.state = 'creation'
        self.host = host
        self.port = port
        '''
        Build a query packet
         00-01: NRPE protocol version
         02-03: packet type (01: query, 02: response)
         04-07: CRC32
         08-09: return code of the check if packet type is response
         10-1034: command (nul terminated)
         1035-1036: reserved
        '''
        crc = 0

        if not command:
            self.rc = 3
            self.message = "Error : no command asked from nrpe query"
            self.state = 'received'
            return

        # We pack it, then we compute CRC32 of this first query
        query = struct.pack(">2hih1024scc", 02, 01, crc, 0, command, 'N', 'D')
        crc = binascii.crc32(query)

        # we restart with the crc value this time
        # because python2.4 do not have pack_into.
        self.query = struct.pack(">2hih1024scc", 02, 01, crc, 0, command, 'N', 'D')


    # Read a return and extract return code
    # and output
    def read(self, data):
        # TODO: Not sure to get all the data in one shot.
        # TODO we should buffer it until we get enough to unpack.

        if self.state == 'received':
            return self.rc, self.message

        self.state = 'received'
        # TODO: check crc

        try:
            response = struct.unpack(">2hih1024s", data)
        except struct.error as err:  # bad format...
            self.rc = 3
            self.message = ("Error : cannot unpack output ; "
                            "datalen=%s : err=%s" % (len(data), err))
        else:
            self.rc = response[3]
            # the output is padded with \x00 at the end so
            # we remove it.
            self.message = re.sub('\x00.*$', '', response[4])
            crc_orig = response[2]

        return self.rc, self.message


class NRPEAsyncClient(asyncore.dispatcher, object):

    def __init__(self, host, port, use_ssl, timeout, unknown_on_timeout, msg):
        asyncore.dispatcher.__init__(self)

        self.use_ssl = use_ssl
        self.start_time = time.time()
        self.timeout = timeout
        self._rc_on_timeout = 3 if unknown_on_timeout else 2
        self.readwrite_error = False  # there was an error at the tcp level..

        # Instantiate our nrpe helper
        self.nrpe = NRPE(host, port, use_ssl, msg)
        self.socket = None

        # And now we create a socket for our connection
        try:
            addrinfo = socket.getaddrinfo(host, port)[0]
        except socket.error as err:
            self.set_exit(2, "Cannot getaddrinfo: %s" % err)
            return

        self.create_socket(addrinfo[0], socket.SOCK_STREAM)

        if use_ssl:
            # The admin want a ssl connection,
            # but there is not openssl lib installed :(
            if OpenSSL is None:
                logger.warning("Python openssl lib is not installed! "
                               "Cannot use ssl, switching back to no-ssl mode; "
                               "original import error: %s",
                               openssl_import_error)
                self.use_ssl = False
            else:
                self.wrap_ssl()

        addr = (host, port)
        try:
            self.connect(addr)
        except socket.error as err:
            self.set_exit(2, "Cannot connect to %s: %s" % (addr, err))
        else:
            self.rc = 3
            self.message = 'Sending request and waiting response..'

    def wrap_ssl(self):
        self.context = OpenSSL.SSL.Context(OpenSSL.SSL.TLSv1_METHOD)
        self.context.set_cipher_list('ADH')
        self._socket = self.socket  # keep the bare socket for later shutdown/close
        self.socket = OpenSSL.SSL.Connection(self.context, self.socket)
        self.set_accept_state()

    def close(self):
        if self.socket is None:
            return
        if self.use_ssl:
            for idx in range(4):
                try:
                    if self.socket.shutdown():
                        break
                except SSLWantReadOrWrite:
                    pass  # just retry for now
                    # or:
                    # asyncore.poll2(0.5)
                    # but not sure we really need it as the SSL shutdown()
                    # should take care of it.
                except SSLError as err:
                    # on python2.7 I keep getting SSLError instance having no
                    # 'reason' nor 'library' attribute or any other detail.
                    # despite the docs telling the opposite:
                    # https://docs.python.org/2/library/ssl.html#ssl.SSLError
                    details = 'library=%s reason=%s : %s' % (
                        getattr(err, 'library', 'missing'),
                        getattr(err, 'reason', 'missing'),
                        err)
                    # output the error in debug mode for now.
                    logger.debug('Error on SSL shutdown : %s ; %s',
                                 details, traceback.format_exc())
                    # keep retry.
            sock = self._socket
        else:
            sock = self.socket
        try:
            # Also always shutdown the underlying socket:
            sock.shutdown(socket.SHUT_RDWR)
        except socket.error as err:
            logger.debug('socket.shutdown failed: %s', str(err))
        super(NRPEAsyncClient, self).close()
        self.socket = None

    def set_exit(self, rc, message):
        self.close()
        self.rc = rc
        self.message = message
        self.execution_time = time.time() - self.start_time
        self.nrpe.state = 'received'

    # Check if we are in timeout. If so, just bailout
    # and set the correct return code from timeout
    # case
    def look_for_timeout(self):
        now = time.time()
        if now - self.start_time > self.timeout:
            message = ('Error: connection timeout after %d seconds'
                       % self.timeout)
            self.set_exit(self._rc_on_timeout, message)

    # We got a read from the socket and keep receiving until it has
    # finished. Maybe it's just a SSL handshake continuation, if so
    # we continue it and wait for handshake finish
    def handle_read(self):
        if self.is_done():
            return
        try:
            self._handle_read()
        except communication_errors as err:
            self.readwrite_error = True
            self.set_exit(2, "Error on read: %s" % err)

    def _handle_read(self):
        try:
            buf = self.recv(NRPE_DATA_PACKET_SIZE)
        except SSLWantReadOrWrite:
            # if we are in ssl, there can be a handshake
            # problem: we can't talk until we finished it.
            try:
                self.socket.do_handshake()
            except SSLWantReadOrWrite:
                pass
            return
        else:
            # Maybe we got nothing from the server (it refused our IP,
            # or our arguments...)
            if buf:
                rc, message = self.nrpe.read(buf)
            else:
                rc = 2
                message = "Error: Empty response from the NRPE server. Are we blacklisted ?"

        self.set_exit(rc, message)

    # Did we finished our job?
    def writable(self):
        return not self.is_done() and self.nrpe.query

    # We can write to the socket. If we are in the ssl handshake phase
    # we just continue it and return. If we finished it, we can write our
    # query
    def handle_write(self):
        try:
            self._handle_write()
        except communication_errors as err:
            self.readwrite_error = True
            self.set_exit(2, 'Error on write: %s' % err)

    def _handle_write(self):
        try:
            sent = self.send(self.nrpe.query)
        except SSLWantReadOrWrite:
            # SSL write/send can require a read ! yes ;)
            try:
                self.socket.do_handshake()
            except SSLWantReadOrWrite:
                # still not finished, we continue
                pass
        else:
            # Maybe we did not send all our query
            # so we bufferize it
            self.nrpe.query = self.nrpe.query[sent:]

    def is_done(self):
        return self.nrpe.state == 'received'

    def handle_error(self):
        err_type, err, tb = sys.exc_info()
        self.set_exit(2, "Error: %s" % str(err))


def parse_args(cmd_args):
    # Default params
    host = None
    command = None
    port = 5666
    unknown_on_timeout = False
    timeout = 10
    use_ssl = True
    add_args = []

    # Manage the options
    try:
        opts, args = getopt.getopt(cmd_args, "H::p::nut::c::a::", [])
    except getopt.GetoptError as err:
        # If we got problem, bail out
        logger.error("Could not parse a command: %s" % err)
        return host, port, unknown_on_timeout, command, timeout, use_ssl, add_args

    #print  "Opts", opts, "Args", args
    for o, a in opts:
        if o == "-H":
            host = a
        elif o == "-p":
            port = int(a)
        elif o == "-c":
            command = a
        elif o == '-t':
            timeout = int(a)
        elif o == '-u':
            unknown_on_timeout = True
        elif o == '-n':
            use_ssl = False
        elif o == '-a':
            # Here we got a, btu also all 'args'
            add_args.append(a)
            add_args.extend(args)

    return host, port, unknown_on_timeout, command, timeout, use_ssl, add_args


# Just print some stuff
class Nrpe_poller(BaseModule):

    def __init__(self, *a, **kw):
        super(Nrpe_poller, self).__init__(*a, **kw)
        self.checks = []

    # Called by poller to say 'get ready'
    def init(self):
        logger.info("[NRPEPoller] Initialization of the nrpe poller module")
        self.i_am_dying = False

    def add_new_check(self, check):
        check.retried = 0
        self.checks.append(check)

    # Get new checks
    # REF: doc/shinken-action-queues.png (3)
    def get_new_checks(self):
        while True:
            try:
                msg = self.s.get(block=False)
            except Empty:
                return
            if msg is not None:
                check = msg.get_data()
                self.add_new_check(check)

    # Launch checks that are in status
    # REF: doc/shinken-action-queues.png (4)
    def launch_new_checks(self):
        for check in self.checks:
            now = time.time()
            if check.status == 'queue':
                # Ok we launch it
                check.status = 'launched'
                check.check_time = now

                # We want the args of the commands so we parse it like a shell
                # shlex want str only
                clean_command = shlex.split(check.command.encode('utf8', 'ignore'))

                # If the command seems good
                if len(clean_command) > 1:
                    # we do not want the first member, check_nrpe thing
                    args = parse_args(clean_command[1:])
                    (host, port, unknown_on_timeout,
                     command, timeout, use_ssl, add_args) = args
                else:
                    # Set an error so we will quit tis check
                    command = None

                # If we do not have the good args, we bail out for this check
                if command is None or host is None:
                    check.status = 'done'
                    check.exit_status = 2
                    check.get_outputs('Error: the parameters host '
                                      'or command are not correct.', 8012)
                    check.execution_time = 0
                    continue

                # Ok we are good, we go on
                total_args = [command]
                total_args.extend(add_args)
                cmd = r'!'.join(total_args)
                check.con = NRPEAsyncClient(host, port, use_ssl,
                                            timeout, unknown_on_timeout, cmd)

    # Check the status of checks
    # if done, return message finished :)
    # REF: doc/shinken-action-queues.png (5)
    def manage_finished_checks(self):
        to_del = []

        # First look for checks in timeout
        for check in self.checks:
            if check.status == 'launched':
                check.con.look_for_timeout()

        # Now we look for finished checks
        for check in self.checks:
            # First manage check in error, bad formed
            if check.status == 'done':
                to_del.append(check)
                self.returns_queue.put(check)

            # Then we check for good checks
            elif check.status == 'launched' and check.con.is_done():
                con = check.con
                # unlink our object from the original check,
                # this might be necessary to allow the check to be again
                # serializable..
                del check.con
                if con.readwrite_error and check.retried < 2:
                    logger.warning('%s: Got an IO error (%s), retrying 1 more time.. (cur=%s)',
                                   check.command, con.message, check.retried)
                    check.retried += 1
                    check.status = 'queue'
                    continue

                if check.retried:
                    logger.info('%s: Successfully retried check :)', check.command)

                check.status = 'done'
                check.exit_status = con.rc
                check.get_outputs(con.message, 8012)
                check.execution_time = con.execution_time

                # and set this check for deleting
                # and try to send it
                to_del.append(check)
                self.returns_queue.put(check)

        # And delete finished checks
        for chk in to_del:
            self.checks.remove(chk)

    # Wrapper function for work in order to catch the exception
    # to see the real work, look at do_work
    def work(self, s, returns_queue, c):
        try:
            self.do_work(s, returns_queue, c)
        except Exception as err:
            logger.exception("NRPE: Got an unhandled exception: %s", err)
            # Ok I die now
            raise

    # id = id of the worker
    # s = Global Queue Master->Slave
    # m = Queue Slave->Master
    # return_queue = queue managed by manager
    # c = Control Queue for the worker
    def do_work(self, s, returns_queue, c):
        logger.info("[NRPEPoller] Module started!")
        ## restore default signal handler for the workers:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        self.set_proctitle(self.name)

        self.returns_queue = returns_queue
        self.s = s
        self.t_each_loop = time.time()

        while True:

            # We check if all new things in connections
            # NB : using poll2 instead of poll (poll1 is with select
            # call that is limited to 1024 connexions, poll2 is ... poll).
            asyncore.poll2(1)

            # If we are dying (big problem!) we do not
            # take new jobs, we just finished the current one
            if not self.i_am_dying:
                # REF: doc/shinken-action-queues.png (3)
                self.get_new_checks()
                # REF: doc/shinken-action-queues.png (4)
                self.launch_new_checks()

            # REF: doc/shinken-action-queues.png (5)
            self.manage_finished_checks()

            # Now get order from master, if any..
            try:
                msg = c.get(block=False)
            except Empty:
                pass
            else:
                if msg.get_type() == 'Die':
                    logger.info("[NRPEPoller] Dad says we should die...")
                    break

