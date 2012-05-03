# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""
Tests for Volume Code.

"""

import cStringIO

import mox

from cinder import context
from cinder import exception
from cinder import db
from cinder import flags
from cinder import log as logging
from cinder.openstack.common import importutils
import cinder.policy
from cinder import rpc
from cinder import test
import cinder.volume.api

FLAGS = flags.FLAGS
LOG = logging.getLogger(__name__)


class VolumeTestCase(test.TestCase):
    """Test Case for volumes."""

    def setUp(self):
        super(VolumeTestCase, self).setUp()
        self.flags(connection_type='fake')
        self.volume = importutils.import_object(FLAGS.volume_manager)
        self.context = context.get_admin_context()

    def tearDown(self):
        super(VolumeTestCase, self).tearDown()

    @staticmethod
    def _create_volume(size='0', snapshot_id=None):
        """Create a volume object."""
        vol = {}
        vol['size'] = size
        vol['snapshot_id'] = snapshot_id
        vol['user_id'] = 'fake'
        vol['project_id'] = 'fake'
        vol['availability_zone'] = FLAGS.storage_availability_zone
        vol['status'] = "creating"
        vol['attach_status'] = "detached"
        return db.volume_create(context.get_admin_context(), vol)

    def test_create_delete_volume(self):
        """Test volume can be created and deleted."""
        volume = self._create_volume()
        volume_id = volume['id']
        self.volume.create_volume(self.context, volume_id)
        self.assertEqual(volume_id, db.volume_get(context.get_admin_context(),
                         volume_id).id)

        self.volume.delete_volume(self.context, volume_id)
        self.assertRaises(exception.NotFound,
                          db.volume_get,
                          self.context,
                          volume_id)

    def test_delete_busy_volume(self):
        """Test volume survives deletion if driver reports it as busy."""
        volume = self._create_volume()
        volume_id = volume['id']
        self.volume.create_volume(self.context, volume_id)

        self.mox.StubOutWithMock(self.volume.driver, 'delete_volume')
        self.volume.driver.delete_volume(mox.IgnoreArg()) \
                                              .AndRaise(exception.VolumeIsBusy)
        self.mox.ReplayAll()
        res = self.volume.delete_volume(self.context, volume_id)
        self.assertEqual(True, res)
        volume_ref = db.volume_get(context.get_admin_context(), volume_id)
        self.assertEqual(volume_id, volume_ref.id)
        self.assertEqual("available", volume_ref.status)

        self.mox.UnsetStubs()
        self.volume.delete_volume(self.context, volume_id)

    def test_create_volume_from_snapshot(self):
        """Test volume can be created from a snapshot."""
        volume_src = self._create_volume()
        self.volume.create_volume(self.context, volume_src['id'])
        snapshot_id = self._create_snapshot(volume_src['id'])
        self.volume.create_snapshot(self.context, volume_src['id'],
                                    snapshot_id)
        volume_dst = self._create_volume(0, snapshot_id)
        self.volume.create_volume(self.context, volume_dst['id'], snapshot_id)
        self.assertEqual(volume_dst['id'],
                         db.volume_get(
                             context.get_admin_context(),
                             volume_dst['id']).id)
        self.assertEqual(snapshot_id, db.volume_get(
                context.get_admin_context(),
                volume_dst['id']).snapshot_id)

        self.volume.delete_volume(self.context, volume_dst['id'])
        self.volume.delete_snapshot(self.context, snapshot_id)
        self.volume.delete_volume(self.context, volume_src['id'])

    def test_too_big_volume(self):
        """Ensure failure if a too large of a volume is requested."""
        # FIXME(vish): validation needs to move into the data layer in
        #              volume_create
        return True
        try:
            volume = self._create_volume('1001')
            self.volume.create_volume(self.context, volume)
            self.fail("Should have thrown TypeError")
        except TypeError:
            pass

    def test_too_many_volumes(self):
        """Ensure that NoMoreTargets is raised when we run out of volumes."""
        vols = []
        total_slots = FLAGS.iscsi_num_targets
        for _index in xrange(total_slots):
            volume = self._create_volume()
            self.volume.create_volume(self.context, volume['id'])
            vols.append(volume['id'])
        volume = self._create_volume()
        self.assertRaises(db.NoMoreTargets,
                          self.volume.create_volume,
                          self.context,
                          volume['id'])
        db.volume_destroy(context.get_admin_context(), volume['id'])
        for volume_id in vols:
            self.volume.delete_volume(self.context, volume_id)

    def test_run_attach_detach_volume(self):
        """Make sure volume can be attached and detached from instance."""
        instance_id = 'fake-inst'
        mountpoint = "/dev/sdf"
        volume = self._create_volume()
        volume_id = volume['id']
        self.volume.create_volume(self.context, volume_id)
        if FLAGS.fake_tests:
            db.volume_attached(self.context, volume_id, instance_id,
                                mountpoint)
        else:
            self.compute.attach_volume(self.context,
                                       instance_id,
                                       volume_id,
                                       mountpoint)
        vol = db.volume_get(context.get_admin_context(), volume_id)
        self.assertEqual(vol['status'], "in-use")
        self.assertEqual(vol['attach_status'], "attached")
        self.assertEqual(vol['mountpoint'], mountpoint)
        self.assertEqual(vol['instance_id'], instance_id)

        self.assertRaises(exception.Error,
                          self.volume.delete_volume,
                          self.context,
                          volume_id)
        if FLAGS.fake_tests:
            db.volume_detached(self.context, volume_id)
        else:
            pass
            self.compute.detach_volume(self.context,
                                       instance_id,
                                       volume_id)
        vol = db.volume_get(self.context, volume_id)
        self.assertEqual(vol['status'], "available")

        self.volume.delete_volume(self.context, volume_id)
        self.assertRaises(exception.VolumeNotFound,
                          db.volume_get,
                          self.context,
                          volume_id)

    def test_concurrent_volumes_get_different_targets(self):
        """Ensure multiple concurrent volumes get different targets."""
        volume_ids = []
        targets = []

        def _check(volume_id):
            """Make sure targets aren't duplicated."""
            volume_ids.append(volume_id)
            admin_context = context.get_admin_context()
            iscsi_target = db.volume_get_iscsi_target_num(admin_context,
                                                          volume_id)
            self.assert_(iscsi_target not in targets)
            targets.append(iscsi_target)
            LOG.debug(_("Target %s allocated"), iscsi_target)
        total_slots = FLAGS.iscsi_num_targets
        for _index in xrange(total_slots):
            volume = self._create_volume()
            d = self.volume.create_volume(self.context, volume['id'])
            _check(d)
        for volume_id in volume_ids:
            self.volume.delete_volume(self.context, volume_id)

    def test_multi_node(self):
        # TODO(termie): Figure out how to test with two nodes,
        # each of them having a different FLAG for storage_node
        # This will allow us to test cross-node interactions
        pass

    @staticmethod
    def _create_snapshot(volume_id, size='0'):
        """Create a snapshot object."""
        snap = {}
        snap['volume_size'] = size
        snap['user_id'] = 'fake'
        snap['project_id'] = 'fake'
        snap['volume_id'] = volume_id
        snap['status'] = "creating"
        return db.snapshot_create(context.get_admin_context(), snap)['id']

    def test_create_delete_snapshot(self):
        """Test snapshot can be created and deleted."""
        volume = self._create_volume()
        self.volume.create_volume(self.context, volume['id'])
        snapshot_id = self._create_snapshot(volume['id'])
        self.volume.create_snapshot(self.context, volume['id'], snapshot_id)
        self.assertEqual(snapshot_id,
                         db.snapshot_get(context.get_admin_context(),
                                         snapshot_id).id)

        self.volume.delete_snapshot(self.context, snapshot_id)
        self.assertRaises(exception.NotFound,
                          db.snapshot_get,
                          self.context,
                          snapshot_id)
        self.volume.delete_volume(self.context, volume['id'])

    def test_cant_delete_volume_with_snapshots(self):
        """Test snapshot can be created and deleted."""
        volume = self._create_volume()
        self.volume.create_volume(self.context, volume['id'])
        snapshot_id = self._create_snapshot(volume['id'])
        self.volume.create_snapshot(self.context, volume['id'], snapshot_id)
        self.assertEqual(snapshot_id,
                         db.snapshot_get(context.get_admin_context(),
                                         snapshot_id).id)

        volume['status'] = 'available'
        volume['host'] = 'fakehost'

        volume_api = cinder.volume.api.API()

        self.assertRaises(exception.InvalidVolume,
                          volume_api.delete,
                          self.context,
                          volume)
        self.volume.delete_snapshot(self.context, snapshot_id)
        self.volume.delete_volume(self.context, volume['id'])

    def test_can_delete_errored_snapshot(self):
        """Test snapshot can be created and deleted."""
        volume = self._create_volume()
        self.volume.create_volume(self.context, volume['id'])
        snapshot_id = self._create_snapshot(volume['id'])
        self.volume.create_snapshot(self.context, volume['id'], snapshot_id)
        snapshot = db.snapshot_get(context.get_admin_context(),
                                   snapshot_id)

        volume_api = cinder.volume.api.API()

        snapshot['status'] = 'badstatus'
        self.assertRaises(exception.InvalidVolume,
                          volume_api.delete_snapshot,
                          self.context,
                          snapshot)

        snapshot['status'] = 'error'
        self.volume.delete_snapshot(self.context, snapshot_id)
        self.volume.delete_volume(self.context, volume['id'])

    def test_create_snapshot_force(self):
        """Test snapshot in use can be created forcibly."""

        def fake_cast(ctxt, topic, msg):
            pass
        self.stubs.Set(rpc, 'cast', fake_cast)
        instance_id = 'fake-inst'

        volume = self._create_volume()
        self.volume.create_volume(self.context, volume['id'])
        db.volume_attached(self.context, volume['id'], instance_id,
                           '/dev/sda1')

        volume_api = cinder.volume.api.API()
        volume = volume_api.get(self.context, volume['id'])
        self.assertRaises(exception.InvalidVolume,
                          volume_api.create_snapshot,
                          self.context, volume,
                          'fake_name', 'fake_description')
        snapshot_ref = volume_api.create_snapshot_force(self.context,
                                                        volume,
                                                        'fake_name',
                                                        'fake_description')
        db.snapshot_destroy(self.context, snapshot_ref['id'])
        db.volume_destroy(self.context, volume['id'])

    def test_delete_busy_snapshot(self):
        """Test snapshot can be created and deleted."""
        volume = self._create_volume()
        volume_id = volume['id']
        self.volume.create_volume(self.context, volume_id)
        snapshot_id = self._create_snapshot(volume_id)
        self.volume.create_snapshot(self.context, volume_id, snapshot_id)

        self.mox.StubOutWithMock(self.volume.driver, 'delete_snapshot')
        self.volume.driver.delete_snapshot(mox.IgnoreArg()) \
                                            .AndRaise(exception.SnapshotIsBusy)
        self.mox.ReplayAll()
        self.volume.delete_snapshot(self.context, snapshot_id)
        snapshot_ref = db.snapshot_get(self.context, snapshot_id)
        self.assertEqual(snapshot_id, snapshot_ref.id)
        self.assertEqual("available", snapshot_ref.status)

        self.mox.UnsetStubs()
        self.volume.delete_snapshot(self.context, snapshot_id)
        self.volume.delete_volume(self.context, volume_id)


