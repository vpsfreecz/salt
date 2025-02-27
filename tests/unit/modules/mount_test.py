# -*- coding: utf-8 -*-
'''
    :codeauthor: :email:`Rupesh Tare <rupesht@saltstack.com>`
'''
# Import Python libs
from __future__ import absolute_import
import os

# Import Salt Testing Libs
from salttesting import TestCase, skipIf
from salttesting.mock import (
    mock_open,
    MagicMock,
    patch,
    NO_MOCK,
    NO_MOCK_REASON
)

# Import Salt Libs
import salt.utils
from salt.exceptions import CommandExecutionError


from salt.modules import mount

# Globals
mount.__grains__ = {}
mount.__salt__ = {}
mount.__context__ = {}

MOCK_SHELL_FILE = 'A B C D F G\n'


@skipIf(NO_MOCK, NO_MOCK_REASON)
class MountTestCase(TestCase):
    '''
    Test cases for salt.modules.mount
    '''
    def test_active(self):
        '''
        List the active mounts.
        '''
        with patch.dict(mount.__grains__, {'os': 'FreeBSD'}):
            mock = MagicMock(return_value='A B C D,E,F')
            with patch.dict(mount.__salt__, {'cmd.run_stdout': mock}):
                self.assertEqual(mount.active(), {'B':
                                                  {'device': 'A',
                                                   'opts': ['D', 'E', 'F'],
                                                   'fstype': 'C'}})

        with patch.dict(mount.__grains__, {'os': 'Solaris'}):
            mock = MagicMock(return_value='A * B * C D/E/F')
            with patch.dict(mount.__salt__, {'cmd.run_stdout': mock}):
                self.assertEqual(mount.active(), {'B':
                                                  {'device': 'A',
                                                   'opts': ['D', 'E', 'F'],
                                                   'fstype': 'C'}})

        with patch.dict(mount.__grains__, {'os': 'OpenBSD'}):
            mock = MagicMock(return_value={})
            with patch.object(mount, '_active_mounts_openbsd', mock):
                self.assertEqual(mount.active(), {})

        with patch.dict(mount.__grains__, {'os': 'MacOS'}):
            mock = MagicMock(return_value={})
            with patch.object(mount, '_active_mounts_darwin', mock):
                self.assertEqual(mount.active(), {})

        with patch.dict(mount.__grains__, {'os': 'MacOS'}):
            mock = MagicMock(return_value={})
            with patch.object(mount, '_active_mountinfo', mock):
                with patch.object(mount, '_active_mounts_darwin', mock):
                    self.assertEqual(mount.active(extended=True), {})

    def test_fstab(self):
        '''
        List the content of the fstab
        '''
        mock = MagicMock(return_value=False)
        with patch.object(os.path, 'isfile', mock):
            self.assertEqual(mount.fstab(), {})

        mock = MagicMock(return_value=True)
        with patch.object(os.path, 'isfile', mock):
            file_data = '\n'.join(['#',
                                   'A B C D,E,F G H'])
            with patch('salt.utils.fopen',
                       mock_open(read_data=file_data),
                       create=True) as m:
                m.return_value.__iter__.return_value = file_data.splitlines()
                self.assertEqual(mount.fstab(), {'B': {'device': 'A',
                                                       'dump': 'G',
                                                       'fstype': 'C',
                                                       'opts': ['D', 'E', 'F'],
                                                       'pass': 'H'}})

    def test_rm_fstab(self):
        '''
        Remove the mount point from the fstab
        '''
        mock = MagicMock(return_value={})
        with patch.object(mount, 'fstab', mock):
            with patch('salt.utils.fopen', mock_open()) as m_open:
                self.assertTrue(mount.rm_fstab('name', 'device'))

        mock_fstab = MagicMock(return_value={'name': 'name'})
        with patch.object(mount, 'fstab', mock_fstab):
            with patch('salt.utils.fopen', mock_open()) as m_open:
                m_open.side_effect = IOError(13, 'Permission denied:', '/file')
                self.assertRaises(CommandExecutionError,
                                  mount.rm_fstab,
                                  'name', 'device')

    def test_set_fstab(self):
        '''
        Tests to verify that this mount is represented in the fstab,
        change the mount to match the data passed, or add the mount
        if it is not present.
        '''
        mock = MagicMock(return_value=False)
        with patch.object(os.path, 'isfile', mock):
            self.assertRaises(CommandExecutionError,
                              mount.set_fstab, 'A', 'B', 'C')

        mock = MagicMock(return_value=True)
        mock_read = MagicMock(side_effect=OSError)
        with patch.object(os.path, 'isfile', mock):
            with patch.object(salt.utils, 'fopen', mock_read):
                self.assertRaises(CommandExecutionError,
                                  mount.set_fstab, 'A', 'B', 'C')

        mock = MagicMock(return_value=True)
        with patch.object(os.path, 'isfile', mock):
            with patch('salt.utils.fopen',
                       mock_open(read_data=MOCK_SHELL_FILE)):
                self.assertEqual(mount.set_fstab('A', 'B', 'C'), 'new')

    def test_rm_automaster(self):
        '''
        Remove the mount point from the auto_master
        '''
        mock = MagicMock(return_value={})
        with patch.object(mount, 'automaster', mock):
            self.assertTrue(mount.rm_automaster('name', 'device'))

        mock = MagicMock(return_value={'name': 'name'})
        with patch.object(mount, 'fstab', mock):
            self.assertTrue(mount.rm_automaster('name', 'device'))

    def test_set_automaster(self):
        '''
        Verify that this mount is represented in the auto_salt, change the mount
        to match the data passed, or add the mount if it is not present.
        '''
        mock = MagicMock(return_value=True)
        with patch.object(os.path, 'isfile', mock):
            self.assertRaises(CommandExecutionError,
                              mount.set_automaster,
                              'A', 'B', 'C')

    def test_automaster(self):
        '''
        Test the list the contents of the fstab
        '''
        self.assertDictEqual(mount.automaster(), {})

    def test_mount(self):
        '''
        Mount a device
        '''
        with patch.dict(mount.__grains__, {'os': 'MacOS'}):
            mock = MagicMock(return_value=True)
            with patch.object(os.path, 'exists', mock):
                mock = MagicMock(return_value=None)
                with patch.dict(mount.__salt__, {'file.mkdir': None}):
                    mock = MagicMock(return_value={'retcode': True,
                                                   'stderr': True})
                    with patch.dict(mount.__salt__, {'cmd.run_all': mock}):
                        self.assertTrue(mount.mount('name', 'device'))

                    mock = MagicMock(return_value={'retcode': False,
                                                   'stderr': False})
                    with patch.dict(mount.__salt__, {'cmd.run_all': mock}):
                        self.assertTrue(mount.mount('name', 'device'))

    def test_remount(self):
        '''
        Attempt to remount a device, if the device is not already mounted, mount
        is called
        '''
        with patch.dict(mount.__grains__, {'os': 'MacOS'}):
            mock = MagicMock(return_value=[])
            with patch.object(mount, 'active', mock):
                mock = MagicMock(return_value=True)
                with patch.object(mount, 'mount', mock):
                    self.assertTrue(mount.remount('name', 'device'))

    def test_umount(self):
        '''
        Attempt to unmount a device by specifying the directory it is
        mounted on
        '''
        mock = MagicMock(return_value={})
        with patch.object(mount, 'active', mock):
            self.assertEqual(mount.umount('name'),
                             'name does not have anything mounted')

        mock = MagicMock(return_value={'name': 'name'})
        with patch.object(mount, 'active', mock):
            mock = MagicMock(return_value={'retcode': True, 'stderr': True})
            with patch.dict(mount.__salt__, {'cmd.run_all': mock}):
                self.assertTrue(mount.umount('name'))

            mock = MagicMock(return_value={'retcode': False})
            with patch.dict(mount.__salt__, {'cmd.run_all': mock}):
                self.assertTrue(mount.umount('name'))

    def test_is_fuse_exec(self):
        '''
        Returns true if the command passed is a fuse mountable application
        '''
        with patch.object(salt.utils, 'which', return_value=None):
            self.assertFalse(mount.is_fuse_exec('cmd'))

        with patch.object(salt.utils, 'which', return_value=True):
            self.assertFalse(mount.is_fuse_exec('cmd'))

        mock = MagicMock(side_effect=[1, 0])
        with patch.object(salt.utils, 'which', mock):
            self.assertFalse(mount.is_fuse_exec('cmd'))

    def test_swaps(self):
        '''
        Return a dict containing information on active swap
        '''

        file_data = '\n'.join(['Filename Type Size Used Priority',
                               '/dev/sda1 partition 31249404 4100 -1'])
        with patch.dict(mount.__grains__, {'os': ''}):
            with patch('salt.utils.fopen',
                       mock_open(read_data=file_data),
                       create=True) as m:
                m.return_value.__iter__.return_value = file_data.splitlines()

                self.assertDictEqual(mount.swaps(), {'/dev/sda1':
                                                     {'priority': '-1',
                                                      'size': '31249404',
                                                      'type': 'partition',
                                                      'used': '4100'}})

        file_data = '\n'.join(['Device Size Used Unknown Unknown Priority',
                               '/dev/sda1 31249404 4100 unknown unknown -1'])
        mock = MagicMock(return_value=file_data)
        with patch.dict(mount.__grains__, {'os': 'OpenBSD'}):
            with patch.dict(mount.__salt__, {'cmd.run_stdout': mock}):
                self.assertDictEqual(mount.swaps(), {'/dev/sda1':
                                                     {'priority': '-1',
                                                      'size': '31249404',
                                                      'type': 'partition',
                                                      'used': '4100'}})

    def test_swapon(self):
        '''
        Activate a swap disk
        '''
        mock = MagicMock(return_value={'name': 'name'})
        with patch.object(mount, 'swaps', mock):
            self.assertEqual(mount.swapon('name'),
                             {'stats': 'name', 'new': False})

        mock = MagicMock(return_value={})
        with patch.object(mount, 'swaps', mock):
            mock = MagicMock(return_value=None)
            with patch.dict(mount.__salt__, {'cmd.run': mock}):
                self.assertEqual(mount.swapon('name', False), {})

        mock = MagicMock(side_effect=[{}, {'name': 'name'}])
        with patch.object(mount, 'swaps', mock):
            mock = MagicMock(return_value=None)
            with patch.dict(mount.__salt__, {'cmd.run': mock}):
                self.assertEqual(mount.swapon('name'), {'stats': 'name',
                                                        'new': True})

    def test_swapoff(self):
        '''
        Deactivate a named swap mount
        '''
        mock = MagicMock(return_value={})
        with patch.object(mount, 'swaps', mock):
            self.assertEqual(mount.swapoff('name'), None)

        mock = MagicMock(return_value={'name': 'name'})
        with patch.object(mount, 'swaps', mock):
            with patch.dict(mount.__grains__, {'os': 'test'}):
                mock = MagicMock(return_value=None)
                with patch.dict(mount.__salt__, {'cmd.run': mock}):
                    self.assertFalse(mount.swapoff('name'))

        mock = MagicMock(side_effect=[{'name': 'name'}, {}])
        with patch.object(mount, 'swaps', mock):
            with patch.dict(mount.__grains__, {'os': 'test'}):
                mock = MagicMock(return_value=None)
                with patch.dict(mount.__salt__, {'cmd.run': mock}):
                    self.assertTrue(mount.swapoff('name'))

    def test_is_mounted(self):
        '''
        Provide information if the path is mounted
        '''
        mock = MagicMock(return_value={})
        with patch.object(mount, 'active', mock):
            self.assertFalse(mount.is_mounted('name'))

        mock = MagicMock(return_value={'name': 'name'})
        with patch.object(mount, 'active', mock):
            self.assertTrue(mount.is_mounted('name'))

if __name__ == '__main__':
    from integration import run_tests
    run_tests(MountTestCase, needs_daemon=False)
