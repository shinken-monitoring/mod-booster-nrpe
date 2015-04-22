#!/usr/bin/env python
# Copyright (C) 2009-2010:
#    Gabes Jean, naparuba@gmail.com
#    Gerhard Lausser, Gerhard.Lausser@consol.de
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


#
# This file is used to test reading and processing of config files
#


import os
import time
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

from multiprocessing import Queue, Manager
from shinken.check import Check

try:
    import unittest2 as unittest
except ImportError:
    import unittest


from shinken.objects.module import Module
from shinken.message import Message


import booster_nrpe


modconf = Module()
modconf.module_name = "NrpePoller"
modconf.module_type = booster_nrpe.properties['type']
modconf.properties = booster_nrpe.properties.copy()


class NrpePollerTestMixin(object):

    def setUp(self):
        super(NrpePollerTestMixin, self).setUp()
        logger.setLevel(logging.DEBUG)

    def _setup_nrpe(self, modconf):
        mod = booster_nrpe.Nrpe_poller(modconf)
        inst = booster_nrpe.get_instance(mod)
        inst.init()
        return inst


@unittest.skipIf(os.name == 'nt', "nrpe poller don't work here")
class TestNrpePoller(NrpePollerTestMixin,
                     unittest.TestCase):

    def test_nrpe_poller(self):

        inst = self._setup_nrpe(modconf)

        manager = Manager()
        to_queue = manager.Queue()
        from_queue = manager.Queue()
        control_queue = Queue()

        # We prepare a check in the to_queue
        status = 'queue'
        command = "$USER1$/check_nrpe -H localhost33  -n -u -t 5 -c check_load3 -a 20" # -a arg1 arg2 arg3"
        ref = None
        t_to_to = time.time()
        c = Check(status, command, ref, t_to_to)

        msg = Message(id=0, type='Do', data=c)
        to_queue.put(msg)

        # The worker will read a message by loop. We want it to
        # do 2 loops, so we fake a message, adn the Number 2 is a real
        # exit one
        msg1 = Message(id=0, type='All is good, continue')
        msg2 = Message(id=0, type='Die')

        control_queue.put(msg1)
        for _ in xrange(1, 2):
            control_queue.put(msg1)

        control_queue.put(msg2)

        inst.work(to_queue, from_queue, control_queue)

        chk = from_queue.get()
        self.assertEqual('done', chk.status)
        self.assertEqual(2, chk.exit_status)


if __name__ == '__main__':
    unittest.main()