class DriverTestCase(test.TestCase):
    """Base Test class for Drivers."""
    driver_name = "cinder.volume.driver.FakeBaseDriver"

    def setUp(self):
        super(DriverTestCase, self).setUp()
        self.flags(volume_driver=self.driver_name,
                   logging_default_format_string="%(message)s")
        self.volume = importutils.import_object(FLAGS.volume_manager)
        self.context = context.get_admin_context()
        self.output = ""

        def _fake_execute(_command, *_args, **_kwargs):
            """Fake _execute."""
            return self.output, None
        self.volume.driver.set_execute(_fake_execute)

        log = logging.getLogger()
        self.stream = cStringIO.StringIO()
        log.logger.addHandler(logging.logging.StreamHandler(self.stream))

    def _attach_volume(self):
        """Attach volumes to an instance. This function also sets
           a fake log message."""
        return []

    def _detach_volume(self, volume_id_list):
        """Detach volumes from an instance."""
        for volume_id in volume_id_list:
            db.volume_detached(self.context, volume_id)
            self.volume.delete_volume(self.context, volume_id)


class VolumeDriverTestCase(DriverTestCase):
    """Test case for VolumeDriver"""
    driver_name = "cinder.volume.driver.VolumeDriver"

    def test_delete_busy_volume(self):
        """Test deleting a busy volume."""
        self.stubs.Set(self.volume.driver, '_volume_not_present',
                       lambda x: False)
        self.stubs.Set(self.volume.driver, '_delete_volume',
                       lambda x, y: False)
        # Want DriverTestCase._fake_execute to return 'o' so that
        # volume.driver.delete_volume() raises the VolumeIsBusy exception.
        self.output = 'o'
        self.assertRaises(exception.VolumeIsBusy,
                          self.volume.driver.delete_volume,
                          {'name': 'test1', 'size': 1024})
        # when DriverTestCase._fake_execute returns something other than
        # 'o' volume.driver.delete_volume() does not raise an exception.
        self.output = 'x'
        self.volume.driver.delete_volume({'name': 'test1', 'size': 1024})


