# -*- coding: utf-8 -*-
'''
    :codeauthor: :email:`Jayesh Kariya <jayeshk@saltstack.com>`
'''

# Import Python libs
from __future__ import absolute_import

# Import Salt Testing Libs
from salttesting import TestCase, skipIf
from salttesting.mock import MagicMock, patch, NO_MOCK, NO_MOCK_REASON

# Import Salt Libs
from salt.modules import groupadd

# Import python Libs
import grp


@skipIf(NO_MOCK, NO_MOCK_REASON)
class GroupAddTestCase(TestCase):
    '''
    TestCase for salt.modules.groupadd
    '''
    groupadd.__grains__ = {}
    groupadd.__salt__ = {}
    groupadd.__context__ = {}
    mock_group = {'passwd': '*', 'gid': 0, 'name': 'test', 'members': ['root']}
    mock_getgrnam = grp.struct_group(('foo', '*', 20, ['test']))

    # 'add' function tests: 1

    def test_add(self):
        '''
        Tests if specified group was added
        '''
        mock = MagicMock(return_value={'retcode': 0})
        with patch.dict(groupadd.__salt__, {'cmd.run_all': mock}):
            self.assertTrue(groupadd.add('test', 100))

        with patch.dict(groupadd.__grains__, {'kernel': 'Linux'}):
            with patch.dict(groupadd.__salt__, {'cmd.run_all': mock}):
                self.assertTrue(groupadd.add('test', 100, True))

    # 'info' function tests: 1

    @patch('grp.getgrnam', MagicMock(return_value=mock_getgrnam))
    def test_info(self):
        '''
        Tests the return of group information
        '''
        ret = {'passwd': '*', 'gid': 20, 'name': 'foo', 'members': ['test']}
        self.assertEqual(groupadd.info('foo'), ret)

    # '_format_info' function tests: 1

    @patch('salt.modules.groupadd._format_info',
           MagicMock(return_value=mock_group))
    def test_format_info(self):
        '''
        Tests the formatting of returned group information
        '''
        data = grp.struct_group(('wheel', '*', 0, ['root']))
        ret = {'passwd': '*', 'gid': 0, 'name': 'test', 'members': ['root']}
        self.assertDictEqual(groupadd._format_info(data), ret)

    # 'getent' function tests: 1

    @patch('grp.getgrall', MagicMock(return_value=[mock_getgrnam]))
    def test_getent(self):
        '''
        Tests the return of information on all groups
        '''
        ret = [{'passwd': '*', 'gid': 20, 'name': 'foo', 'members': ['test']}]
        self.assertEqual(groupadd.getent(), ret)

    # 'chgid' function tests: 2

    def test_chgid_gid_same(self):
        '''
        Tests if the group id is the same as argument
        '''
        mock_pre_gid = MagicMock(return_value=10)
        with patch.dict(groupadd.__salt__,
                        {'file.group_to_gid': mock_pre_gid}):
            self.assertTrue(groupadd.chgid('test', 10))

    def test_chgid(self):
        '''
        Tests the gid for a named group was changed
        '''
        mock_pre_gid = MagicMock(return_value=0)
        mock_cmdrun = MagicMock(return_value=0)
        with patch.dict(groupadd.__salt__,
                        {'file.group_to_gid': mock_pre_gid}):
            with patch.dict(groupadd.__salt__, {'cmd.run': mock_cmdrun}):
                self.assertFalse(groupadd.chgid('test', 500))

    # 'delete' function tests: 1

    def test_delete(self):
        '''
        Tests if the specified group was deleted
        '''
        mock_ret = MagicMock(return_value={'retcode': 0})
        with patch.dict(groupadd.__salt__, {'cmd.run_all': mock_ret}):
            self.assertTrue(groupadd.delete('test'))

    # 'adduser' function tests: 1

    def test_adduser(self):
        '''
        Tests if specified user gets added in the group.
        '''
        os_version_list = [
            {'grains': {'kernel': 'Linux', 'os_family': 'RedHat', 'osmajorrelease': '5'},
             'cmd': 'gpasswd -a root test'},

            {'grains': {'kernel': 'Linux', 'os_family': 'Suse', 'osrelease_info': [11, 2]},
             'cmd': 'usermod -A test root'},

            {'grains': {'kernel': 'Linux'},
             'cmd': 'gpasswd --add root test'},

            {'grains': {'kernel': 'OTHERKERNEL'},
             'cmd': 'usermod -G test root'},
        ]

        for os_version in os_version_list:
            mock = MagicMock(return_value={'retcode': 0})
            with patch.dict(groupadd.__grains__, os_version['grains']):
                with patch.dict(groupadd.__salt__, {'cmd.retcode': mock}):
                    self.assertFalse(groupadd.adduser('test', 'root'))
                    groupadd.__salt__['cmd.retcode'].assert_called_once_with(os_version['cmd'], python_shell=False)

    # 'deluser' function tests: 1

    def test_deluser(self):
        '''
        Tests if specified user gets deleted from the group.
        '''
        os_version_list = [
            {'grains': {'kernel': 'Linux', 'os_family': 'RedHat', 'osmajorrelease': '5'},
             'cmd': 'gpasswd -d root test'},

            {'grains': {'kernel': 'Linux', 'os_family': 'Suse', 'osrelease_info': [11, 2]},
             'cmd': 'usermod -R test root'},

            {'grains': {'kernel': 'Linux'},
             'cmd': 'gpasswd --del root test'},

            {'grains': {'kernel': 'OpenBSD'},
             'cmd': 'usermod -S foo root'},
        ]

        for os_version in os_version_list:
            mock_ret = MagicMock(return_value={'retcode': 0})
            mock_stdout = MagicMock(return_value='test foo')
            mock_info = MagicMock(return_value={'passwd': '*',
                                                'gid': 0,
                                                'name': 'test',
                                                'members': ['root']})

            with patch.dict(groupadd.__grains__, os_version['grains']):
                with patch.dict(groupadd.__salt__, {'cmd.retcode': mock_ret,
                                                    'group.info': mock_info,
                                                    'cmd.run_stdout': mock_stdout}):
                    self.assertFalse(groupadd.deluser('test', 'root'))
                    groupadd.__salt__['cmd.retcode'].assert_called_once_with(os_version['cmd'], python_shell=False)

    # 'deluser' function tests: 1

    def test_members(self):
        '''
        Tests if members of the group, get replaced with a provided list.
        '''
        os_version_list = [
            {'grains': {'kernel': 'Linux', 'os_family': 'RedHat', 'osmajorrelease': '5'},
             'cmd': "gpasswd -M foo test"},

            {'grains': {'kernel': 'Linux', 'os_family': 'Suse', 'osrelease_info': [11, 2]},
             'cmd': 'groupmod -A foo test'},

            {'grains': {'kernel': 'Linux'},
             'cmd': 'gpasswd --members foo test'},

            {'grains': {'kernel': 'OpenBSD'},
             'cmd': 'usermod -G test foo'},
        ]

        for os_version in os_version_list:
            mock_ret = MagicMock(return_value={'retcode': 0})
            mock_stdout = MagicMock(return_value={'cmd.run_stdout': 1})
            mock_info = MagicMock(return_value={'passwd': '*',
                                                'gid': 0,
                                                'name': 'test',
                                                'members': ['root']})
            mock = MagicMock(return_value=True)

            with patch.dict(groupadd.__grains__, os_version['grains']):
                with patch.dict(groupadd.__salt__, {'cmd.retcode': mock_ret,
                                                    'group.info': mock_info,
                                                    'cmd.run_stdout': mock_stdout,
                                                    'cmd.run': mock}):
                    self.assertFalse(groupadd.members('test', 'foo'))
                    groupadd.__salt__['cmd.retcode'].assert_called_once_with(os_version['cmd'], python_shell=False)


if __name__ == '__main__':
    from integration import run_tests
    run_tests(GroupAddTestCase, needs_daemon=False)