class ISCSITestCase(DriverTestCase):
    """Test Case for ISCSIDriver"""
    driver_name = "cinder.volume.driver.ISCSIDriver"

    def _attach_volume(self):
        """Attach volumes to an instance. This function also sets
           a fake log message."""
        volume_id_list = []
        for index in xrange(3):
            vol = {}
            vol['size'] = 0
            vol_ref = db.volume_create(self.context, vol)
            self.volume.create_volume(self.context, vol_ref['id'])
            vol_ref = db.volume_get(self.context, vol_ref['id'])

            # each volume has a different mountpoint
            mountpoint = "/dev/sd" + chr((ord('b') + index))
            instance_id = 'fake-inst'
            db.volume_attached(self.context, vol_ref['id'], instance_id,
                               mountpoint)
            volume_id_list.append(vol_ref['id'])

        return volume_id_list

    def test_check_for_export_with_no_volume(self):
        """No log message when no volume is attached to an instance."""
        self.stream.truncate(0)
        instance_id = 'fake-inst'
        self.volume.check_for_export(self.context, instance_id)
        self.assertEqual(self.stream.getvalue(), '')

    def test_check_for_export_with_all_volume_exported(self):
        """No log message when all the processes are running."""
        volume_id_list = self._attach_volume()

        self.mox.StubOutWithMock(self.volume.driver.tgtadm, 'show_target')
        for i in volume_id_list:
            tid = db.volume_get_iscsi_target_num(self.context, i)
            self.volume.driver.tgtadm.show_target(tid)

        self.stream.truncate(0)
        self.mox.ReplayAll()
        instance_id = 'fake-inst'
        self.volume.check_for_export(self.context, instance_id)
        self.assertEqual(self.stream.getvalue(), '')
        self.mox.UnsetStubs()

        self._detach_volume(volume_id_list)

    def test_check_for_export_with_some_volume_missing(self):
        """Output a warning message when some volumes are not recognied
           by ietd."""
        volume_id_list = self._attach_volume()
        instance_id = 'fake-inst'

        tid = db.volume_get_iscsi_target_num(self.context, volume_id_list[0])
        self.mox.StubOutWithMock(self.volume.driver.tgtadm, 'show_target')
        self.volume.driver.tgtadm.show_target(tid).AndRaise(
            exception.ProcessExecutionError())

        self.mox.ReplayAll()
        self.assertRaises(exception.ProcessExecutionError,
                          self.volume.check_for_export,
                          self.context,
                          instance_id)
        msg = _("Cannot confirm exported volume id:%s.") % volume_id_list[0]
        self.assertTrue(0 <= self.stream.getvalue().find(msg))
        self.mox.UnsetStubs()

        self._detach_volume(volume_id_list)


class VolumePolicyTestCase(test.TestCase):

    def setUp(self):
        super(VolumePolicyTestCase, self).setUp()

        cinder.policy.reset()
        cinder.policy.init()

        self.context = context.get_admin_context()

    def tearDown(self):
        super(VolumePolicyTestCase, self).tearDown()
        cinder.policy.reset()

    def _set_rules(self, rules):
        cinder.common.policy.set_brain(cinder.common.policy.HttpBrain(rules))

    def test_check_policy(self):
        self.mox.StubOutWithMock(cinder.policy, 'enforce')
        target = {
            'project_id': self.context.project_id,
            'user_id': self.context.user_id,
        }
        cinder.policy.enforce(self.context, 'volume:attach', target)
        self.mox.ReplayAll()
        cinder.volume.api.check_policy(self.context, 'attach')

    def test_check_policy_with_target(self):
        self.mox.StubOutWithMock(cinder.policy, 'enforce')
        target = {
            'project_id': self.context.project_id,
            'user_id': self.context.user_id,
            'id': 2,
        }
        cinder.policy.enforce(self.context, 'volume:attach', target)
        self.mox.ReplayAll()
        cinder.volume.api.check_policy(self.context, 'attach', {'id': 2})
